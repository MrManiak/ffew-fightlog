"""Who was "Unknown Loki"? (technical plan §5.2, ship-type-only actors).

In order:
1. peer-log match: dedupe matched the event against the sender's own log
2. elimination: exactly one pilot on the implied side flew that hull, and no logged
   pilot who could have sent it shows the matching event in their own log
3. otherwise the actor stays unattributed
Curator overrides win over all of these.
"""

from collections import Counter
from dataclasses import dataclass

from .hulls import HullTimeline
from .identity import FRIENDLY_ACT, HOSTILE_ACT


@dataclass
class ShipOnlyActor:
    key: str  # s:<log>:<polarity>:<label>
    log_id: int
    polarity: str
    ship: str
    times: list[int]
    listener_key: str  # the log's listener, who the actor interacted with


def parse_ship_only_key(key: str) -> tuple[int, str, str] | None:
    if not key.startswith("s:"):
        return None
    try:
        _, log_id, pol, label = key.split(":", 3)
        return int(log_id), pol, label
    except ValueError:
        return None


def attribute(
    actors: list[ShipOnlyActor],
    peer_matches: dict[str, Counter],
    side_of: dict[str, int | None],
    pilots: set[str],
    hulls: HullTimeline,
    log_windows: dict[str, tuple[int, int]],
    overrides: dict[str, str] | None = None,
) -> dict[str, tuple[str, str]]:
    """Map ship-only key → (pilot key, source)."""
    overrides = overrides or {}
    result: dict[str, tuple[str, str]] = {}
    for actor in actors:
        if actor.key in overrides:
            result[actor.key] = (overrides[actor.key], "manual")
            continue
        matches = peer_matches.get(actor.key)
        if matches:
            pilot, _ = matches.most_common(1)[0]
            result[actor.key] = (pilot, "peer_log")
            continue
        listener_side = side_of.get(actor.listener_key)
        if listener_side is None or actor.polarity not in (FRIENDLY_ACT, HOSTILE_ACT):
            continue
        candidates = []
        for pilot in pilots:
            if pilot == actor.listener_key:
                continue
            side = side_of.get(pilot)
            if side is None:
                continue
            if actor.polarity == FRIENDLY_ACT and side != listener_side:
                continue
            if actor.polarity == HOSTILE_ACT and side == listener_side:
                continue
            if not hulls.flew(pilot, actor.ship):
                continue
            if not all(hulls.compatible(pilot, actor.ship, t) for t in actor.times[:: max(1, len(actor.times) // 20)]):
                continue
            window = log_windows.get(pilot)
            if window and any(window[0] <= t <= window[1] for t in actor.times):
                # This pilot uploaded a log covering the event; had they sent it,
                # dedupe would have matched it against their own log.
                continue
            candidates.append(pilot)
        if len(candidates) == 1:
            result[actor.key] = (candidates[0], "elimination")
    return result
