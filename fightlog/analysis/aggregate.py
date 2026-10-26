"""Fight aggregation (technical plan §5.5): timeline, per-pilot table, targets, drill-down.

One pass over the packed fight store. Pure Python: 250k events take well under a second.
"""

from collections import defaultdict
from dataclasses import dataclass

from fightlog.parser import Flag, Kind, Quality

from .identity import display_name

SERIES = (
    "damage_out",
    "damage_in",
    "friendly_fire",
    "rep_out",
    "rep_in",
    "cap_out",
    "cap_in",
    "neut_out",
    "neut_in",
    "nos_out",
    "nos_in",
    "jam_out",
    "jam_in",
    "tackle_out",
    "tackle_in",
)
REP_FIELD = {int(Kind.REP_SHIELD): "shield", int(Kind.REP_ARMOR): "armor", int(Kind.REP_HULL): "hull"}
K_DAMAGE, K_MISS = int(Kind.DAMAGE), int(Kind.MISS)
K_CAP, K_NEUT, K_NOS, K_JAM = int(Kind.CAP), int(Kind.NEUT), int(Kind.NOS), int(Kind.JAM)
K_TACKLE = (int(Kind.SCRAM), int(Kind.DISRUPT))
F_AOE, F_CAN_MISS, F_NON_SHIP, F_OBSERVED = (
    int(Flag.AOE),
    int(Flag.CAN_MISS),
    int(Flag.NON_SHIP_TARGET),
    int(Flag.OBSERVED),
)


@dataclass
class Perspective:
    kind: str  # side, pilot, log
    value: str

    @classmethod
    def parse(cls, text: str | None, default_side: str) -> "Perspective":
        if text and ":" in text:
            kind, value = text.split(":", 1)
            if kind in ("side", "pilot", "log"):
                return cls(kind, value)
        return cls("side", default_side)

    def __str__(self) -> str:
        return f"{self.kind}:{self.value}"


def _new_row(key: str, info: dict) -> dict:
    return {
        "key": key,
        "name": info.get("name") or key,
        "kind": info.get("kind"),
        "side": info.get("side"),
        "ships": info.get("ships", ""),
        "corp": info.get("corp", ""),
        "alliance": info.get("alliance", ""),
        "has_log": bool(info.get("has_log")),
        "attributed": bool(info.get("attributed")),
        "hint": info.get("hint", ""),
        "damage_out": 0,
        "damage_in": 0,
        "ff_out": 0,
        "non_ship_out": 0,
        "hits": 0,
        "misses": 0,
        "quality": [0] * 7,
        "rep_out": {"shield": 0, "armor": 0, "hull": 0},
        "rep_in": {"shield": 0, "armor": 0, "hull": 0},
        "cap_out": 0,
        "cap_in": 0,
        "neut_out": 0,
        "neut_in": 0,
        "nos_out": 0,
        "nos_in": 0,
        "jam_out": 0,
        "jam_in": 0,
        "tackle_out": 0,
        "tackle_in": 0,
    }


def aggregate(
    store,
    participants: dict[str, dict],
    active_logs: set[int],
    perspective: Perspective,
    bucket: int = 10,
    include_non_ships: bool = False,
    kills: list[dict] | None = None,
) -> dict:
    """Aggregate the active logs' events.

    ``participants`` maps every actor key to {"name", "kind", "side", "has_log", ...},
    with ship-only actors already mapped to the pilot they were attributed to via "alias".
    """
    actors = store.actors
    alias = [participants.get(a["key"], {}).get("alias", a["key"]) for a in actors]
    side = [participants.get(k, {}).get("side") for k in alias]
    is_party = [k in participants and participants[k].get("party", True) for k in alias]

    def in_perspective(i: int) -> bool:
        if perspective.kind == "side":
            return side[i] is not None and str(side[i]) == perspective.value
        if perspective.kind == "pilot":
            return alias[i] == perspective.value
        return False

    t_col, kind_col = store.column("t"), store.column("kind")
    src_col, tgt_col = store.column("src"), store.column("tgt")
    amount_col, quality_col, flags_col = store.column("amount"), store.column("quality"), store.column("flags")
    log_col, dup_col = store.column("log_id"), store.column("dup_group")
    n = len(store)
    rows: dict[str, dict] = {}
    targets: dict[str, dict] = {}
    timeline: dict[int, list[int]] = defaultdict(lambda: [0] * len(SERIES))
    series_index = {name: i for i, name in enumerate(SERIES)}
    seen_dups: set[int] = set()
    start = end = None
    log_perspective = int(perspective.value) if perspective.kind == "log" and perspective.value.isdigit() else None
    log_listener_key = None
    if log_perspective is not None:
        log_listener_key = participants.get(f"__log:{log_perspective}", {}).get("key")

    def row(i: int) -> dict:
        key = alias[i]
        r = rows.get(key)
        if r is None:
            r = rows[key] = _new_row(key, participants.get(key, {"name": display_name(actors[i])}))
        return r

    for i in range(n):
        log_id = log_col[i]
        if log_id not in active_logs:
            continue
        if log_perspective is not None and log_id != log_perspective:
            continue
        g = dup_col[i]
        if g >= 0:
            if g in seen_dups:
                continue
            seen_dups.add(g)
        t = t_col[i]
        start = t if start is None or t < start else start
        end = t if end is None or t > end else end
        kind, amount, flags = kind_col[i], amount_col[i], flags_col[i]
        s, d = src_col[i], tgt_col[i]
        # Only player-vs-player events count; non-ship targets (drones, missiles,
        # structures without a side) are tracked separately below.
        if not is_party[s] or not (is_party[d] or (kind == K_DAMAGE and flags & F_NON_SHIP)):
            continue
        if log_listener_key is not None:
            out = alias[s] == log_listener_key
            inc = alias[d] == log_listener_key
        else:
            out = in_perspective(s)
            inc = in_perspective(d)
        same_side = side[s] is not None and side[s] == side[d]
        bucket_row = timeline[(t // bucket) * bucket]
        src_row = row(s) if is_party[s] else None
        tgt_row = row(d) if is_party[d] else None
        if kind == K_DAMAGE:
            non_ship = bool(flags & F_NON_SHIP)
            if non_ship and not include_non_ships:
                if src_row:
                    src_row["non_ship_out"] += amount
                continue
            if same_side:
                bucket_row[series_index["friendly_fire"]] += amount if (out or inc) else 0
                if src_row:
                    src_row["ff_out"] += amount
                continue
            if out:
                bucket_row[series_index["damage_out"]] += amount
            if inc:
                bucket_row[series_index["damage_in"]] += amount
            if src_row:
                src_row["damage_out"] += amount
                if flags & F_CAN_MISS:
                    src_row["hits"] += 1
                    q = quality_col[i]
                    if 0 < q < 7:
                        src_row["quality"][q] += 1
            if tgt_row:
                tgt_row["damage_in"] += amount
                key = alias[d]
                tr = targets.get(key)
                if tr is None:
                    tr = targets[key] = {"key": key, "first": t, "last": t, "damage": 0, "shooters": set(), "reps": 0}
                tr["last"] = t
                tr["damage"] += amount
                tr["shooters"].add(alias[s])
        elif kind == K_MISS:
            if src_row and not same_side:
                src_row["misses"] += 1
        elif kind in REP_FIELD:
            field = REP_FIELD[kind]
            if out:
                bucket_row[series_index["rep_out"]] += amount
            if inc:
                bucket_row[series_index["rep_in"]] += amount
            if src_row:
                src_row["rep_out"][field] += amount
            if tgt_row:
                tgt_row["rep_in"][field] += amount
                tr = targets.get(alias[d])
                if tr is not None:
                    tr["reps"] += amount
        else:
            name = {K_CAP: "cap", K_NEUT: "neut", K_NOS: "nos", K_JAM: "jam"}.get(kind)
            if name is None and kind in K_TACKLE:
                name = "tackle"
            if name is None:
                continue
            value = 1 if name in ("jam", "tackle") else amount
            if out:
                bucket_row[series_index[f"{name}_out"]] += value
            if inc:
                bucket_row[series_index[f"{name}_in"]] += value
            if src_row:
                src_row[f"{name}_out"] += value
            if tgt_row:
                tgt_row[f"{name}_in"] += value

    labels = sorted(timeline)
    if labels:
        full = list(range(labels[0], labels[-1] + bucket, bucket))
    else:
        full = []
    series = {name: [timeline[t][i] if t in timeline else 0 for t in full] for i, name in enumerate(SERIES)}
    deaths = {}
    for kill in kills or []:
        key = kill.get("victim_key")
        if key and (key not in deaths or kill["time"] < deaths[key]):
            deaths[key] = kill["time"]
    pilot_rows = []
    for key, r in rows.items():
        shots = r["hits"] + r["misses"]
        r["hit_pct"] = round(100.0 * r["hits"] / shots, 1) if shots else None
        r["states"] = _cell_states(r)
        pilot_rows.append(r)
    pilot_rows.sort(key=lambda r: (r["side"] is None, r["side"] if r["side"] is not None else 99, -r["damage_out"]))
    target_rows = []
    for key, tr in targets.items():
        died = deaths.get(key)
        stop = died if died is not None and died >= tr["first"] else tr["last"]
        target_rows.append(
            {
                "key": key,
                "name": participants.get(key, {}).get("name", key),
                "side": participants.get(key, {}).get("side"),
                "first_hit": tr["first"],
                "end": stop,
                "seconds": stop - tr["first"],
                "died": died is not None,
                "damage": tr["damage"],
                "shooters": len(tr["shooters"]),
                "reps": tr["reps"],
            }
        )
    target_rows.sort(key=lambda r: -r["damage"])
    totals: dict = defaultdict(lambda: defaultdict(int))
    for r in pilot_rows:
        side_key = "unknown" if r["side"] is None else str(r["side"])
        bucket_totals = totals[side_key]
        for field in ("damage_out", "damage_in", "ff_out", "cap_out", "neut_out", "jam_out", "tackle_out"):
            bucket_totals[field] += r[field]
        bucket_totals["rep_out"] += sum(r["rep_out"].values())
        bucket_totals["pilots"] += 1
    return {
        "bucket": bucket,
        "start": start,
        "end": end,
        "labels": full,
        "series": series,
        "pilots": pilot_rows,
        "targets": target_rows,
        "totals": {k: dict(v) for k, v in totals.items()},
        "perspective": str(perspective),
    }


def _cell_states(r: dict) -> dict:
    if r["kind"] == "ship_only" and not r["attributed"]:
        base = "unattributed"
    elif r["has_log"]:
        base = "complete"
    else:
        base = "partial"
    states = {"default": base}
    if r["hits"] + r["misses"] == 0:
        states["hit_pct"] = "na"
    return states


def drilldown(
    store,
    participants: dict[str, dict],
    active_logs: set[int],
    start: int,
    end: int,
    key: str | None = None,
    kinds: set[int] | None = None,
    limit: int = 500,
    offset: int = 0,
    log_names: dict | None = None,
) -> dict:
    actors = store.actors
    alias = [participants.get(a["key"], {}).get("alias", a["key"]) for a in actors]
    out = []
    total = 0
    seen = set()
    for t, kind, src, tgt, module, amount, quality, flags, line_no, log_id, dup in store.rows():
        if log_id not in active_logs or t < start or t > end:
            continue
        if kinds and kind not in kinds:
            continue
        if key and alias[src] != key and alias[tgt] != key:
            continue
        if dup >= 0:
            if dup in seen:
                continue
            seen.add(dup)
        total += 1
        if total <= offset or len(out) >= limit:
            continue
        out.append(
            {
                "t": t,
                "kind": Kind(kind).name.lower(),
                "src": participants.get(alias[src], {}).get("name") or display_name(actors[src]),
                "tgt": participants.get(alias[tgt], {}).get("name") or display_name(actors[tgt]),
                "src_raw": actors[src].get("raw", ""),
                "tgt_raw": actors[tgt].get("raw", ""),
                "module": store.module_name(module),
                "amount": amount,
                "quality": Quality(quality).name.replace("_", " ").title() if quality else "",
                "aoe": bool(flags & F_AOE),
                "observed": bool(flags & F_OBSERVED),
                "log": (log_names or {}).get(log_id, log_id),
                "line": line_no,
            }
        )
    return {"total": total, "offset": offset, "events": out}


def pilot_breakdown(store, participants: dict[str, dict], active_logs: set[int], key: str) -> dict:
    actors = store.actors
    alias = [participants.get(a["key"], {}).get("alias", a["key"]) for a in actors]
    weapons: dict[str, dict] = defaultdict(lambda: {"damage": 0, "hits": 0, "misses": 0})
    targets: dict[str, int] = defaultdict(int)
    sources: dict[str, int] = defaultdict(int)
    reppers: dict[str, int] = defaultdict(int)
    ewar: dict[str, int] = defaultdict(int)
    seen = set()
    for _t, kind, src, tgt, module, amount, _quality, flags, _line_no, log_id, dup in store.rows():
        if log_id not in active_logs:
            continue
        if dup >= 0:
            if dup in seen:
                continue
            seen.add(dup)
        s, d = alias[src], alias[tgt]
        if key not in (s, d):
            continue
        name_s = participants.get(s, {}).get("name") or display_name(actors[src])
        name_d = participants.get(d, {}).get("name") or display_name(actors[tgt])
        if kind == K_DAMAGE:
            if s == key:
                w = weapons[store.module_name(module) or "?"]
                w["damage"] += amount
                if flags & F_CAN_MISS:
                    w["hits"] += 1
                targets[name_d] += amount
            else:
                sources[name_s] += amount
        elif kind == K_MISS and s == key:
            weapons[store.module_name(module) or "?"]["misses"] += 1
        elif kind in REP_FIELD and d == key:
            reppers[name_s] += amount
        elif kind in (K_NEUT, K_NOS, K_JAM) + K_TACKLE and d == key:
            ewar[f"{name_s} ({Kind(kind).name.lower()})"] += 1

    def top(d):
        return sorted(({"name": k, "value": v} for k, v in d.items()), key=lambda r: -r["value"])[:25]

    return {
        "weapons": sorted(({"name": k, **v} for k, v in weapons.items()), key=lambda r: -r["damage"]),
        "targets": top(targets),
        "damaged_by": top(sources),
        "repped_by": top(reppers),
        "ewar_by": top(ewar),
    }
