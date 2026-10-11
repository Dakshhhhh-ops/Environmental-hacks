"""Ask Sentinel — a dispatcher asks a question in plain words.

A Strands agent decides which read-only tool answers it (network, zone, spot, or route) and with
what arguments. The answer shown to the dispatcher is composed by code from the tool's result,
not from the model's own sentences: in testing a small local model picked the right tool every
time but described the numbers loosely, which is not acceptable for dispatch decisions.
"""
from __future__ import annotations

import asyncio
import difflib
from typing import Optional

from .agent import _build_model, agent_model_id, agent_provider
from .routing import PLACES, check_route
from .spots_loader import load_spots

SYSTEM_PROMPT = """You route questions from a Delhi transport dispatcher about rain risk at underpasses and low points.
Call exactly one tool that fits the question, then reply with the single word DONE.
- network_status: overall picture, counts, worst spots, or anything general.
- zone_status: one part of Delhi (Central, North, South, East, West, South East, South West, North West).
- spot_status: one named underpass, road or low point.
- route_check: a trip between two named places."""

LEVEL_WORD = {"red": "HIGH", "amber": "WATCH", "green": "LOW"}


def _match(query: str, names: list[str]) -> Optional[str]:
    """Best match of a spoken name against known names, or None."""
    q = query.strip().lower()
    if not q:
        return None
    lowered = {n.lower(): n for n in names}
    for low, original in lowered.items():
        if q == low or q in low or low in q:
            return original
    words = [w for w in q.replace(",", " ").replace("(", " ").replace(")", " ").split() if len(w) > 2 and w not in ("the", "road", "delhi", "station", "hospital", "underpass")]
    scored = sorted(((sum(w in low for w in words), original) for low, original in lowered.items()), reverse=True)
    if scored and scored[0][0] > 0 and (len(scored) == 1 or scored[0][0] > scored[1][0]):
        return scored[0][1]
    close = difflib.get_close_matches(q, list(lowered), n=1, cutoff=0.6)
    return lowered[close[0]] if close else None


def _available(a: Optional[dict]) -> bool:
    return bool(a) and a.get("data_available", True)


def _spot_sentence(spot: dict, a: Optional[dict]) -> str:
    if not _available(a):
        return f"{spot['name']} has not been assessed in this run."
    pct = round(a["risk_ratio"] * 100)
    level = LEVEL_WORD[a["risk_level"]]
    if a.get("trigger") == "recorded_rain":
        return (f"{spot['name']} is {level}: {a.get('recent_peak_mm_hr')} mm/hr of rain is already recorded there, "
                f"{pct}% of its {a['threshold_mm_hr']} mm/hr limit.")
    return (f"{spot['name']} is {level}: the forecast peak is {a['peak_precipitation_mm_hr']} mm/hr, "
            f"{pct}% of its {a['threshold_mm_hr']} mm/hr limit.")


def _advice(a: Optional[dict]) -> str:
    if not _available(a) or a["risk_level"] == "green":
        return "It is below watch level. That is a statement about rain, not a check of the road."
    now = a.get("trigger") == "recorded_rain"
    if a["risk_level"] == "red":
        return "Hold trips through it now." if now else "Plan an alternative and check conditions before each trip."
    return "Check conditions before each trip."


def summarize(assessments: list[dict]) -> str:
    """Computed status of the whole network."""
    avail = [a for a in assessments if _available(a)]
    if not avail:
        return "No assessment has been run yet, so there is nothing to report. Run an assessment first."
    red = [a for a in avail if a["risk_level"] == "red"]
    amber = [a for a in avail if a["risk_level"] == "amber"]
    if not red and not amber:
        return f"None of the {len(avail)} assessed low points is at watch level."
    now = [a for a in red + amber if a.get("trigger") == "recorded_rain"]
    worst = sorted(red + amber, key=lambda a: -a["risk_ratio"])[:3]
    text = (f"{len(red)} low points are HIGH and {len(amber)} are on WATCH, out of {len(avail)} assessed. "
            f"Worst: " + ", ".join(f"{a['spot_name']} ({round(a['risk_ratio'] * 100)}% of its limit)" for a in worst) + ".")
    if now:
        text += f" {len(now)} of these were flagged by rain already falling, not by the forecast."
    return text


class Toolbox:
    """The read-only lookups the agent can choose from. Each returns the sentence a dispatcher will read."""

    def __init__(self, assessments: list[dict]):
        self.spots = load_spots()
        self.assessments = assessments
        self.by_id = {a["spot_id"]: a for a in assessments}
        self.risk = {a["spot_id"]: a["risk_level"] for a in assessments if _available(a)}

    def network(self) -> dict:
        return {"answer": summarize(self.assessments)}

    def zone(self, zone: str) -> dict:
        zones = sorted({s.get("zone") for s in self.spots if s.get("zone")})
        name = _match(zone, zones)
        if not name:
            return {"answer": f"There is no zone called \"{zone}\". The zones are: {', '.join(zones)}."}
        groups: dict[str, list[str]] = {"HIGH": [], "WATCH": [], "LOW": []}
        for s in self.spots:
            a = self.by_id.get(s["id"])
            if s.get("zone") == name and _available(a):
                groups[LEVEL_WORD[a["risk_level"]]].append(s["name"] + (" (rain now)" if a.get("trigger") == "recorded_rain" else ""))
        parts = [f"{level}: {', '.join(names)}" for level, names in groups.items() if names]
        return {"answer": f"{name} Delhi. " + (". ".join(parts) + "." if parts else "Nothing there has been assessed in this run.")}

    def spot(self, name: str) -> dict:
        match = _match(name, [s["name"] for s in self.spots])
        if not match:
            return {"answer": f"\"{name}\" is not one of the {len(self.spots)} monitored low points, so there is no reading for it. "
                              f"Ask about a zone, or pick a location from the list below the map."}
        spot = next(s for s in self.spots if s["name"] == match)
        a = self.by_id.get(spot["id"])
        return {"answer": f"{_spot_sentence(spot, a)} {_advice(a)}", "spot_id": spot["id"]}

    def route(self, origin: str, destination: str) -> dict:
        names = [p["name"] for p in PLACES]
        a, b = _match(origin, names), _match(destination, names)
        if not a or not b or a == b:
            unknown = [q for q, m in ((origin, a), (destination, b)) if not m]
            return {"answer": (f"I can only check routes between the listed places, and could not match {' or '.join(unknown) if unknown else 'two different places'}. "
                               f"Known places: {', '.join(names)}.")}
        pa, pb = (next(p for p in PLACES if p["name"] == n) for n in (a, b))
        try:
            r = asyncio.run(check_route((pa["lat"], pa["lon"]), (pb["lat"], pb["lon"]), self.spots, self.risk))
        except Exception as exc:
            return {"answer": f"The routing service did not respond, so the route from {a} to {b} could not be checked. ({exc})"}
        rec, fast = r["routes"][r["recommended_index"]], r["routes"][r["fastest_index"]]
        hits = lambda route: ", ".join(f"{h['name']} ({LEVEL_WORD[h['risk_level']]})" for h in route["hits"])
        if not self.risk:
            answer = f"{a} to {b}: {fast['duration_min']} min by the fastest route. No assessment has been run, so nothing is flagged yet."
        elif r["all_routes_flagged"]:
            answer = (f"{a} to {b}: every driving route passes a flagged low point. The least exposed takes {rec['duration_min']} min "
                      f"and passes {hits(rec)}. Hold the trip or confirm conditions there first.")
        elif fast["score"] > 0:
            answer = (f"{a} to {b}: do not use the fastest route, it passes {hits(fast)}. The suggested route takes {rec['duration_min']} min "
                      f"({r['extra_minutes']} min longer) and is clear of flagged spots.")
        else:
            answer = (f"{a} to {b}: the fastest route takes {fast['duration_min']} min and is clear of flagged spots. "
                      f"Clear of flagged spots is not a guarantee the road is dry.")
        return {"answer": answer, "route": {"from_id": pa["id"], "to_id": pb["id"]}}


def _run(question: str, assessments: list[dict]) -> tuple[Optional[dict], list[str]]:
    from strands import Agent, tool

    box = Toolbox(assessments)
    calls: list[str] = []
    results: list[dict] = []

    def record(label: str, result: dict) -> str:
        calls.append(label)
        results.append(result)
        return result["answer"]

    @tool
    def network_status() -> str:
        """Overall picture: how many low points are HIGH, WATCH and LOW, and the worst ones."""
        return record("network_status", box.network())

    @tool
    def zone_status(zone: str) -> str:
        """Status of every low point in one zone of Delhi.

        Args:
            zone: Central, North, South, East, West, South East, South West or North West.
        """
        return record(f"zone_status({zone})", box.zone(zone))

    @tool
    def spot_status(name: str) -> str:
        """Status of one named underpass, road or low point.

        Args:
            name: The name as the dispatcher said it, for example "Minto Road" or "Moolchand underpass".
        """
        return record(f"spot_status({name})", box.spot(name))

    @tool
    def route_check(origin: str, destination: str) -> str:
        """Check whether driving routes between two named places pass flagged low points.

        Args:
            origin: Where the trip starts, for example "AIIMS Hospital".
            destination: Where the trip ends, for example "ISBT Kashmere Gate".
        """
        return record(f"route_check({origin} -> {destination})", box.route(origin, destination))

    agent = Agent(model=_build_model(), tools=[network_status, zone_status, spot_status, route_check],
                  system_prompt=SYSTEM_PROMPT, callback_handler=None)
    agent(question)
    if not results:
        # Small models sometimes answer in prose. Ask once more, plainly.
        agent("You did not call a tool. Call the one tool that fits my question now.")
    return (results[0] if results else None), calls


def keyword_route(question: str, box: Toolbox) -> tuple[dict, str]:
    """Pick a lookup from the words in the question. Used when the agent is unavailable or does not choose."""
    q = question.lower()
    places = [p["name"] for p in PLACES]
    if " to " in q:
        left, right = q.split(" to ", 1)
        origin = _match(left.split(" from ")[-1], places)
        destination = _match(right, places)
        if origin and destination and origin != destination:
            return box.route(origin, destination), f"route_check({origin} -> {destination})"
    for spot in box.spots:
        core = spot["name"].lower().split(" (")[0].replace(" underpass", "").replace(" railway bridge", "").replace(" crossing", "")
        if core in q:
            return box.spot(spot["name"]), f"spot_status({spot['name']})"
    for zone in sorted({s.get("zone") for s in box.spots if s.get("zone")}, key=len, reverse=True):
        if zone.lower() in q:
            return box.zone(zone), f"zone_status({zone})"
    return box.network(), "network_status"


async def ask(question: str, assessments: list[dict], timeout: float = 60) -> dict:
    """Answer a dispatcher question. The agent chooses the lookup; code writes the answer."""
    trace = {"framework": "Strands Agents SDK", "provider": agent_provider(), "model": agent_model_id(), "tools_called": []}
    if not assessments:
        return {"answer": summarize([]), "routed_by": "none", "reason": "no assessment has been run", "agent_trace": trace}
    reason = None
    try:
        loop = asyncio.get_running_loop()
        result, calls = await asyncio.wait_for(loop.run_in_executor(None, _run, question, assessments), timeout=timeout)
        trace["tools_called"] = calls
        if result:
            return {**result, "routed_by": "agent", "reason": None, "agent_trace": trace}
        reason = "the agent did not choose a lookup, so the question was matched by keywords"
    except Exception as exc:
        reason = f"agent unavailable, so the question was matched by keywords: {(str(exc) or type(exc).__name__)[:160]}"
    loop = asyncio.get_running_loop()
    result, label = await loop.run_in_executor(None, keyword_route, question, Toolbox(assessments))
    trace["tools_called"] = [label]
    return {**result, "routed_by": "keywords", "reason": reason, "agent_trace": trace}
