"""Per-log clock offsets (technical plan §5.3).

Two logs that saw the same interaction (A's "to B" and B's "from A") tell us how far
apart their clocks are. Offsets are solved relative to the best-connected log.
"""

from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict, deque

MIN_MATCHES = 5


def _signature_index(rows):
    index = defaultdict(list)
    for t, sig in rows:
        index[sig].append(t)
    for times in index.values():
        times.sort()
    return index


def pair_offset(a_rows, b_rows, search: int) -> tuple[int, int]:
    """(delta, matches) where delta = t_B - t_A for the same events."""
    index = _signature_index(b_rows)
    hist: Counter = Counter()
    for t, sig in a_rows:
        times = index.get(sig)
        if not times:
            continue
        lo = bisect_left(times, t - search)
        hi = bisect_right(times, t + search)
        for tb in times[lo:hi]:
            hist[tb - t] += 1
    if not hist:
        return 0, 0
    best = max(range(-search, search + 1), key=lambda d: (hist[d - 1] + hist[d] + hist[d + 1], hist[d], -abs(d)))
    return best, hist[best - 1] + hist[best] + hist[best + 1]


def estimate_offsets(store, listener_keys: dict[int, str], search: int) -> tuple[dict[int, int], dict]:
    """Offsets per log id, plus pairwise evidence for display.

    ``store`` is a fight store (with log_id); ``listener_keys`` maps log id → "p:<listener>".
    """
    actors = store.actors
    by_log: dict[int, list] = defaultdict(list)
    t_col, kind_col, src_col, tgt_col = (store.column(c) for c in ("t", "kind", "src", "tgt"))
    amount_col, module_col, log_col = store.column("amount"), store.column("module"), store.column("log_id")
    key_to_log = {key: log_id for log_id, key in listener_keys.items()}
    for i in range(len(store)):
        src_key = actors[src_col[i]]["key"]
        tgt_key = actors[tgt_col[i]]["key"]
        if src_key in key_to_log and tgt_key in key_to_log and src_key != tgt_key:
            sig = (src_key, tgt_key, kind_col[i], amount_col[i], module_col[i])
            by_log[log_col[i]].append((t_col[i], sig))
    logs = list(listener_keys)
    edges: dict[tuple[int, int], tuple[int, int]] = {}
    for i, a in enumerate(logs):
        for b in logs[i + 1 :]:
            delta, matches = pair_offset(by_log.get(a, []), by_log.get(b, []), search)
            if matches >= MIN_MATCHES:
                edges[(a, b)] = (delta, matches)
    offsets = {log_id: 0 for log_id in logs}
    if not edges:
        return offsets, {}
    weight = Counter()
    for (a, b), (_, matches) in edges.items():
        weight[a] += matches
        weight[b] += matches
    reference = max(logs, key=lambda log_id: (weight[log_id], -log_id))
    adjacency = defaultdict(list)
    for (a, b), (delta, _) in edges.items():
        adjacency[a].append((b, delta))
        adjacency[b].append((a, -delta))
    seen = {reference}
    queue = deque([reference])
    while queue:
        a = queue.popleft()
        for b, delta in adjacency[a]:
            if b not in seen:
                offsets[b] = offsets[a] + delta
                seen.add(b)
                queue.append(b)
    evidence = {f"{a}-{b}": {"delta": d, "matches": m} for (a, b), (d, m) in edges.items()}
    return offsets, evidence
