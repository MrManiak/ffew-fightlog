"""zKillboard linking (technical plan §6)."""

import math
import time
from collections import Counter
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

import requests

from django.core.cache import cache
from django.db import transaction
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from allianceauth.services.hooks import get_extension_logger

from fightlog import __url__, __version__, app_settings
from fightlog.catalog import load_catalog
from fightlog.locks import cache_lock
from fightlog.models import Fight, FightKillmail, Killmail, Participant

from . import resolve

logger = get_extension_logger(__name__)

ZKB_API = "https://zkillboard.com/api/"
USER_AGENT = f"ffew-fightlog/{__version__} (+{__url__})"
LAST_REQUEST_KEY = "fightlog-zkb-last-request"
RETRY_AT_KEY = "fightlog-zkb-retry-at"
PAGE_SIZE = 200
MAX_PAGES = 5
CORE_BEFORE = timedelta(minutes=2)
CORE_AFTER = timedelta(minutes=5)


class ZkbRateLimited(RuntimeError):
    def __init__(self, retry_at: float):
        super().__init__(f"zKillboard rate limit until {retry_at}")
        self.retry_at = retry_at


def zkb_get(path: str) -> list[dict]:
    """GET one zKillboard API path, spacing requests across all workers."""
    retry_at = cache.get(RETRY_AT_KEY)
    if retry_at and retry_at > time.time():
        raise ZkbRateLimited(retry_at)
    with cache_lock("zkb", timeout=60, wait=120):
        last = cache.get(LAST_REQUEST_KEY) or 0.0
        wait = last + app_settings.FIGHTLOG_ZKB_REQUEST_DELAY_MS / 1000.0 - time.time()
        if wait > 0:
            time.sleep(wait)
        response = requests.get(
            ZKB_API + path.strip("/") + "/",
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip", "Accept": "application/json"},
            timeout=(5, 30),
        )
        cache.set(LAST_REQUEST_KEY, time.time(), 3600)
    if response.status_code == 429:
        seconds = int(response.headers.get("Retry-After", "10") or 10)
        retry_at = time.time() + seconds
        cache.set(RETRY_AT_KEY, retry_at, seconds + 5)
        raise ZkbRateLimited(retry_at)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, list) else []


def _km_time(km: dict) -> datetime | None:
    value = km.get("killmail_time")
    return parse_datetime(value) if value else None


def fetch_entity_kills(entity: str, entity_id: int, start: datetime, now: datetime) -> list[dict]:
    """Killmails involving an entity from shortly before ``start`` until now, paging until older than that."""
    out = []
    oldest_wanted = start - timedelta(minutes=app_settings.FIGHTLOG_ZKB_EXPANSION_MINUTES * 2 + 15)
    if now - oldest_wanted <= timedelta(days=7):
        seconds = (now - oldest_wanted).total_seconds()
        past = min(604800, max(3600, int(math.ceil(seconds / 3600.0)) * 3600))
        base = f"{entity}ID/{entity_id}/pastSeconds/{past}"
    else:
        base = f"{entity}ID/{entity_id}/year/{start.year}/month/{start.month}"
    for page in range(1, MAX_PAGES + 1):
        rows = zkb_get(f"{base}/page/{page}")
        out.extend(rows)
        times = [t for t in (_km_time(r) for r in rows) if t]
        if len(rows) < PAGE_SIZE or (times and min(times) < oldest_wanted):
            break
    return out


def fetch_system_kills(system_id: int, start: datetime, now: datetime) -> list[dict]:
    return fetch_entity_kills("solarSystem", system_id, start, now)


def _aoe_suspect(attacker: dict) -> bool:
    weapon, ship = attacker.get("weapon_type_id"), attacker.get("ship_type_id")
    if weapon and ship and weapon == ship:
        return True
    info = load_catalog().by_id(weapon)
    return bool(info and load_catalog().is_aoe(info.name))


def save_killmail(km: dict) -> Killmail | None:
    killmail_id = km.get("killmail_id")
    when = _km_time(km)
    if not killmail_id or not when:
        return None
    victim = km.get("victim") or {}
    zkb = km.get("zkb") or {}
    attackers = [
        {
            "character_id": a.get("character_id"),
            "corporation_id": a.get("corporation_id"),
            "alliance_id": a.get("alliance_id"),
            "ship_type_id": a.get("ship_type_id"),
            "weapon_type_id": a.get("weapon_type_id"),
            "damage_done": a.get("damage_done", 0),
            "final_blow": bool(a.get("final_blow")),
            "aoe_suspect": _aoe_suspect(a),
        }
        for a in km.get("attackers") or []
    ]
    obj, _ = Killmail.objects.update_or_create(
        id=killmail_id,
        defaults={
            "hash": zkb.get("hash", "")[:64],
            "time": when,
            "solar_system_id": km.get("solar_system_id"),
            "victim_character_id": victim.get("character_id"),
            "victim_corporation_id": victim.get("corporation_id"),
            "victim_alliance_id": victim.get("alliance_id"),
            "victim_ship_type_id": victim.get("ship_type_id"),
            "total_value": float(zkb.get("totalValue") or 0.0),
            "is_npc": bool(zkb.get("npc")),
            "is_solo": bool(zkb.get("solo")),
            "attackers": attackers,
        },
    )
    return obj


def _characters(km: dict) -> set[int]:
    ids = {a.get("character_id") for a in km.get("attackers") or []}
    ids.add((km.get("victim") or {}).get("character_id"))
    ids.discard(None)
    return ids


def select_kills(kms: list[dict], participant_ids: set[int], start: datetime, end: datetime):
    """(core, related, system_id) per technical plan §6 steps 3–5."""
    by_id = {km["killmail_id"]: km for km in kms if km.get("killmail_id") and _km_time(km)}
    core_window = (start - CORE_BEFORE, end + CORE_AFTER)
    core = {
        kid: km
        for kid, km in by_id.items()
        if core_window[0] <= _km_time(km) <= core_window[1] and len(_characters(km) & participant_ids) >= 2
    }
    systems = Counter(km.get("solar_system_id") for km in core.values() if km.get("solar_system_id"))
    system_id = systems.most_common(1)[0][0] if systems else None
    related = {kid: km for kid, km in core.items() if system_id and km.get("solar_system_id") != system_id}
    for kid in related:
        core.pop(kid)
    window = list(core_window)
    margin = timedelta(minutes=app_settings.FIGHTLOG_ZKB_EXPANSION_MINUTES)
    for _ in range(app_settings.FIGHTLOG_ZKB_EXPANSION_ROUNDS):
        added = False
        for kid, km in by_id.items():
            if kid in core or kid in related:
                continue
            when = _km_time(km)
            if window[0] - margin <= when <= window[1] + margin and len(_characters(km) & participant_ids) >= 2:
                related[kid] = km
                window = [min(window[0], when), max(window[1], when)]
                added = True
        if not added:
            break
    return core, related, system_id


def _seed_entities(fight: Fight) -> list[tuple[str, int]]:
    seeds: list[tuple[str, int]] = []
    friendly = fight.participants.filter(side__order=0, kind=Participant.Kind.PILOT)
    for alliance_id, _count in Counter(
        friendly.exclude(alliance_id__isnull=True).values_list("alliance_id", flat=True)
    ).most_common(2):
        seeds.append(("alliance", alliance_id))
    for corp_id, _count in Counter(
        friendly.exclude(corporation_id__isnull=True).values_list("corporation_id", flat=True)
    ).most_common(2):
        seeds.append(("corporation", corp_id))
    pilots = fight.participants.filter(kind=Participant.Kind.PILOT).exclude(character_id__isnull=True)
    for character_id in pilots.order_by("-has_log", "name").values_list("character_id", flat=True):
        seeds.append(("character", character_id))
    unique = list(dict.fromkeys(seeds))
    return unique[: app_settings.FIGHTLOG_ZKB_MAX_ENTITY_QUERIES]


def _in_fight(km: dict, fight: Fight, system_id: int, participant_ids: set[int]) -> bool:
    """A system kill inside the fight's core window that involves at least one participant."""
    when = _km_time(km)
    return bool(
        when
        and fight.start_at - CORE_BEFORE <= when <= fight.end_at + CORE_AFTER
        and km.get("solar_system_id") == system_id
        and _characters(km) & participant_ids
    )


def link_fight(fight: Fight, now: datetime | None = None) -> int:
    """Find and store this fight's killmails. Returns the number of new links."""
    now = now or timezone.now()
    participant_ids = set(fight.participants.exclude(character_id__isnull=True).values_list("character_id", flat=True))
    kms: dict[int, dict] = {}
    for entity, entity_id in _seed_entities(fight):
        for km in fetch_entity_kills(entity, entity_id, fight.start_at, now):
            if km.get("killmail_id"):
                kms[km["killmail_id"]] = km
    core, related, system_id = select_kills(list(kms.values()), participant_ids, fight.start_at, fight.end_at)
    if system_id:
        for km in fetch_system_kills(system_id, fight.start_at, now):
            kid = km.get("killmail_id")
            if kid and kid not in core and kid not in related and _in_fight(km, fight, system_id, participant_ids):
                core[kid] = km
    new_links = 0
    with transaction.atomic():
        existing = set(fight.fight_killmails.values_list("killmail_id", flat=True))
        for role, group in (("core", core), ("related", related)):
            for kid, km in group.items():
                obj = save_killmail(km)
                if obj is None:
                    continue
                link, created = FightKillmail.objects.get_or_create(fight=fight, killmail=obj, defaults={"role": role})
                if not created and link.role != role:
                    link.role = role
                    link.save(update_fields=["role"])
                new_links += int(created and kid not in existing)
        windows = [_km_time(km) for km in list(core.values()) + list(related.values())]
        fight.zkb_window_start = min(windows) if windows else None
        fight.zkb_window_end = max(windows) if windows else None
        fight.zkb_checked_at = now
        if system_id and fight.system_source != Fight.SystemSource.MANUAL:
            system = resolve.solar_system(system_id)
            if system:
                fight.system, fight.system_source = system, Fight.SystemSource.ZKB
        fight.save()
    return new_links


def epoch_to_dt(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=dt_timezone.utc)
