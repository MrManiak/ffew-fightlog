"""Hull timelines: which ship each pilot flew when (technical plan §5.2).

A listener's own log never names their hull. Other logs' labels and killmails do.
"""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass

NEAR_SECONDS = 900  # an observation this close to t still counts when nothing brackets t


@dataclass
class HullObservation:
    first: int
    last: int
    ship: str
    source: str  # peer_log, killmail, inferred


class HullTimeline:
    def __init__(self):
        self._obs: dict[str, list[HullObservation]] = defaultdict(list)
        self._starts: dict[str, list[int]] = {}

    def add(self, key: str, ship: str, first: int, last: int, source: str) -> None:
        if not ship:
            return
        self._obs[key].append(HullObservation(first, last, ship, source))
        self._starts.pop(key, None)

    def _sorted(self, key: str) -> list[HullObservation]:
        if key not in self._starts:
            self._obs[key].sort(key=lambda o: (o.first, o.last))
            self._starts[key] = [o.first for o in self._obs[key]]
        return self._obs[key]

    def known(self, key: str) -> bool:
        return bool(self._obs.get(key))

    def at(self, key: str, t: int) -> str | None:
        obs = self._sorted(key) if key in self._obs else []
        if not obs:
            return None
        starts = self._starts[key]
        i = bisect_right(starts, t) - 1
        before = obs[i] if i >= 0 else None
        after = obs[i + 1] if i + 1 < len(obs) else None
        if before and before.first <= t <= before.last:
            return before.ship
        if before and after and before.ship == after.ship:
            return before.ship
        candidates = []
        if before and t - before.last <= NEAR_SECONDS:
            candidates.append((t - before.last, before.ship))
        if after and after.first - t <= NEAR_SECONDS:
            candidates.append((after.first - t, after.ship))
        if candidates:
            return min(candidates)[1]
        if len({o.ship for o in obs}) == 1:
            return obs[0].ship
        return None

    def compatible(self, key: str, ship: str | None, t: int) -> bool:
        """Could this pilot have been in that ship at t? Unknown hulls are compatible."""
        if not ship:
            return True
        current = self.at(key, t)
        return current is None or current == ship

    def flew(self, key: str, ship: str) -> bool:
        return any(o.ship == ship for o in self._obs.get(key, []))

    def intervals(self, key: str) -> list[HullObservation]:
        """Merge consecutive observations of the same ship."""
        merged: list[HullObservation] = []
        for obs in self._sorted(key) if key in self._obs else []:
            if merged and merged[-1].ship == obs.ship:
                last = merged[-1]
                last.last = max(last.last, obs.last)
                if obs.source == "peer_log":
                    last.source = "peer_log"
            else:
                merged.append(HullObservation(obs.first, obs.last, obs.ship, obs.source))
        return merged

    def keys(self):
        return list(self._obs)
