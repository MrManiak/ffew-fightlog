"""Shared test helpers: fixtures, fake ESI and zKillboard, users with characters."""

import gzip
import json
from contextlib import ExitStack, contextmanager
from datetime import datetime
from datetime import timezone as dt_timezone
from pathlib import Path
from unittest import mock

from allianceauth.authentication.models import CharacterOwnership
from allianceauth.eveonline.models import EveCharacter
from app_utils.testdata_factories import UserMainFactory
from app_utils.testing import add_character_to_user

FIXTURES = Path(__file__).resolve().parent / "fixtures"
FIGHT_DIR = FIXTURES / "fight-2026-10-25"
SAMPLE_LOG = "20261025_195145_90000001.txt.gz"
FIVE_LOGS = [
    "20261025_195145_90000001.txt.gz",
    "20261025_195149_90000006.txt.gz",
    "20261025_195425_90000007.txt.gz",
    "20261025_195427_90000013.txt.gz",
    "20261025_195433_90000070.txt.gz",
]
FFEW_CORP = 840323545
WEFEW_ALLIANCE = 99011699
FIGHT_DAY_AFTER = datetime(2026, 10, 26, 12, 0, tzinfo=dt_timezone.utc)

ALL_PERMS = [
    "fightlog.basic_access",
    "fightlog.upload_any_log",
    "fightlog.curate_fights",
    "fightlog.post_to_discord",
]


def fixture_bytes(name: str) -> bytes:
    path = FIGHT_DIR / name if (FIGHT_DIR / name).exists() else FIXTURES / name
    data = path.read_bytes()
    return gzip.decompress(data) if path.suffix == ".gz" else data


def fixture_filename(name: str) -> str:
    return name[:-3] if name.endswith(".gz") else name


def esi_data() -> dict:
    return json.loads((FIGHT_DIR / "esi.json").read_text(encoding="utf-8"))


def zkill_data() -> list[dict]:
    return json.loads(gzip.decompress((FIGHT_DIR / "zkill.json.gz").read_bytes()))


class FakeEsi:
    """Answers the resolve module's ESI wrappers from the fixture's esi.json."""

    def __init__(self, data: dict | None = None):
        self.data = data or esi_data()
        self.by_id = {c["id"]: name for name, c in self.data["characters"].items()}
        self.calls = 0

    def universe_ids(self, names):
        self.calls += 1
        out = {"characters": [], "corporations": [], "alliances": []}
        lowered = {n.lower(): n for n in names}
        for name, c in self.data["characters"].items():
            if name.lower() in lowered:
                out["characters"].append({"id": c["id"], "name": name})
        for corp_id, c in self.data["corporations"].items():
            if c["name"].lower() in lowered:
                out["corporations"].append({"id": int(corp_id), "name": c["name"]})
        for alliance_id, a in self.data["alliances"].items():
            if a["name"].lower() in lowered:
                out["alliances"].append({"id": int(alliance_id), "name": a["name"]})
        return out

    def universe_names(self, ids):
        self.calls += 1
        return [{"id": i, "name": self.by_id[i], "category": "character"} for i in ids if i in self.by_id]

    def affiliations(self, ids):
        self.calls += 1
        out = []
        for i in ids:
            name = self.by_id.get(i)
            if name:
                c = self.data["characters"][name]
                row = {"character_id": i, "corporation_id": c["corporation_id"]}
                if c.get("alliance_id"):
                    row["alliance_id"] = c["alliance_id"]
                out.append(row)
        return out

    def corporation(self, corp_id):
        self.calls += 1
        return self.data["corporations"][str(corp_id)]

    def alliance(self, alliance_id):
        self.calls += 1
        return self.data["alliances"][str(alliance_id)]

    def system(self, system_id):
        raise RuntimeError("systems come from the catalog in tests")

    @contextmanager
    def patched(self):
        with mock.patch.multiple(
            "fightlog.services.resolve",
            esi_universe_ids=self.universe_ids,
            esi_universe_names=self.universe_names,
            esi_affiliations=self.affiliations,
            esi_corporation=self.corporation,
            esi_alliance=self.alliance,
            esi_system=self.system,
        ):
            yield self


class FakeZkb:
    """Answers zkb_get from the recorded killmails: everything on page 1, nothing after."""

    def __init__(self, killmails=None):
        self.killmails = killmails if killmails is not None else zkill_data()
        self.paths = []

    def get(self, path):
        self.paths.append(path)
        if "/page/1" not in path and "page" in path:
            return []
        return [dict(km) for km in self.killmails]

    @contextmanager
    def patched(self):
        with mock.patch("fightlog.services.zkb.zkb_get", self.get):
            yield self


@contextmanager
def offline(zkb: bool = False):
    """Fake ESI, and zKillboard either faked or disabled."""
    with ExitStack() as stack:
        esi = stack.enter_context(FakeEsi().patched())
        if zkb:
            stack.enter_context(FakeZkb().patched())
        else:
            stack.enter_context(mock.patch("fightlog.app_settings.FIGHTLOG_ZKB_ENABLED", False))
        yield esi


def make_character(
    character_id: int,
    name: str,
    corp_id: int = FFEW_CORP,
    corp_ticker: str = "FFEW",
    alliance_id: int | None = WEFEW_ALLIANCE,
    alliance_ticker: str = "WEFEW",
) -> EveCharacter:
    character, _ = EveCharacter.objects.get_or_create(
        character_id=character_id,
        defaults={
            "character_name": name,
            "corporation_id": corp_id,
            "corporation_name": corp_ticker,
            "corporation_ticker": corp_ticker,
            "alliance_id": alliance_id,
            "alliance_name": alliance_ticker,
            "alliance_ticker": alliance_ticker,
        },
    )
    return character


def make_user(username: str, characters: list[tuple[int, str]] | None = None, permissions=None):
    """A user whose main is the first character; the rest are alts."""
    characters = characters or [(91000000 + abs(hash(username)) % 900000, f"{username} main")]
    main = make_character(*characters[0])
    user = UserMainFactory(
        username=username,
        main_character__character=main,
        permissions=["fightlog.basic_access"] if permissions is None else permissions,
    )
    for cid, name in characters[1:]:
        add_character_to_user(user, make_character(cid, name))
    return user


def fleet_owner(username: str = "owner", permissions=None):
    """The user who multiboxed the reference fight: owns all five listeners."""
    return make_user(
        username,
        [
            (90000001, "Pilot 001"),
            (90000006, "Pilot 006"),
            (90000007, "Pilot 007"),
            (90000013, "Pilot 013"),
            (90000070, "Pilot 072"),
        ],
        permissions=permissions,
    )


def owns(user, character_id: int) -> bool:
    return CharacterOwnership.objects.filter(user=user, character__character_id=character_id).exists()


def ingest_fixture(user, names, process: bool = True):
    """Upload fixture logs as ``user``; optionally parse and rebuild right away."""
    from fightlog.services import ingest
    from fightlog.services.rebuild import rebuild_fight

    logs = []
    affected = set()
    for name in names:
        result = ingest.accept_upload(user, fixture_filename(name), fixture_bytes(name))
        assert result.ok, result.message
        logs.append(result.log)
        if process:
            result.log.refresh_from_db()
            affected |= ingest.process_log(result.log)
    if process:
        for fight_id in sorted(affected):
            rebuild_fight(fight_id)
    return logs, affected
