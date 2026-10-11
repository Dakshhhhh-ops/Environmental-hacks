"""SNS service — publish alerts with local-skipped fallback."""
from __future__ import annotations

import os
from typing import Optional


class SnsError(Exception):
    pass


def _get_topic_arn() -> Optional[str]:
    arn = os.environ.get("SNS_TOPIC_ARN", "").strip()
    return arn if arn else None


async def publish_alert(topic_arn: str, subject: str, message: str) -> str:
    """Publish to SNS. Returns message ID. Raises SnsError on failure."""
    import asyncio
    import boto3
    from botocore.config import Config

    def _publish() -> str:
        client = boto3.client("sns", region_name=os.environ.get("AWS_REGION", "us-east-1"),
                              config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}))
        resp = client.publish(TopicArn=topic_arn, Subject=subject[:100], Message=message)
        return resp.get("MessageId", "")

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _publish)


async def subscribe_email(topic_arn: str, email: str) -> Optional[str]:
    """Subscribe email to SNS topic. Returns subscription ARN (may be pending confirmation)."""
    import asyncio
    import boto3
    from botocore.config import Config

    def _sub() -> Optional[str]:
        client = boto3.client("sns", region_name=os.environ.get("AWS_REGION", "us-east-1"),
                              config=Config(connect_timeout=5, read_timeout=10, retries={"max_attempts": 1}))
        resp = client.subscribe(TopicArn=topic_arn, Protocol="email", Endpoint=email)
        return resp.get("SubscriptionArn")

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sub)


def is_sns_configured() -> bool:
    return bool(_get_topic_arn())


def get_sns_topic_arn() -> Optional[str]:
    return _get_topic_arn()
