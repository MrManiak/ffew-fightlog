"""Analysis for views: load a fight's packed store, map participants, cache results."""

import hashlib
import json
from array import array
from collections import OrderedDict
from threading import Lock

from django.core.cache import cache
from django.db.models import Count

from fightlog import app_settings
from fightlog.analysis.aggregate import Perspective, aggregate, drilldown, pilot_breakdown
from fightlog.analysis.identity import display_name
from fightlog.analysis.store import FIGHT_COLUMNS, EventStore
from fightlog.models import CombatLog, Fight, Participant, Side

_LRU: "OrderedDict[tuple, EventStore]" = OrderedDict()
_LRU_SIZE = 4
_LRU_LOCK = Lock()


def load_fight_store(fight: Fight) -> EventStore | None:
    key = (fight.pk, fight.built_version)
    with _LRU_LOCK:
        if key in _LRU:
            _LRU.move_to_end(key)
            return _LRU[key]
    data = getattr(fight, "data", None) if hasattr(fight, "data") else None
    if data is None:
        return None
    tables = data.actors or {}
    store = EventStore(FIGHT_COLUMNS, tables.get("actors", []), tables.get("modules", []))
    for chunk in data.chunks.order_by("seq"):
        part = EventStore.decode(chunk.store)
        for name in store.names:
            store.data[name].extend(part.data[name])
    with _LRU_LOCK:
        _LRU[key] = store
        while len(_LRU) > _LRU_SIZE:
            _LRU.popitem(last=False)
    return store


def participants_map(fight: Fight) -> dict[str, dict]:
    """Actor key → display info, with ship-only actors aliased to their attributed pilot."""
    rows = list(fight.participants.select_related("side", "attributed_to"))
    info: dict[str, dict] = {}
    for p in rows:
        info[p.key] = {
            "key": p.key,
            "name": p.name,
            "kind": p.kind,
            "side": p.side_id,
            "side_name": p.side.name if p.side else "Unknown",
            "has_log": p.has_log,
            "ships": p.ships,
            "corp": p.corporation_ticker,
            "alliance": p.alliance_ticker,
            "character_id": p.character_id,
            "hint": p.hint,
            "party": True,
            "attributed": bool(p.attributed_to_id),
        }
    for p in rows:
        if p.attributed_to_id:
            info[p.key]["alias"] = p.attributed_to.key
    data = getattr(fight, "data", None) if hasattr(fight, "data") else None
    if data:
        for actor in (data.actors or {}).get("actors", []):
            if actor["key"] not in info:
                info[actor["key"]] = {
                    "key": actor["key"],
                    "name": display_name(actor),
                    "kind": actor.get("kind"),
                    "side": None,
                    "party": False,
                }
    for log_id, listener in (
        CombatLog.objects.filter(engagements__fight=fight).values_list("pk", "listener_name").distinct()
    ):
        info[f"__log:{log_id}"] = {"key": f"p:{listener}", "party": False}
    return info


def fight_logs(fight: Fight) -> list[CombatLog]:
    return list(
        CombatLog.objects.filter(engagements__fight=fight).distinct().defer("raw_gz").order_by("listener_name", "pk")
    )


def default_active_logs(fight: Fight) -> set[int]:
    return {log.pk for log in fight_logs(fight) if not log.excluded}


def parse_logs_param(fight: Fight, value: str | None) -> set[int]:
    all_logs = {log.pk for log in fight_logs(fight)}
    if not value:
        return default_active_logs(fight)
    wanted = set()
    for part in value.split(","):
        part = part.strip()
        if part.isdigit() and int(part) in all_logs:
            wanted.add(int(part))
    return wanted


def kills_for(fight: Fight) -> list[dict]:
    out = []
    for link in fight.fight_killmails.select_related("killmail", "victim_participant"):
        km = link.killmail
        out.append(
            {
                "id": km.id,
                "time": int(km.time.timestamp()),
                "role": link.role,
                "victim_key": link.victim_participant.key if link.victim_participant else None,
                "victim": link.victim_participant.name if link.victim_participant else None,
                "side": link.victim_participant.side_id if link.victim_participant else None,
                "ship_type_id": km.victim_ship_type_id,
                "value": km.total_value,
                "system_id": km.solar_system_id,
                "url": km.zkb_url,
            }
        )
    out.sort(key=lambda k: k["time"])
    return out


def default_side(fight: Fight) -> Side | None:
    """The side the fight is seen from by default: ours, or the largest other side when none of ours took part."""
    sides = list(fight.sides.annotate(size=Count("participants")))
    friendly = next((s for s in sides if s.order == 0), None)
    if friendly is not None and friendly.size:
        return friendly
    return next((s for s in sides if s.order > 0 and s.size), friendly)


def _cache_key(fight: Fight, *parts) -> str:
    digest = hashlib.sha1(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]
    return f"fightlog:agg:{fight.pk}:{fight.built_version}:{digest}"


def analysis(
    fight: Fight, active_logs: set[int], perspective: str | None, bucket: int, include_non_ships: bool
) -> dict:
    side = default_side(fight)
    persp = Perspective.parse(perspective, str(side.pk) if side else "0")
    key = _cache_key(fight, sorted(active_logs), str(persp), bucket, include_non_ships)
    cached = cache.get(key)
    if cached is not None:
        return cached
    store = load_fight_store(fight)
    if store is None:
        return {"empty": True}
    participants = participants_map(fight)
    kills = kills_for(fight)
    result = aggregate(store, participants, active_logs, persp, bucket, include_non_ships, kills)
    result["kills"] = kills
    result["sides"] = [
        {"id": s.pk, "name": s.name, "order": s.order, "friendly": s.is_friendly} for s in fight.sides.all()
    ]
    cache.set(key, result, app_settings.FIGHTLOG_ANALYSIS_CACHE_SECONDS)
    return result


def events(fight: Fight, active_logs: set[int], start: int, end: int, key: str | None, kinds, limit: int, offset: int):
    store = load_fight_store(fight)
    if store is None:
        return {"total": 0, "events": []}
    names = {log.pk: log.listener_name for log in fight_logs(fight)}
    return drilldown(store, participants_map(fight), active_logs, start, end, key, kinds, limit, offset, names)


def pilot(fight: Fight, active_logs: set[int], key: str) -> dict:
    store = load_fight_store(fight)
    if store is None:
        return {}
    participants = participants_map(fight)
    result = pilot_breakdown(store, participants, active_logs, key)
    p = Participant.objects.filter(fight=fight, key=key).select_related("side").first()
    result["participant"] = participants.get(key, {"key": key})
    if p:
        result["participant"]["ship_rows"] = [
            {"ship": s.type_name, "source": s.get_source_display(), "first": s.first_seen, "last": s.last_seen}
            for s in p.ship_rows.order_by("first_seen")
        ]
    return result


def clear_lru() -> None:
    with _LRU_LOCK:
        _LRU.clear()


__all__ = ["analysis", "array", "events", "load_fight_store", "participants_map", "pilot"]
