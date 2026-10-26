"""Cross-log duplicate grouping (technical plan §5.3).

When both ends of an interaction uploaded a log, the event appears twice: A's "to B"
and B's "from A". Third-party tackle that several pilots saw appears once per log.
Matched copies share a ``dup_group``; aggregation counts a group once if any of its
logs is active. Matching a ship-type-only copy ("boosted by Loki") against the
sender's own log also tells us who that Loki was.
"""

from bisect import bisect_left
from collections import Counter, defaultdict

from fightlog.parser import ActorKind, Flag, Kind

from .hulls import HullTimeline

TACKLE = (int(Kind.SCRAM), int(Kind.DISRUPT))


class _Pool:
    """Unmatched candidate rows by signature, sorted by time."""

    def __init__(self):
        self.times: dict = defaultdict(list)
        self.rows: dict = defaultdict(list)
        self.used: set = set()

    def add(self, sig, t: int, row: int) -> None:
        self.times[sig].append(t)
        self.rows[sig].append(row)

    def finish(self) -> None:
        for sig, times in self.times.items():
            order = sorted(range(len(times)), key=times.__getitem__)
            self.times[sig] = [times[i] for i in order]
            self.rows[sig] = [self.rows[sig][i] for i in order]

    def take(self, sig, t: int, tolerance: int, accept=None) -> int | None:
        times = self.times.get(sig)
        if not times:
            return None
        i = bisect_left(times, t - tolerance)
        best, best_gap = None, None
        while i < len(times) and times[i] <= t + tolerance:
            row = self.rows[sig][i]
            if row not in self.used and (accept is None or accept(row)):
                gap = abs(times[i] - t)
                if best_gap is None or gap < best_gap:
                    best, best_gap = row, gap
            i += 1
        if best is not None:
            self.used.add(best)
        return best


def assign_dup_groups(store, listener_keys: dict[int, str], hulls: HullTimeline, tolerance: int):
    """Fill the store's dup_group column. Returns (groups found, attributions).

    ``attributions`` maps a ship-only actor key to a Counter of pilot keys it matched.
    """
    actors = store.actors
    t_col, kind_col = store.column("t"), store.column("kind")
    src_col, tgt_col = store.column("src"), store.column("tgt")
    amount_col, module_col = store.column("amount"), store.column("module")
    flags_col, log_col, dup_col = store.column("flags"), store.column("log_id"), store.column("dup_group")
    listener_of = dict(listener_keys)
    key_to_log = {key: log_id for log_id, key in listener_keys.items()}
    attributions: dict[str, Counter] = defaultdict(Counter)
    next_group = 0

    def ship_of(actor: dict) -> str | None:
        return actor.get("ship") if actor.get("kind") == ActorKind.SHIP_ONLY else None

    # 1. Direct interactions between two logged listeners.
    outgoing = _Pool()
    for i in range(len(store)):
        log_id = log_col[i]
        if actors[src_col[i]]["key"] == listener_of.get(log_id) and not flags_col[i] & Flag.OBSERVED:
            outgoing.add((log_id, kind_col[i], amount_col[i], module_col[i]), t_col[i], i)
    outgoing.finish()
    for i in range(len(store)):
        b_log = log_col[i]
        b_key = listener_of.get(b_log)
        tgt = actors[tgt_col[i]]
        if tgt["key"] != b_key or flags_col[i] & Flag.OBSERVED:
            continue
        src = actors[src_col[i]]
        src_ship = ship_of(src)
        if src["key"] in key_to_log and key_to_log[src["key"]] != b_log:
            candidates = [key_to_log[src["key"]]]
        elif src_ship:
            candidates = [
                a for a, a_key in listener_of.items() if a != b_log and hulls.compatible(a_key, src_ship, t_col[i])
            ]
        else:
            continue

        def target_ok(row, b_key=b_key, t=t_col[i]):
            a_tgt = actors[tgt_col[row]]
            if a_tgt["key"] == b_key:
                return True
            a_ship = ship_of(a_tgt)
            return bool(a_ship) and hulls.compatible(b_key, a_ship, t)

        for a_log in candidates:
            sig = (a_log, kind_col[i], amount_col[i], module_col[i])
            match = outgoing.take(sig, t_col[i], tolerance, target_ok)
            if match is None:
                continue
            dup_col[i] = dup_col[match] = next_group
            next_group += 1
            if src_ship:
                attributions[src["key"]][listener_of[a_log]] += 1
            a_tgt = actors[tgt_col[match]]
            if ship_of(a_tgt):
                attributions[a_tgt["key"]][b_key] += 1
            break

    # 2. Third-party tackle seen by several logs, and "X → Ship" vs "X → you!".
    observed = _Pool()
    for i in range(len(store)):
        if flags_col[i] & Flag.OBSERVED and kind_col[i] in TACKLE and dup_col[i] < 0:
            src, tgt = actors[src_col[i]], actors[tgt_col[i]]
            observed.add((kind_col[i], src.get("raw") or src["key"], tgt.get("raw") or tgt["key"]), t_col[i], i)
    observed.finish()
    for i in range(len(store)):
        if not (flags_col[i] & Flag.OBSERVED) or kind_col[i] not in TACKLE or dup_col[i] >= 0:
            continue
        if i in observed.used:
            continue
        src, tgt = actors[src_col[i]], actors[tgt_col[i]]
        sig = (kind_col[i], src.get("raw") or src["key"], tgt.get("raw") or tgt["key"])
        observed.used.add(i)
        group = None
        group_logs = {log_col[i]}
        while True:
            match = observed.take(sig, t_col[i], tolerance, lambda row, seen=group_logs: log_col[row] not in seen)
            if match is None:
                break
            if group is None:
                group = next_group
                next_group += 1
                dup_col[i] = group
            dup_col[match] = group
            group_logs.add(log_col[match])
    tackle_to_you = _Pool()
    for i in range(len(store)):
        if kind_col[i] in TACKLE and actors[tgt_col[i]]["key"] == listener_of.get(log_col[i]):
            src = actors[src_col[i]]
            tackle_to_you.add((kind_col[i], src.get("raw") or src["key"]), t_col[i], i)
    tackle_to_you.finish()
    for i in range(len(store)):
        if not (flags_col[i] & Flag.OBSERVED) or kind_col[i] not in TACKLE:
            continue
        src, tgt = actors[src_col[i]], actors[tgt_col[i]]
        tgt_ship = ship_of(tgt)
        if not tgt_ship:
            continue

        def victim_ok(row, me=log_col[i], ship=tgt_ship, t=t_col[i]):
            return log_col[row] != me and hulls.compatible(listener_of[log_col[row]], ship, t)

        match = tackle_to_you.take((kind_col[i], src.get("raw") or src["key"]), t_col[i], tolerance, victim_ok)
        if match is None:
            continue
        group = dup_col[match] if dup_col[match] >= 0 else dup_col[i]
        if group < 0:
            group = next_group
            next_group += 1
        dup_col[i] = dup_col[match] = group
        attributions[tgt["key"]][listener_of[log_col[match]]] += 1
    return next_group, attributions
