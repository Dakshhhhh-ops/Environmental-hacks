"""Storage abstraction — DynamoDB when configured, JSON files otherwise."""
from __future__ import annotations

import json
import os
import uuid
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..config import settings
from ..models import Proposal, VerificationRecord


class StorageError(Exception):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ddb_safe(value):
    """boto3 DynamoDB rejects Python floats; serialize them as Decimal recursively."""
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {key: _ddb_safe(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_ddb_safe(item) for item in value]
    return value


def _local_path(name: str) -> Path:
    d = Path(settings.local_data_dir)
    d.mkdir(parents=True, exist_ok=True)
    # Protect local data dir — ensure .gitignore covers it
    return d / f"{name}.json"


def _read_local(name: str) -> list[dict]:
    p = _local_path(name)
    if not p.exists():
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
            if not isinstance(data, list):
                raise StorageError(f"Local {name} storage must contain a JSON list: {p}")
            return data
    except json.JSONDecodeError as exc:
        raise StorageError(f"Local {name} storage contains invalid JSON: {p}") from exc
    except OSError as exc:
        raise StorageError(f"Cannot read local {name} storage at {p}: {exc}") from exc


def _write_local(name: str, records: list[dict]) -> None:
    p = _local_path(name)
    # Atomic write
    tmp = p.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, ensure_ascii=False)
    tmp.replace(p)


class Storage:
    """Unified storage interface."""

    def __init__(self) -> None:
        self._use_dynamodb = False
        self._dynamodb = None
        self._table_proposals = None
        self._table_thresholds = None
        self._table_history = None
        if settings.use_dynamodb:
            try:
                import boto3
                self._dynamodb = boto3.resource("dynamodb", region_name=settings.aws_region)
                self._table_proposals = self._dynamodb.Table(settings.dynamodb_table_proposals)
                self._table_thresholds = self._dynamodb.Table(settings.dynamodb_table_thresholds)
                self._table_history = self._dynamodb.Table(settings.dynamodb_table_history)
                self._use_dynamodb = True
            except Exception as e:
                # Report clearly — do not silently switch to local
                raise StorageError(f"DynamoDB configured but unavailable: {e}") from e

    @property
    def mode(self) -> str:
        return "dynamodb" if self._use_dynamodb else "local"

    # --- Proposals ---

    def save_proposal(self, proposal: dict) -> None:
        if self._use_dynamodb:
            self._table_proposals.put_item(Item=_ddb_safe(proposal))
        else:
            records = _read_local("proposals")
            # Upsert by id
            records = [r for r in records if r.get("id") != proposal.get("id")]
            records.append(proposal)
            _write_local("proposals", records)

    def get_proposal(self, proposal_id: str) -> Optional[dict]:
        if self._use_dynamodb:
            resp = self._table_proposals.get_item(Key={"id": proposal_id})
            return resp.get("Item")
        else:
            for r in _read_local("proposals"):
                if r.get("id") == proposal_id:
                    return r
            return None

    def list_proposals(self) -> list[dict]:
        if self._use_dynamodb:
            resp = self._table_proposals.scan()
            items = resp.get("Items", [])
            # Handle pagination simply
            while "LastEvaluatedKey" in resp:
                resp = self._table_proposals.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
                items.extend(resp.get("Items", []))
            items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return items
        else:
            records = _read_local("proposals")
            records.sort(key=lambda x: x.get("created_at", ""), reverse=True)
            return records

    def update_proposal(self, proposal_id: str, updates: dict) -> Optional[dict]:
        if self._use_dynamodb:
            # Build UpdateExpression
            expr_parts: list[str] = []
            names: dict[str, str] = {}
            values: dict[str, object] = {}
            for i, (k, v) in enumerate(updates.items()):
                nk = f"#k{i}"
                vk = f":v{i}"
                names[nk] = k
                values[vk] = v
                expr_parts.append(f"{nk} = {vk}")
            expr = "SET " + ", ".join(expr_parts)
            resp = self._table_proposals.update_item(
                Key={"id": proposal_id},
                UpdateExpression=expr,
                ExpressionAttributeNames=names,
                ExpressionAttributeValues=_ddb_safe(values),
                ReturnValues="ALL_NEW",
            )
            return resp.get("Attributes")
        else:
            records = _read_local("proposals")
            found = False
            for r in records:
                if r.get("id") == proposal_id:
                    r.update(updates)
                    found = True
                    break
            if not found:
                return None
            _write_local("proposals", records)
            for r in records:
                if r.get("id") == proposal_id:
                    return r
            return None

    def claim_publication(self, proposal_id: str) -> bool:
        """Atomically reserve one SNS attempt so retries cannot double-publish."""
        if self._use_dynamodb:
            from botocore.exceptions import ClientError
            try:
                self._table_proposals.update_item(
                    Key={"id": proposal_id},
                    UpdateExpression="SET delivery_state = :sending",
                    ConditionExpression="attribute_exists(id) AND #status = :approved AND (attribute_not_exists(delivery_state) OR attribute_type(delivery_state, :nulltype))",
                    ExpressionAttributeNames={"#status": "status"},
                    ExpressionAttributeValues={":sending": "publishing", ":approved": "approved", ":nulltype": "NULL"},
                )
                return True
            except ClientError as exc:
                if exc.response.get("Error", {}).get("Code") == "ConditionalCheckFailedException":
                    return False
                raise StorageError(f"DynamoDB publication claim failed: {exc}") from exc
        records = _read_local("proposals")
        for record in records:
            if record.get("id") == proposal_id:
                if record.get("status") != "approved" or record.get("delivery_state") is not None:
                    return False
                record["delivery_state"] = "publishing"
                _write_local("proposals", records)
                return True
        return False

    # --- Thresholds ---

    def get_thresholds(self) -> dict[str, float]:
        """Return {spot_id: threshold_mm_hr} — calibrated values if present."""
        if self._use_dynamodb:
            resp = self._table_thresholds.scan()
            items = resp.get("Items", [])
            return {item["spot_id"]: float(item["threshold_mm_hr"]) for item in items}
        else:
            records = _read_local("thresholds")
            return {r["spot_id"]: float(r["threshold_mm_hr"]) for r in records}

    def set_threshold(self, spot_id: str, threshold: float, original: float) -> None:
        if self._use_dynamodb:
            self._table_thresholds.put_item(Item={
                "spot_id": spot_id,
                "threshold_mm_hr": Decimal(str(threshold)),
                "original_threshold_mm_hr": Decimal(str(original)),
                "updated_at": _now_iso(),
            })
        else:
            records = _read_local("thresholds")
            records = [r for r in records if r.get("spot_id") != spot_id]
            records.append({
                "spot_id": spot_id,
                "threshold_mm_hr": threshold,
                "original_threshold_mm_hr": original,
                "updated_at": _now_iso(),
            })
            _write_local("thresholds", records)

    # --- Verification history ---

    def save_verification_records(self, records: list[dict]) -> None:
        if self._use_dynamodb:
            for rec in records:
                self._table_history.put_item(Item=_ddb_safe(rec))
        else:
            existing = _read_local("history")
            indexed = {(r.get("proposal_id"), r.get("spot_id")): i for i, r in enumerate(existing)}
            for rec in records:
                key = (rec.get("proposal_id"), rec.get("spot_id"))
                if key in indexed:
                    existing[indexed[key]] = rec
                else:
                    indexed[key] = len(existing)
                    existing.append(rec)
            _write_local("history", existing)

    def list_history(self) -> list[dict]:
        if self._use_dynamodb:
            resp = self._table_history.scan()
            records = resp.get("Items", [])
            while "LastEvaluatedKey" in resp:
                resp = self._table_history.scan(ExclusiveStartKey=resp["LastEvaluatedKey"])
                records.extend(resp.get("Items", []))
        else:
            records = _read_local("history")
        records.sort(key=lambda x: x.get("verified_at", ""), reverse=True)
        return records

    def has_verification(self, proposal_id: str) -> bool:
        """Check if verification already exists for this proposal (idempotency)."""
        records = self.list_history()
        matching = [r for r in records if r.get("proposal_id") == proposal_id]
        if matching:
            return all(r.get("outcome") != "pending" for r in matching)
        # Also check DynamoDB if applicable — but history is local, so this covers it
        return False

    def save_local_subscription(self, email: str) -> None:
        if self._use_dynamodb:
            return
        subscriptions = _read_local("subscriptions")
        normalized = email.casefold()
        if not any(item.get("email", "").casefold() == normalized for item in subscriptions):
            subscriptions.append({"email": email, "created_at": _now_iso(), "delivery_state": "local-skipped"})
            _write_local("subscriptions", subscriptions)


# Singleton accessor
_storage: Optional[Storage] = None


def get_storage() -> Storage:
    global _storage
    if _storage is None:
        _storage = Storage()
    return _storage


def reset_storage() -> None:
    """For testing — reset singleton."""
    global _storage
    _storage = None
