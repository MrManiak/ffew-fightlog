"""Side guessing (technical plan §5.4).

Evidence between groups (alliance, else corp, else the individual):
- hostile: targeted damage, neuts, nos, jams and tackle (+1 per event)
- nothing: AoE damage (smartbombs, bombs) — it hits friend and foe alike
- friendly: reps and cap transfers (+2), shared fleet membership (+5 per relation)
- killmails: victim vs each attacker, weighted by damage share; attackers of the same
  victim are friendly to each other. AoE-suspect attackers are left out.

Groups with Auth-owned characters or configured allies seed the friendly side. A
greedy signed clustering then places every other group, or leaves it Unknown. With no
seed at all, the most active hostile group anchors side 1 and the friendly side stays empty.
Validated against the 2026-10-25 reference fight (review response §2.2).
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from fightlog.parser import Flag
from fightlog.parser.events import FRIENDLY_KINDS, HOSTILE_KINDS

HOSTILE_PER_EVENT = 1.0
FRIENDLY_PER_REP = 2.0
FRIENDLY_PER_FLEET = 5.0
KILLMAIL_HOSTILE = 50.0
KILLMAIL_COATTACK = 30.0
MIN_EVIDENCE = 3.0
FRIENDLY = 0
HINT_AOE_ONLY = "aoe_mutual_only"


@dataclass
class Party:
    key: str
    corp_id: int | None = None
    corp_ticker: str | None = None
    alliance_id: int | None = None
    alliance_ticker: str | None = None
    aa_owned: bool = False

    @property
    def group(self) -> str:
        if self.alliance_id:
            return f"a:{self.alliance_id}"
        if self.alliance_ticker:
            return f"at:{self.alliance_ticker}"
        if self.corp_id:
            return f"c:{self.corp_id}"
        if self.corp_ticker:
            return f"ct:{self.corp_ticker}"
        return f"i:{self.key}"


@dataclass
class KillmailEvidence:
    victim: str | None  # party key
    attackers: list[tuple[str, float, bool]]  # (party key, damage share, aoe_suspect)


@dataclass
class SideResult:
    group_of: dict[str, str]  # party key → group
    side_of_group: dict[str, int]  # group → side index (0 = friendly)
    confidence: dict[str, float]
    hints: dict[str, str]
    friendly_groups: set[str] = field(default_factory=set)

    def side_of(self, key: str) -> int | None:
        group = self.group_of.get(key)
        return self.side_of_group.get(group) if group else None


def collect_interactions(store, parties: set[str]) -> tuple[Counter, Counter, Counter]:
    """(hostile, friendly, aoe) counters over (src key, tgt key), each dup group once."""
    actors = store.actors
    hostile, friendly, aoe = Counter(), Counter(), Counter()
    seen_groups = set()
    has_dup = "dup_group" in store.names
    cols = [store.column(c) for c in ("kind", "src", "tgt", "flags")]
    dup_col = store.column("dup_group") if has_dup else None
    for i in range(len(store)):
        if dup_col is not None and dup_col[i] >= 0:
            if dup_col[i] in seen_groups:
                continue
            seen_groups.add(dup_col[i])
        kind, src, tgt, flags = cols[0][i], cols[1][i], cols[2][i], cols[3][i]
        src_key, tgt_key = actors[src]["key"], actors[tgt]["key"]
        if src_key not in parties or tgt_key not in parties or src_key == tgt_key:
            continue
        if flags & Flag.AOE:
            aoe[(src_key, tgt_key)] += 1
        elif kind in HOSTILE_KINDS:
            hostile[(src_key, tgt_key)] += 1
        elif kind in FRIENDLY_KINDS:
            friendly[(src_key, tgt_key)] += 1
    return hostile, friendly, aoe


def guess_sides(
    parties: dict[str, Party],
    hostile_events: Counter,
    friendly_events: Counter,
    aoe_events: Counter,
    fleet_pairs: list[tuple[str, str]],
    killmails: list[KillmailEvidence],
    friendly_groups: set[str],
    pinned: dict[str, int] | None = None,
    min_confidence: float = 0.3,
) -> SideResult:
    group_of = {key: party.group for key, party in parties.items()}
    hostile: dict[tuple[str, str], float] = defaultdict(float)
    friendly: dict[tuple[str, str], float] = defaultdict(float)
    aoe_dir: Counter = Counter()

    def pair(a, b):
        return (a, b) if a <= b else (b, a)

    def groups(a_key, b_key):
        a, b = group_of.get(a_key), group_of.get(b_key)
        if a is None or b is None or a == b:
            return None
        return a, b

    for (src, tgt), n in hostile_events.items():
        g = groups(src, tgt)
        if g:
            hostile[pair(*g)] += n * HOSTILE_PER_EVENT
    for (src, tgt), n in friendly_events.items():
        g = groups(src, tgt)
        if g:
            friendly[pair(*g)] += n * FRIENDLY_PER_REP
    for (src, tgt), n in aoe_events.items():
        g = groups(src, tgt)
        if g:
            aoe_dir[g] += n
    for a, b in fleet_pairs:
        g = groups(a, b)
        if g:
            friendly[pair(*g)] += FRIENDLY_PER_FLEET
    log_hostile = dict(hostile)
    coattack: dict[tuple[str, str], float] = defaultdict(float)
    for km in killmails:
        kept = [(group_of[k], share) for k, share, suspect in km.attackers if not suspect and k in group_of]
        victim_group = group_of.get(km.victim) if km.victim else None
        if victim_group:
            for g, share in kept:
                if g != victim_group:
                    hostile[pair(victim_group, g)] += KILLMAIL_HOSTILE * share
        for i, (g1, s1) in enumerate(kept):
            for g2, s2 in kept[i + 1 :]:
                if g1 != g2:
                    coattack[pair(g1, g2)] += KILLMAIL_COATTACK * min(s1, s2)
    for key, value in coattack.items():
        # Shooting the same third party doesn't make two groups friends when our logs
        # show them shooting each other.
        if log_hostile.get(key, 0.0) < MIN_EVIDENCE:
            friendly[key] += value

    all_groups = set(group_of.values())
    seeds = {g for key, g in group_of.items() if parties[key].aa_owned} | (friendly_groups & all_groups)
    pinned_groups: dict[str, int] = {}
    for key, side in (pinned or {}).items():
        if key in group_of:
            pinned_groups[group_of[key]] = side
    side_of: dict[str, int] = {g: FRIENDLY for g in seeds}
    side_of.update(pinned_groups)
    fixed = set(side_of)

    def w(table, a, b):
        return table.get(pair(a, b), 0.0)

    totals = Counter()
    for (a, b), v in list(hostile.items()) + list(friendly.items()):
        totals[a] += v
        totals[b] += v
    order = sorted((g for g in all_groups if g not in fixed), key=lambda g: (-totals[g], g))
    anchors = set()
    if not side_of:
        # Nobody we know took part (logs uploaded for others): anchor the most active group
        # with enough hostile evidence on side 1, so the rest can be placed around it.
        hostile_weight = Counter()
        for (a, b), v in hostile.items():
            hostile_weight[a] += v
            hostile_weight[b] += v
        first = next((g for g in order if hostile_weight[g] >= MIN_EVIDENCE), None)
        if first is not None:
            side_of[first] = 1
            anchors.add(first)
    n_sides = max(side_of.values(), default=0) + 1
    for _ in range(3):
        for g in order:
            if g in anchors:
                continue
            scores: dict[int, float] = defaultdict(float)
            hostile_total = 0.0
            for other, side in side_of.items():
                if other == g:
                    continue
                scores[side] += w(friendly, g, other) - w(hostile, g, other)
                hostile_total += w(hostile, g, other)
            if totals[g] < MIN_EVIDENCE:
                side_of.pop(g, None)
                continue
            ranked = sorted(scores.items(), key=lambda kv: -kv[1])
            best_side, best = ranked[0] if ranked else (None, -1.0)
            if best > 0:
                side_of[g] = best_side
            elif hostile_total >= MIN_EVIDENCE:
                neutral = [s for s, v in ranked if s != FRIENDLY and v >= 0]
                if neutral:
                    side_of[g] = neutral[0]
                else:
                    current = side_of.get(g)
                    if current is None or current == FRIENDLY:
                        side_of[g] = n_sides
                        n_sides += 1
            else:
                side_of.pop(g, None)

    # Confidence: evidence consistent with the placement (friendly with its own side,
    # hostile to the others) against evidence that contradicts it.
    confidence: dict[str, float] = {g: 1.0 for g in fixed}
    for g in list(side_of):
        if g in fixed:
            continue
        support = against = 0.0
        for other, side in side_of.items():
            if other == g:
                continue
            if side == side_of[g]:
                support += w(friendly, g, other)
                against += w(hostile, g, other)
            else:
                support += w(hostile, g, other)
                against += w(friendly, g, other)
        total = support + against
        confidence[g] = max(0.0, (support - against) / total) if total else 0.0
        if confidence[g] < min_confidence:
            side_of.pop(g)
    # Renumber hostile sides 1..n in order of size.
    sizes = Counter(side for g, side in side_of.items() if side != FRIENDLY)
    renumber = {side: i + 1 for i, (side, _) in enumerate(sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0])))}
    side_of = {g: (FRIENDLY if s == FRIENDLY else renumber[s]) for g, s in side_of.items()}

    friendly_set = {g for g, s in side_of.items() if s == FRIENDLY}
    hints: dict[str, str] = {}
    for g in all_groups - set(side_of):
        mutual = any(aoe_dir[(g, f)] and aoe_dir[(f, g)] for f in friendly_set)
        touched = any(w(hostile, g, f) or w(friendly, g, f) for f in friendly_set)
        if mutual and not touched:
            hints[g] = HINT_AOE_ONLY
    return SideResult(group_of, side_of, confidence, hints, friendly_set)
