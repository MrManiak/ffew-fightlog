"""Fight rebuild (technical plan §4.2 and §5): everything derived from a fight's logs."""

from collections import Counter, defaultdict
from datetime import datetime
from datetime import timezone as dt_timezone

from django.db import transaction
from django.db.models import F

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.services.hooks import get_extension_logger

from fightlog import app_settings
from fightlog.analysis.aggregate import Perspective, aggregate
from fightlog.analysis.attribution import ShipOnlyActor, attribute, parse_ship_only_key
from fightlog.analysis.dedupe import assign_dup_groups
from fightlog.analysis.hulls import HullTimeline
from fightlog.analysis.identity import FRIENDLY_ACT, HOSTILE_ACT, display_name
from fightlog.analysis.offsets import estimate_offsets
from fightlog.analysis.sides import KillmailEvidence, Party, collect_interactions, guess_sides
from fightlog.analysis.store import FIGHT_COLUMNS, EventStore
from fightlog.catalog import load_catalog
from fightlog.models import (
    AttributionOverride,
    CombatLog,
    Fight,
    FightData,
    FightDataChunk,
    FriendlyEntity,
    Participant,
    ParticipantShip,
    Side,
    SideOverride,
)
from fightlog.parser import ActorKind, ContextEvent, ContextKind

from . import resolve
from .fights import is_kept_when_empty

logger = get_extension_logger(__name__)

PARTY_KINDS = (ActorKind.PILOT, ActorKind.STRUCTURE, ActorKind.DRONE)


def _dt(epoch: int) -> datetime:
    return datetime.fromtimestamp(epoch, tz=dt_timezone.utc)


def _merge_actor(into: dict, actor: dict) -> None:
    for key, value in actor.items():
        if key == "listener":
            into["listener"] = into.get("listener") or value
        elif value and not into.get(key):
            into[key] = value


def merge_engagements(engagements, offsets: dict[int, int] | None = None):
    """Engagement stores → one fight store (sorted, offsets applied) + actor info + hulls."""
    offsets = offsets or {}
    rows = []
    actor_info: dict[str, dict] = {}
    hull_obs = []
    for engagement in engagements:
        store = EventStore.decode(engagement.store)
        actors, modules = store.actors, store.modules
        for actor in actors:
            _merge_actor(actor_info.setdefault(actor["key"], {"key": actor["key"]}), actor)
        shift = offsets.get(engagement.log_id, 0)
        for key, ship, first, last in store.meta.get("hulls", []):
            hull_obs.append((key, ship, first - shift, last - shift, engagement.log_id))
        log_id = engagement.log_id
        for t, kind, src, tgt, module, amount, quality, flags, line_no in store.rows():
            rows.append(
                (
                    t - shift,
                    kind,
                    actors[src]["key"],
                    actors[tgt]["key"],
                    modules[module] if module >= 0 else None,
                    amount,
                    quality,
                    flags,
                    line_no,
                    log_id,
                )
            )
    rows.sort(key=lambda r: (r[0], r[9], r[8]))
    fight_store = EventStore(FIGHT_COLUMNS)
    for t, kind, src, tgt, module, amount, quality, flags, line_no, log_id in rows:
        fight_store.append(
            t=t,
            kind=kind,
            src=fight_store.actor_id(actor_info[src]),
            tgt=fight_store.actor_id(actor_info[tgt]),
            module=fight_store.module_id(module),
            amount=amount,
            quality=quality,
            flags=flags,
            line_no=line_no,
            log_id=log_id,
            dup_group=-1,
        )
    return fight_store, actor_info, hull_obs


def fleet_periods(context: list[dict], session_start: int | None) -> list[tuple[int, int, set[str]]]:
    """(start, end, member names) per fleet the listener was in, from context lines."""
    events = sorted((ContextEvent.from_dict(c) for c in context), key=lambda c: c.t)
    periods: list[tuple[int, int, set[str]]] = []
    start = session_start or (events[0].t if events else 0)
    members: set[str] = set()
    for event in events:
        if event.kind in ContextKind.FLEET_START_KINDS:
            if members:
                periods.append((start, event.t, members))
            start, members = event.t, {event.name} if event.name else set()
        elif event.kind in ContextKind.FLEET_KINDS and event.name:
            members.add(event.name)
    if members:
        periods.append((start, 2**31 - 1, members))
    return periods


def system_from_logs(logs: list[CombatLog], start: int, end: int) -> tuple[str | None, bool]:
    """(system name from the logs' last system line, saw w-space hints)."""
    names = Counter()
    wspace = False
    for log in logs:
        last_system, last_t = None, None
        hint_since_system = False
        for c in sorted(log.context or [], key=lambda c: c["t"]):
            if c["t"] > end:
                break
            if c["kind"] == ContextKind.SYSTEM:
                last_system, last_t, hint_since_system = c.get("system"), c["t"], False
            elif c["kind"] in (ContextKind.WSPACE_HINT, ContextKind.WORMHOLE_HINT):
                # Wormhole jumps are never logged: after a hint, the last system line may be stale.
                if last_t is not None or c["t"] >= start - 3600:
                    hint_since_system = True
        if hint_since_system:
            wspace = True
        elif last_system and last_t is not None and last_t <= end:
            names[last_system] += 1
    if wspace:
        return None, True
    return (names.most_common(1)[0][0] if names else None), False


class FightRebuilder:
    def __init__(self, fight: Fight):
        self.fight = fight
        self.catalog = load_catalog()

    def run(self) -> Fight | None:
        fight = self.fight
        started_version = fight.data_version
        engagements = list(fight.engagements.select_related("log").order_by("start_at", "pk"))
        if not engagements:
            return self._empty(started_version)
        logs = {e.log_id: e.log for e in engagements}
        listener_keys = {log_id: f"p:{log.listener_name}" for log_id, log in logs.items()}

        # 1. Merge, estimate clock offsets, merge again with offsets applied.
        raw_store, _, _ = merge_engagements(engagements)
        offsets, offset_evidence = estimate_offsets(
            raw_store, listener_keys, app_settings.FIGHTLOG_CLOCK_OFFSET_SEARCH_SECONDS
        )
        store, actor_info, hull_obs = merge_engagements(engagements, offsets)
        for log_id, log in logs.items():
            if log.clock_offset != offsets.get(log_id, 0):
                CombatLog.objects.filter(pk=log_id).update(clock_offset=offsets.get(log_id, 0))
        times = store.column("t")
        start, end = times[0], times[-1]

        # 2. Identities: labelled pilot names → ESI, killmail participants → names.
        pilot_names = {
            info["name"]
            for key, info in actor_info.items()
            if key.startswith("p:") and info.get("name") and info.get("kind") in PARTY_KINDS
        }
        killmail_links = list(fight.fight_killmails.select_related("killmail"))
        km_char_ids = set()
        for link in killmail_links:
            km = link.killmail
            km_char_ids.add(km.victim_character_id)
            km_char_ids.update(a.get("character_id") for a in km.attackers)
        km_names = resolve.resolve_character_ids(km_char_ids) if km_char_ids else {}
        resolved = resolve.resolve_names(pilot_names | set(km_names.values()))

        # Killmail affiliations are historical: they win for pilots on this fight's killmails.
        km_affiliation: dict[str, tuple] = {}
        for link in killmail_links:
            km = link.killmail
            entries = [
                (km.victim_character_id, km.victim_corporation_id, km.victim_alliance_id, km.victim_ship_type_id, True)
            ]
            entries += [
                (a.get("character_id"), a.get("corporation_id"), a.get("alliance_id"), a.get("ship_type_id"), False)
                for a in km.attackers
            ]
            for cid, corp_id, alliance_id, ship_id, _ in entries:
                name = km_names.get(cid)
                if name:
                    km_affiliation[f"p:{name}"] = (cid, corp_id, alliance_id)
                    ship = self.catalog.by_id(ship_id)
                    if ship:
                        t = int(km.time.timestamp())
                        hull_obs.append((f"p:{name}", ship.name, t, t, None))
        for key in km_affiliation:
            if key not in actor_info:
                actor_info[key] = {"key": key, "kind": ActorKind.PILOT, "name": key[2:], "from_killmail": True}

        corp_ids, alliance_ids = set(), set()
        for rn in resolved.values():
            corp_ids.add(rn.corporation_id)
            alliance_ids.add(rn.alliance_id)
        for cid, corp_id, alliance_id in km_affiliation.values():
            corp_ids.add(corp_id)
            alliance_ids.add(alliance_id)
        corp_ids.discard(None)
        alliance_ids.discard(None)
        corps = resolve.corporations(corp_ids)
        alliance_ids.update(c.alliance_id for c in corps.values() if c.alliance_id)
        alliances = resolve.alliances(alliance_ids)
        ticker_to_corp: dict[str, int] = {}
        for key, info in actor_info.items():
            rn = resolved.get(info.get("name")) if key.startswith("p:") else None
            if rn and rn.corporation_id and info.get("corp"):
                ticker_to_corp.setdefault(info["corp"], rn.corporation_id)
        for corp in corps.values():
            ticker_to_corp.setdefault(corp.ticker, corp.id)
        ticker_to_alliance = {a.ticker: a.id for a in alliances.values()}

        identity: dict[str, dict] = {}
        for key, info in actor_info.items():
            if info.get("kind") not in PARTY_KINDS:
                continue
            character_id = corp_id = alliance_id = None
            rn = resolved.get(info.get("name")) if key.startswith("p:") else None
            if rn and rn.character_id:
                character_id, corp_id, alliance_id = rn.character_id, rn.corporation_id, rn.alliance_id
            if info.get("corp") and info["corp"] in ticker_to_corp:
                fight_time_corp = ticker_to_corp[info["corp"]]
                if fight_time_corp != corp_id:
                    corp_id = fight_time_corp
                    alliance_id = corps[corp_id].alliance_id if corp_id in corps else None
            if key in km_affiliation:
                character_id, corp_id, alliance_id = km_affiliation[key]
            if not alliance_id and info.get("alliance") in ticker_to_alliance:
                alliance_id = ticker_to_alliance[info["alliance"]]
            if corp_id and not alliance_id and corp_id in corps:
                alliance_id = corps[corp_id].alliance_id
            identity[key] = {
                "character_id": character_id,
                "corp_id": corp_id,
                "corp_ticker": (corps[corp_id].ticker if corp_id in corps else info.get("corp")) or "",
                "alliance_id": alliance_id,
                "alliance_ticker": (alliances[alliance_id].ticker if alliance_id in alliances else info.get("alliance"))
                or "",
            }
        owned_ids = set(
            CharacterOwnership.objects.filter(
                character__character_id__in=[i["character_id"] for i in identity.values() if i["character_id"]]
            ).values_list("character__character_id", flat=True)
        )
        listener_ids = {log.listener_character_id for log in logs.values() if log.listener_character_id}
        owned_listener_ids = set(
            CharacterOwnership.objects.filter(character__character_id__in=listener_ids).values_list(
                "character__character_id", flat=True
            )
        )
        for log in logs.values():
            key = listener_keys[log.pk]
            if key in identity and log.listener_character_id:
                identity[key]["character_id"] = identity[key]["character_id"] or log.listener_character_id
        # 3. Hulls, then cross-log duplicates.
        hulls = HullTimeline()
        for key, ship, first, last, log_id in hull_obs:
            hulls.add(key, ship, first, last, "killmail" if log_id is None else "peer_log")
        _, peer_matches = assign_dup_groups(store, listener_keys, hulls, app_settings.FIGHTLOG_DEDUPE_TOLERANCE_SECONDS)

        # 4. Sides.
        parties = {
            key: Party(
                key=key,
                corp_id=ident["corp_id"],
                corp_ticker=ident["corp_ticker"] or None,
                alliance_id=ident["alliance_id"],
                alliance_ticker=ident["alliance_ticker"] or None,
                aa_owned=bool(ident["character_id"])
                and (ident["character_id"] in owned_ids or ident["character_id"] in owned_listener_ids),
            )
            for key, ident in identity.items()
        }
        hostile, friendly, aoe = collect_interactions(store, set(parties))
        fleet_pairs = []
        for log in logs.values():
            session = int(log.session_started_at.timestamp()) if log.session_started_at else None
            for p_start, p_end, members in fleet_periods(log.context or [], session):
                if p_start <= end and p_end >= start:
                    for member in members:
                        if f"p:{member}" in parties:
                            fleet_pairs.append((listener_keys[log.pk], f"p:{member}"))
        killmail_evidence = []
        for link in killmail_links:
            if link.role != "core":
                continue
            km = link.killmail
            total = sum(a.get("damage_done", 0) for a in km.attackers) or 1
            attackers = []
            for a in km.attackers:
                name = km_names.get(a.get("character_id"))
                if name:
                    attackers.append((f"p:{name}", a.get("damage_done", 0) / total, bool(a.get("aoe_suspect"))))
            victim = km_names.get(km.victim_character_id)
            killmail_evidence.append(KillmailEvidence(f"p:{victim}" if victim else None, attackers))
        friendly_groups = set()
        for entity in FriendlyEntity.objects.all():
            friendly_groups.add(("a:" if entity.entity_type == "alliance" else "c:") + str(entity.entity_id))
        overrides = {o.participant_key: o for o in SideOverride.objects.filter(fight=fight).select_related("side")}
        pinned = {k: o.side.order for k, o in overrides.items() if o.side is not None}
        result = guess_sides(
            parties,
            hostile,
            friendly,
            aoe,
            fleet_pairs,
            killmail_evidence,
            friendly_groups,
            pinned=pinned,
            min_confidence=app_settings.FIGHTLOG_SIDE_MIN_CONFIDENCE,
        )
        side_index: dict[str, int | None] = {}
        for key in parties:
            if key in overrides:
                side_index[key] = overrides[key].side.order if overrides[key].side else None
            else:
                side_index[key] = result.side_of(key)

        # 5. Ship-only actors.
        actor_times: dict[str, list[int]] = defaultdict(list)
        src_col, tgt_col = store.column("src"), store.column("tgt")
        for i in range(len(store)):
            for idx in (src_col[i], tgt_col[i]):
                key = store.actors[idx]["key"]
                if key.startswith("s:"):
                    actor_times[key].append(times[i])
        ship_only = []
        for key, ts in actor_times.items():
            parsed = parse_ship_only_key(key)
            if parsed and parsed[0] in listener_keys:
                ship_only.append(
                    ShipOnlyActor(
                        key,
                        parsed[0],
                        parsed[1],
                        actor_info[key].get("ship") or parsed[2],
                        ts,
                        listener_keys[parsed[0]],
                    )
                )
        log_windows = {}
        log_col = store.column("log_id")
        for i in range(len(store)):
            key = listener_keys.get(log_col[i])
            first_last = log_windows.get(key)
            log_windows[key] = (times[i], times[i]) if first_last is None else (first_last[0], times[i])
        attribution_overrides = {
            f"s:{o.log_id}:{o.polarity}:{o.raw_label}": o.participant_key
            for o in AttributionOverride.objects.filter(fight=fight)
        }
        pilots = {k for k, i in actor_info.items() if i.get("kind") == ActorKind.PILOT and k in parties}
        attributions = attribute(ship_only, peer_matches, side_index, pilots, hulls, log_windows, attribution_overrides)
        for actor in ship_only:
            if actor.key in attributions:
                side_index[actor.key] = side_index.get(attributions[actor.key][0])
            elif actor.key in overrides:
                side_index[actor.key] = overrides[actor.key].side.order if overrides[actor.key].side else None
            else:
                listener_side = side_index.get(actor.listener_key)
                if actor.polarity == FRIENDLY_ACT:
                    side_index[actor.key] = listener_side
                elif actor.polarity == HOSTILE_ACT and listener_side == 0:
                    side_index[actor.key] = 1
                else:
                    side_index[actor.key] = None

        with transaction.atomic():
            sides = self._save_sides(side_index)
            self._save_participants(
                actor_info,
                identity,
                side_index,
                sides,
                result,
                overrides,
                attributions,
                hulls,
                listener_keys,
                owned_ids | owned_listener_ids,
            )
            self._link_victims()
            self._save_system_and_title(list(logs.values()), start, end, sides)
            self._save_store(store)
            fight.start_at, fight.end_at = _dt(start), _dt(end)
            fight.summary = self._summary(store, logs, offsets, offset_evidence)
            fight.built_version = started_version
            fight.save()
        return fight

    # persistence helpers ----------------------------------------------------------

    def _empty(self, started_version: int) -> Fight | None:
        fight = self.fight
        if not is_kept_when_empty(fight):
            fight.delete()
            return None
        FightData.objects.filter(fight=fight).delete()
        fight.participants.all().delete()
        fight.summary = {"empty": True}
        fight.built_version = started_version
        fight.save()
        return fight

    def _save_sides(self, side_index: dict) -> dict[int, Side]:
        fight = self.fight
        orders = {0} | {s for s in side_index.values() if s is not None}
        existing = {s.order: s for s in fight.sides.all()}
        for order in sorted(orders):
            if order not in existing:
                name = "Friendly" if order == 0 else ("Hostile" if order == 1 else f"Hostile {order}")
                existing[order] = Side.objects.create(fight=fight, order=order, is_friendly=order == 0, name=name)
        return existing

    def _save_participants(
        self,
        actor_info,
        identity,
        side_index,
        sides,
        result,
        overrides,
        attributions,
        hulls,
        listener_keys,
        owned_ids,
    ):
        fight = self.fight
        wanted = {}
        for key, info in actor_info.items():
            kind = info.get("kind")
            if kind not in PARTY_KINDS and kind != ActorKind.SHIP_ONLY:
                continue
            wanted[key] = info
        existing = {p.key: p for p in fight.participants.all()}
        listener_set = set(listener_keys.values())
        rows = {}
        for key, info in wanted.items():
            ident = identity.get(key, {})
            side_order = side_index.get(key)
            group = result.group_of.get(key)
            participant = existing.get(key) or Participant(fight=fight, key=key)
            participant.kind = (
                info.get("kind") if info.get("kind") in Participant.Kind.values else Participant.Kind.PILOT
            )
            participant.name = (
                display_name(info) if info.get("kind") == ActorKind.SHIP_ONLY else info.get("name") or key
            )[:255]
            participant.character_id = ident.get("character_id")
            participant.corporation_id = ident.get("corp_id")
            participant.corporation_ticker = (ident.get("corp_ticker") or info.get("corp") or "")[:10]
            participant.alliance_id = ident.get("alliance_id")
            participant.alliance_ticker = (ident.get("alliance_ticker") or info.get("alliance") or "")[:10]
            participant.side = sides.get(side_order) if side_order is not None else None
            participant.side_source = "manual" if key in overrides else "auto"
            participant.side_confidence = result.confidence.get(group, 0.0) if group else 0.0
            participant.hint = result.hints.get(group, "") if group and side_order is None else ""
            participant.has_log = key in listener_set
            participant.is_aa_member = bool(participant.character_id and participant.character_id in owned_ids)
            ships = [o.ship for o in hulls.intervals(key)]
            if info.get("kind") == ActorKind.SHIP_ONLY and info.get("ship"):
                ships = [info["ship"]]
            participant.ships = ", ".join(dict.fromkeys(ships))[:255]
            participant.attributed_to = None
            participant.attribution_source = ""
            participant.save()
            rows[key] = participant
        for key, (pilot_key, source) in attributions.items():
            if key in rows and pilot_key in rows:
                rows[key].attributed_to = rows[pilot_key]
                rows[key].attribution_source = source
                rows[key].save(update_fields=["attributed_to", "attribution_source"])
        fight.participants.exclude(key__in=list(rows)).delete()
        ParticipantShip.objects.filter(participant__fight=fight).delete()
        ship_rows = []
        for key, participant in rows.items():
            for obs in hulls.intervals(key):
                info = self.catalog.get(obs.ship)
                ship_rows.append(
                    ParticipantShip(
                        participant=participant,
                        type_id=info[0].type_id if info else None,
                        type_name=obs.ship[:100],
                        source=obs.source,
                        first_seen=_dt(obs.first),
                        last_seen=_dt(obs.last),
                    )
                )
        ParticipantShip.objects.bulk_create(ship_rows)

    def _link_victims(self):
        fight = self.fight
        by_character = {p.character_id: p for p in fight.participants.exclude(character_id__isnull=True)}
        for link in fight.fight_killmails.select_related("killmail"):
            participant = by_character.get(link.killmail.victim_character_id)
            if link.victim_participant_id != (participant.pk if participant else None):
                link.victim_participant = participant
                link.save(update_fields=["victim_participant"])

    def _save_system_and_title(self, logs, start, end, sides):
        fight = self.fight
        if fight.system_source not in (Fight.SystemSource.MANUAL, Fight.SystemSource.ZKB):
            name, wspace = system_from_logs(logs, start, end)
            system = resolve.solar_system_by_name(name) if name else None
            if system:
                fight.system, fight.system_source = system, Fight.SystemSource.LOG
            elif wspace:
                fight.system, fight.system_source = None, Fight.SystemSource.WSPACE_UNKNOWN
            else:
                fight.system, fight.system_source = None, Fight.SystemSource.NONE
        if not fight.title_is_manual:
            fight.title = self._title(start, sides)

    def _group_label(self, side: Side | None) -> str | None:
        if side is None:
            return None
        counts = Counter()
        for alliance, corp in side.participants.filter(kind=Participant.Kind.PILOT).values_list(
            "alliance_ticker", "corporation_ticker"
        ):
            label = alliance or corp
            if label:
                counts[label] += 1
        return counts.most_common(1)[0][0] if counts else None

    def _title(self, start: int, sides: dict[int, Side]) -> str:
        friendly_side = sides.get(0)
        if friendly_side is not None and not friendly_side.participants.exists() and 1 in sides:
            # None of ours took part: name the fight after the two largest other sides.
            first, second = self._group_label(sides.get(1)) or "Unknown", self._group_label(sides.get(2)) or "Unknown"
        else:
            first = app_settings.FIGHTLOG_FRIENDLY_LABEL or self._group_label(friendly_side) or "Friendly"
            second = self._group_label(sides.get(1)) or "Unknown"
        title = f"{_dt(start):%Y-%m-%d %H:%M} — {first} vs {second}"
        if self.fight.system:
            title += f" — {self.fight.system.name}"
        return title[:200]

    def _save_store(self, store: EventStore) -> None:
        fight = self.fight
        data, _ = FightData.objects.get_or_create(fight=fight)
        data.chunks.all().delete()
        size = app_settings.FIGHTLOG_STORE_CHUNK_EVENTS
        chunks = []
        for seq, begin in enumerate(range(0, len(store), size)):
            part = store.slice(begin, begin + size)
            part.actors, part.modules, part.meta = [], [], {}
            chunks.append(
                FightDataChunk(fight_data=data, seq=seq, store=part.encode(app_settings.FIGHTLOG_MAX_BLOB_BYTES))
            )
        FightDataChunk.objects.bulk_create(chunks)
        data.actors = {"actors": store.actors, "modules": store.modules}
        data.event_count = len(store)
        data.chunk_count = len(chunks)
        data.save()

    def _summary(self, store: EventStore, logs: dict, offsets: dict, offset_evidence: dict) -> dict:
        from .analysis import participants_map

        fight = self.fight
        participants = participants_map(fight)
        active = {log_id for log_id, log in logs.items() if not log.excluded}
        friendly_side = fight.sides.filter(order=0).first()
        result = aggregate(
            store, participants, active, Perspective("side", str(friendly_side.pk) if friendly_side else "0"), 30
        )
        per_side = Counter(p.side_id for p in fight.participants.filter(kind=Participant.Kind.PILOT))
        losses = Counter()
        isk = Counter()
        for link in fight.fight_killmails.filter(role="core").select_related("killmail", "victim_participant"):
            side_id = link.victim_participant.side_id if link.victim_participant else None
            losses[side_id] += 1
            isk[side_id] += link.killmail.total_value
        friendly_pilots = fight.participants.filter(side__order=0, kind=Participant.Kind.PILOT)
        return {
            "logs": len(logs),
            "events": len(store),
            "pilots": {str(k): v for k, v in per_side.items()},
            "losses": {str(k): v for k, v in losses.items()},
            "isk_lost": {str(k): v for k, v in isk.items()},
            "coverage": {"logged": friendly_pilots.filter(has_log=True).count(), "pilots": friendly_pilots.count()},
            "totals": result["totals"],
            "offsets": {str(k): v for k, v in offsets.items()},
            "offset_evidence": offset_evidence,
        }


def rebuild_fight(fight_id: int) -> Fight | None:
    fight = Fight.objects.filter(pk=fight_id).first()
    if fight is None:
        return None
    if fight.built_version >= fight.data_version:
        return fight
    return FightRebuilder(fight).run()


def mark_stale(fight_ids) -> None:
    Fight.objects.filter(pk__in=list(fight_ids)).update(data_version=F("data_version") + 1)


__all__ = ["FightRebuilder", "merge_engagements", "rebuild_fight", "system_from_logs"]
