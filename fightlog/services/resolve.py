"""Names, affiliations, tickers and solar systems (technical plan §5.2).

ESI is called through thin wrappers so tests can replace them. Failures are logged
and skipped: a fight still rebuilds from log tickers when ESI is down.
"""

from datetime import timedelta

from django.utils import timezone

from allianceauth.services.hooks import get_extension_logger

from fightlog.catalog import load_catalog
from fightlog.models import Alliance, Corporation, ResolvedName, SolarSystem

logger = get_extension_logger(__name__)

NAME_MAX_AGE = timedelta(days=30)
ORG_MAX_AGE = timedelta(days=7)


def _plain(obj):
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if isinstance(obj, list):
        return [_plain(o) for o in obj]
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    return obj


def _client():
    from fightlog.providers import esi

    return esi.client


# Thin ESI wrappers -------------------------------------------------------------


def esi_universe_ids(names: list[str]) -> dict:
    return _plain(_client().Universe.PostUniverseIds(body=names).result(use_cache=False)) or {}


def esi_universe_names(ids: list[int]) -> list[dict]:
    return _plain(_client().Universe.PostUniverseNames(body=ids).result(use_cache=False)) or []


def esi_affiliations(ids: list[int]) -> list[dict]:
    return _plain(_client().Character.PostCharactersAffiliation(body=ids).result(use_cache=False)) or []


def esi_corporation(corporation_id: int) -> dict:
    return _plain(_client().Corporation.GetCorporationsCorporationId(corporation_id=corporation_id).result())


def esi_alliance(alliance_id: int) -> dict:
    return _plain(_client().Alliance.GetAlliancesAllianceId(alliance_id=alliance_id).result())


def esi_system(system_id: int) -> dict:
    return _plain(_client().Universe.GetUniverseSystemsSystemId(system_id=system_id).result())


def esi_constellation(constellation_id: int) -> dict:
    return _plain(
        _client().Universe.GetUniverseConstellationsConstellationId(constellation_id=constellation_id).result()
    )


def esi_region(region_id: int) -> dict:
    return _plain(_client().Universe.GetUniverseRegionsRegionId(region_id=region_id).result())


# Characters ---------------------------------------------------------------------


def _chunks(items: list, size: int):
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _store_affiliations(rows_by_id: dict[int, ResolvedName]) -> None:
    ids = [cid for cid in rows_by_id if cid]
    for chunk in _chunks(ids, 1000):
        try:
            affiliations = esi_affiliations(chunk)
        except Exception as ex:  # noqa: BLE001 - ESI is optional
            logger.warning("ESI affiliation lookup failed: %s", ex)
            return
        for row in affiliations:
            rn = rows_by_id.get(row.get("character_id"))
            if rn:
                rn.corporation_id = row.get("corporation_id")
                rn.alliance_id = row.get("alliance_id")


def resolve_names(names) -> dict[str, ResolvedName]:
    """Character names → ResolvedName rows (character_id None when no such character)."""
    wanted = {n for n in names if n and len(n) <= 100}
    if not wanted:
        return {}
    now = timezone.now()
    found = {rn.name: rn for rn in ResolvedName.objects.filter(name__in=wanted)}
    stale = [n for n in wanted if n not in found or now - found[n].resolved_at > NAME_MAX_AGE]
    if stale:
        resolved: dict[str, int] = {}
        try:
            for chunk in _chunks(sorted(stale), 500):
                data = esi_universe_ids(chunk)
                lowered = {n.lower(): n for n in chunk}
                for row in data.get("characters") or []:
                    original = lowered.get(str(row["name"]).lower())
                    if original:
                        resolved[original] = row["id"]
        except Exception as ex:  # noqa: BLE001
            logger.warning("ESI name lookup failed: %s", ex)
            return found
        rows: dict[int, ResolvedName] = {}
        for name in stale:
            rn = found.get(name) or ResolvedName(name=name)
            rn.character_id = resolved.get(name)
            rn.resolved_at = now
            found[name] = rn
            if rn.character_id:
                rows[rn.character_id] = rn
        _store_affiliations(rows)
        for name in stale:
            rn = found[name]
            ResolvedName.objects.update_or_create(
                name=name,
                defaults={
                    "character_id": rn.character_id,
                    "corporation_id": rn.corporation_id,
                    "alliance_id": rn.alliance_id,
                    "resolved_at": now,
                },
            )
    return found


def resolve_character_ids(ids) -> dict[int, str]:
    """Character IDs (from killmails) → names, cached as ResolvedName rows."""
    wanted = {int(i) for i in ids if i}
    if not wanted:
        return {}
    known = {rn.character_id: rn.name for rn in ResolvedName.objects.filter(character_id__in=wanted)}
    missing = sorted(wanted - set(known))
    now = timezone.now()
    for chunk in _chunks(missing, 900):
        try:
            rows = esi_universe_names(chunk)
        except Exception as ex:  # noqa: BLE001
            logger.warning("ESI ID lookup failed: %s", ex)
            break
        for row in rows:
            if row.get("category") == "character":
                known[row["id"]] = row["name"]
                ResolvedName.objects.update_or_create(
                    name=row["name"], defaults={"character_id": row["id"], "resolved_at": now}
                )
    return known


# Corporations and alliances -------------------------------------------------------


def corporations(ids) -> dict[int, Corporation]:
    wanted = {int(i) for i in ids if i}
    now = timezone.now()
    found = {c.id: c for c in Corporation.objects.filter(id__in=wanted)}
    for corp_id in sorted(wanted):
        if corp_id in found and now - found[corp_id].fetched_at <= ORG_MAX_AGE:
            continue
        try:
            data = esi_corporation(corp_id)
        except Exception as ex:  # noqa: BLE001
            logger.warning("ESI corporation %s lookup failed: %s", corp_id, ex)
            continue
        found[corp_id], _ = Corporation.objects.update_or_create(
            id=corp_id,
            defaults={
                "ticker": data.get("ticker", "")[:10],
                "name": data.get("name", "")[:255],
                "alliance_id": data.get("alliance_id"),
                "fetched_at": now,
            },
        )
    return found


def alliances(ids) -> dict[int, Alliance]:
    wanted = {int(i) for i in ids if i}
    now = timezone.now()
    found = {a.id: a for a in Alliance.objects.filter(id__in=wanted)}
    for alliance_id in sorted(wanted):
        if alliance_id in found and now - found[alliance_id].fetched_at <= ORG_MAX_AGE:
            continue
        try:
            data = esi_alliance(alliance_id)
        except Exception as ex:  # noqa: BLE001
            logger.warning("ESI alliance %s lookup failed: %s", alliance_id, ex)
            continue
        found[alliance_id], _ = Alliance.objects.update_or_create(
            id=alliance_id,
            defaults={"ticker": data.get("ticker", "")[:10], "name": data.get("name", "")[:255], "fetched_at": now},
        )
    return found


# Solar systems ---------------------------------------------------------------------


def solar_system(system_id: int | None) -> SolarSystem | None:
    """From the database, else the bundled catalog, else ESI."""
    if not system_id:
        return None
    existing = SolarSystem.objects.filter(id=system_id).first()
    if existing:
        return existing
    info = load_catalog().system(system_id)
    if info:
        defaults = {
            "name": info.name,
            "security": info.security,
            "constellation_id": info.constellation_id,
            "region_id": info.region_id,
            "region_name": info.region,
        }
    else:
        try:
            data = esi_system(system_id)
            constellation = esi_constellation(data["constellation_id"])
            region = esi_region(constellation["region_id"])
        except Exception as ex:  # noqa: BLE001
            logger.warning("ESI system %s lookup failed: %s", system_id, ex)
            return None
        defaults = {
            "name": data.get("name", str(system_id)),
            "security": float(data.get("security_status", 0.0)),
            "constellation_id": data.get("constellation_id"),
            "region_id": constellation.get("region_id"),
            "region_name": region.get("name", ""),
        }
    system, _ = SolarSystem.objects.get_or_create(id=system_id, defaults=defaults)
    return system


def solar_system_by_name(name: str | None) -> SolarSystem | None:
    info = load_catalog().system_by_name(name)
    return solar_system(info.system_id) if info else None
