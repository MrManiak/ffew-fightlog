"""Stable keys for actors.

Pilots, NPCs and structures are keyed by name. Ship-type-only actors ("boosted by
Loki") are keyed by the raw log data they came from, ``s:<log>:<polarity>:<label>``,
so no key ever depends on a side guess (technical plan §3).
"""

from fightlog.parser import Actor, ActorKind, Event, Flag, Kind
from fightlog.parser.events import FRIENDLY_KINDS

FRIENDLY_ACT = "friendly_act"
HOSTILE_ACT = "hostile_act"
OBSERVED = "observed"


def polarity(event: Event) -> str:
    if event.flags & Flag.OBSERVED:
        return OBSERVED
    return FRIENDLY_ACT if event.kind in FRIENDLY_KINDS else HOSTILE_ACT


def actor_key(actor: Actor, log_id: int, event_polarity: str, listener_name: str | None = None) -> str:
    kind = actor.kind
    if kind == ActorKind.LISTENER:
        return "p:" + (actor.name or listener_name or "?")
    if kind == ActorKind.PILOT:
        return "p:" + (actor.name or "?")
    if kind == ActorKind.NPC:
        return "n:" + (actor.name or actor.raw)
    if kind == ActorKind.STRUCTURE:
        return "st:" + (actor.name or actor.ship or actor.raw)
    if kind == ActorKind.DRONE:
        if actor.owner:
            return "p:" + actor.owner
        return f"d:{actor.corp or ''}:{actor.name or actor.ship or actor.raw}"
    if kind == ActorKind.CHARGE:
        return f"c:{actor.corp or ''}:{actor.name or actor.raw}"
    if kind == ActorKind.SHIP_ONLY:
        return f"s:{log_id}:{event_polarity}:{actor.raw or actor.ship}"
    if kind == ActorKind.EMPTY:
        return "e:"
    return "u:" + (actor.name or actor.raw or "?")


def actor_dict(actor: Actor, key: str, listener_name: str | None = None) -> dict:
    data = {"key": key, "kind": actor.kind}
    name = actor.name or (listener_name if actor.kind == ActorKind.LISTENER else None)
    for field, value in (
        ("name", name),
        ("ship", actor.ship),
        ("corp", actor.corp),
        ("alliance", actor.alliance),
        ("owner", actor.owner),
        ("raw", actor.raw),
    ):
        if value:
            data[field] = value
    if actor.kind == ActorKind.DRONE and actor.owner:
        # Drone actions count for the owner; the drone type is not the owner's hull.
        data = {"key": key, "kind": ActorKind.PILOT, "name": actor.owner, "raw": actor.raw}
    if actor.kind == ActorKind.LISTENER:
        data["kind"] = ActorKind.PILOT
        data["listener"] = True
    return data


def display_name(actor: dict) -> str:
    if actor.get("kind") == ActorKind.SHIP_ONLY:
        return f"Unknown {actor.get('ship') or actor.get('raw') or 'pilot'}"
    return actor.get("name") or actor.get("ship") or actor.get("raw") or actor["key"]


def is_party(actor: dict) -> bool:
    """Can this actor be on a side?"""
    return actor.get("kind") in (ActorKind.PILOT, ActorKind.STRUCTURE, ActorKind.DRONE, ActorKind.SHIP_ONLY)


__all__ = ["FRIENDLY_ACT", "HOSTILE_ACT", "OBSERVED", "Kind", "actor_dict", "actor_key", "display_name", "polarity"]
