"""Split a log's PvP events into engagements (technical plan §4.4)."""

from fightlog.parser import ActorKind, Event, Flag, ParsedLog

from .identity import actor_dict, actor_key, polarity
from .store import EventStore


def is_pvp(event: Event) -> bool:
    if event.flags & Flag.OBSERVED:
        return event.src.is_player and event.tgt.is_player
    other = event.tgt if event.src.kind == ActorKind.LISTENER else event.src
    return other.kind != ActorKind.LISTENER and other.is_player


def split_engagements(events: list[Event], gap_seconds: int, min_events: int) -> list[list[Event]]:
    pvp = [e for e in events if is_pvp(e)]
    groups: list[list[Event]] = []
    current: list[Event] = []
    for event in pvp:
        if current and event.t - current[-1].t > gap_seconds:
            groups.append(current)
            current = []
        current.append(event)
    if current:
        groups.append(current)
    return [g for g in groups if len(g) >= min_events]


def build_store(events: list[Event], log_id: int, listener_name: str | None) -> EventStore:
    store = EventStore()
    hulls: dict[tuple[str, str], list[int]] = {}
    for event in events:
        pol = polarity(event)
        src_key = actor_key(event.src, log_id, pol, listener_name)
        tgt_key = actor_key(event.tgt, log_id, pol, listener_name)
        for actor, key in ((event.src, src_key), (event.tgt, tgt_key)):
            if actor.kind == ActorKind.PILOT and actor.ship:
                span = hulls.setdefault((key, actor.ship), [event.t, event.t])
                span[1] = event.t
        store.append(
            t=event.t,
            kind=int(event.kind),
            src=store.actor_id(actor_dict(event.src, src_key, listener_name)),
            tgt=store.actor_id(actor_dict(event.tgt, tgt_key, listener_name)),
            module=store.module_id(event.module),
            amount=event.amount,
            quality=int(event.quality),
            flags=int(event.flags),
            line_no=event.line_no,
        )
    # Hull observations: (pilot key, ship, first seen, last seen) from labels in this log.
    store.meta["hulls"] = [[key, ship, span[0], span[1]] for (key, ship), span in sorted(hulls.items())]
    return store


def pilot_keys(store: EventStore) -> list[str]:
    """Keys of identified players in an engagement, used to cluster logs into fights."""
    return sorted({a["key"] for a in store.actors if a["key"].startswith("p:")})


def engagements_for_log(parsed: ParsedLog, log_id: int, gap_seconds: int, min_events: int) -> list[EventStore]:
    return [
        build_store(group, log_id, parsed.listener)
        for group in split_engagements(parsed.events, gap_seconds, min_events)
    ]
