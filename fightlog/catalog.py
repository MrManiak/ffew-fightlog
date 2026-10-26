"""The bundled EVE type catalog (built from CCP's SDE by tools/build_type_catalog.py).

Pure Python: the parser uses it without Django.
"""

import gzip
import json
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

CAT_SHIP = 6
CAT_MODULE = 7
CAT_CHARGE = 8
CAT_ENTITY = 11
CAT_DRONE = 18
CAT_DEPLOYABLE = 22
CAT_STARBASE = 23
CAT_SOVEREIGNTY = 40
CAT_ORBITAL = 46
CAT_STRUCTURE = 65
CAT_STRUCTURE_MODULE = 66
CAT_FIGHTER = 87

STRUCTURE_CATEGORIES = frozenset({CAT_DEPLOYABLE, CAT_STARBASE, CAT_SOVEREIGNTY, CAT_ORBITAL, CAT_STRUCTURE})

# Weapons whose damage lands on everything around them. Their hits are never
# evidence of which side anyone is on (technical plan §5.4).
AOE_GROUP_KEYWORDS = ("Smart Bomb", "Burst Jammer", "Burst Projector", "Super Weapon", "Doomsday", "Area Denial")
AOE_GROUP_NAMES = frozenset({"Bomb", "Bomb ECM", "Bomb Energy"})
# Weapons that can miss, so hit % means something for them.
TURRET_GROUP_NAMES = frozenset({"Energy Weapon", "Projectile Weapon", "Hybrid Weapon", "Precursor Weapon"})
# Names old logs use for types that have since been renamed.
LEGACY_NAMES = {
    "Ansiblex Jump Gate": "Ansiblex Jump Bridge",
}


@dataclass(frozen=True)
class TypeInfo:
    type_id: int
    name: str
    group_id: int
    group: str
    category_id: int
    published: bool


@dataclass(frozen=True)
class SystemInfo:
    system_id: int
    name: str
    security: float
    constellation_id: int
    constellation: str
    region_id: int
    region: str

    @property
    def is_wspace(self) -> bool:
        return 31000000 <= self.system_id < 32000000


class Catalog:
    def __init__(self, data: dict):
        self.sde_build = data.get("sde_build")
        groups = {int(k): (v[0], int(v[1])) for k, v in data["groups"].items()}
        self._charge_groups = {name for name, category_id in groups.values() if category_id == CAT_CHARGE}
        self._by_name: dict[str, list[TypeInfo]] = {}
        self._by_lower: dict[str, list[TypeInfo]] = {}
        self._by_id: dict[int, TypeInfo] = {}
        for type_id, name, group_id, published in data["types"]:
            group_name, category_id = groups[group_id]
            info = TypeInfo(type_id, name, group_id, group_name, category_id, bool(published))
            self._by_name.setdefault(name, []).append(info)
            self._by_lower.setdefault(name.lower(), []).append(info)
            self._by_id[type_id] = info
        regions = {int(k): v for k, v in data.get("regions", {}).items()}
        constellations = {int(k): v for k, v in data.get("constellations", {}).items()}
        self._systems: dict[int, SystemInfo] = {}
        self._systems_by_name: dict[str, SystemInfo] = {}
        for system_id, name, security, constellation_id, region_id in data.get("systems", []):
            info = SystemInfo(
                system_id,
                name,
                security,
                constellation_id,
                constellations.get(constellation_id, ""),
                region_id,
                regions.get(region_id, ""),
            )
            self._systems[system_id] = info
            self._systems_by_name[name.lower()] = info
        self._prefix_cache: dict[str, tuple] = {}

    @property
    def type_count(self) -> int:
        return len(self._by_id)

    # types

    def get(self, name: str | None) -> list[TypeInfo]:
        if not name:
            return []
        found = self._by_name.get(name) or self._by_lower.get(name.lower())
        if found:
            return found
        legacy = LEGACY_NAMES.get(name)
        return self._by_name.get(legacy, []) if legacy else []

    def by_id(self, type_id: int | None) -> TypeInfo | None:
        return self._by_id.get(type_id) if type_id else None

    def categories(self, name: str | None) -> set[int]:
        return {info.category_id for info in self.get(name)}

    def has_category(self, name: str | None, *category_ids: int) -> bool:
        return bool(self.categories(name) & set(category_ids))

    def is_npc_name(self, name: str | None) -> bool:
        return self.has_category(name, CAT_ENTITY)

    def is_player_hull(self, name: str | None) -> bool:
        return any(i.category_id == CAT_SHIP and i.published for i in self.get(name))

    def is_structure(self, name: str | None) -> bool:
        return bool(self.categories(name) & STRUCTURE_CATEGORIES)

    def is_known(self, name: str | None) -> bool:
        return bool(self.get(name))

    def is_charge_group(self, name: str | None) -> bool:
        return bool(name) and name in self._charge_groups

    def is_weapon_name(self, name: str | None) -> bool:
        """Something that can appear in the weapon slot of a damage line."""
        return self.has_category(name, CAT_MODULE, CAT_CHARGE, CAT_DRONE, CAT_FIGHTER, CAT_STRUCTURE_MODULE)

    def is_player_module(self, name: str | None) -> bool:
        return self.has_category(name, CAT_MODULE, CAT_STRUCTURE_MODULE)

    def is_aoe(self, name: str | None) -> bool:
        for info in self.get(name):
            if info.group in AOE_GROUP_NAMES or any(k in info.group for k in AOE_GROUP_KEYWORDS):
                return True
        return False

    def can_miss(self, name: str | None) -> bool:
        """Turrets, drones and fighters can miss; missiles, smartbombs and bombs can't."""
        for info in self.get(name):
            if info.category_id in (CAT_DRONE, CAT_FIGHTER) or info.group in TURRET_GROUP_NAMES:
                return True
        return False

    def is_drone_weapon(self, name: str | None) -> bool:
        return self.has_category(name, CAT_DRONE, CAT_FIGHTER)

    def longest_type_prefix(self, text: str) -> tuple[TypeInfo | None, str]:
        """Split 'Brutix Pilotecho98' into (Brutix, 'Pilotecho98')."""
        cached = self._prefix_cache.get(text)
        if cached is not None:
            return cached
        words = text.split(" ")
        result: tuple[TypeInfo | None, str] = (None, text)
        for k in range(len(words), 0, -1):
            infos = self._by_name.get(" ".join(words[:k]))
            if infos:
                result = (_preferred(infos), " ".join(words[k:]).strip())
                break
        if len(self._prefix_cache) < 50000:
            self._prefix_cache[text] = result
        return result

    # systems

    def system(self, system_id: int | None) -> SystemInfo | None:
        return self._systems.get(system_id) if system_id else None

    def system_by_name(self, name: str | None) -> SystemInfo | None:
        return self._systems_by_name.get(name.lower()) if name else None


def _preferred(infos: list[TypeInfo]) -> TypeInfo:
    """Prefer published player types over NPC look-alikes with the same name."""
    return sorted(infos, key=lambda i: (not i.published, i.category_id == CAT_ENTITY, i.type_id))[0]


@lru_cache(maxsize=1)
def load_catalog() -> Catalog:
    raw = resources.files("fightlog").joinpath("data/types.json.gz").read_bytes()
    return Catalog(json.loads(gzip.decompress(raw)))
