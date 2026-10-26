"""Build anonymized test fixtures from real gamelogs.

Usage:
    python tools/anonymize.py fight <out_dir> <zkill.json> <log> [<log> ...]
    python tools/anonymize.py shapes <out_file> <Gamelogs folder>

``fight`` rewrites a set of logs of one fight plus its zKillboard killmails so that
every pilot name, character ID, ship name and player-structure name is replaced by
a stable fake, consistently across logs, killmails and the ESI mock it writes
(esi.json). Corporation and alliance tickers and names stay real: they are public
organisation data the side-guessing tests depend on.

``shapes`` writes one anonymized raw line for every combat line shape in a corpus.
Real logs never leave your machine; only the outputs of this tool are committed.
"""

import gzip
import json
import re
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fightlog.catalog import load_catalog  # noqa: E402
from fightlog.parser import ActorKind, ContextKind, parse_log  # noqa: E402
from fightlog.parser.markup import clean  # noqa: E402
from fightlog.parser.patterns import skeleton  # noqa: E402

UA = "ffew-fightlog fixture builder (+https://github.com/MrManiak/ffew-fightlog)"
FAKE_ID_BASE = 90000000
SHOWINFO_RE = re.compile(r"(showinfo:\d+//)(\d+)")
QUOTED_RE = re.compile(r"“[^”]*”")
# The timestamp and a line's leading amount ("<b>351</b>", "<b>+8 GJ</b>"): never names.
LEAD_RE = re.compile(r"^\[[^\]]*\] \(\w+\) (?:(?:<[^>]*>)*[+-]?\d+(?: GJ)?)?")


def esi(path: str, body=None):
    req = urllib.request.Request(
        "https://esi.evetech.net/" + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"User-Agent": UA, "X-Compatibility-Date": "2026-05-19", "Content-Type": "application/json"},
        method="POST" if body is not None else "GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # nosec - fixed ESI host
        return json.load(resp)


class Anonymizer:
    def __init__(self):
        self.catalog = load_catalog()
        self.names: dict[str, str] = {}
        self.ids: dict[int, int] = {}
        self.structures: dict[str, str] = {}

    def fake_name(self, real: str) -> str:
        if real not in self.names:
            self.names[real] = f"Pilot {len(self.names) + 1:03d}"
        return self.names[real]

    def fake_id(self, real: int) -> int:
        if real not in self.ids:
            self.ids[real] = FAKE_ID_BASE + len(self.ids) + 1
        return self.ids[real]

    def collect(self, parsed) -> None:
        def keep(name: str | None) -> bool:
            if not name or len(name) < 3:
                return False  # EVE names have at least 3 characters
            return not self.catalog.is_known(name) and not self.catalog.is_npc_name(name)

        if keep(parsed.listener):
            self.fake_name(parsed.listener)
        for event in parsed.events:
            for actor in (event.src, event.tgt):
                if actor.kind in (ActorKind.PILOT, ActorKind.LISTENER, ActorKind.UNKNOWN) and keep(actor.name):
                    self.fake_name(actor.name)
                if actor.owner and keep(actor.owner):
                    self.fake_name(actor.owner)
                if actor.kind == ActorKind.STRUCTURE and actor.name and actor.name != actor.ship and keep(actor.name):
                    self.structures.setdefault(actor.name, f"Structure {len(self.structures) + 1:02d}")
        for ctx in parsed.context:
            if (
                ctx.name
                and keep(ctx.name)
                and (ctx.kind in ContextKind.FLEET_KINDS or ctx.kind == ContextKind.KILL_NOTICE)
            ):
                self.fake_name(ctx.name)

    def rewrite(self, text: str) -> str:
        replacements = sorted(list(self.names.items()) + list(self.structures.items()), key=lambda kv: -len(kv[0]))
        mapping = dict(replacements)
        parts = []
        for real, _ in replacements:
            if real.isdigit():
                # All-digit names: also keep clear of dates, times and "(N deflected)".
                parts.append(r"(?<![A-Za-z0-9.:+-])" + real + r"(?![A-Za-z0-9.:])(?! deflected)")
            else:
                parts.append(r"(?<![A-Za-z0-9])" + re.escape(real) + r"(?![A-Za-z0-9])")
        if parts:
            pattern = re.compile("|".join(parts))
            lines = []
            for line in text.split("\n"):
                # One pass per line, so a fake name is never rewritten again.
                lead = LEAD_RE.match(line)
                cut = lead.end() if lead else 0
                lines.append(line[:cut] + pattern.sub(lambda m: mapping[m.group(0)], line[cut:]))
            text = "\n".join(lines)
        text = SHOWINFO_RE.sub(lambda m: m.group(1) + str(self.fake_id(int(m.group(2)))), text)
        return QUOTED_RE.sub("“Ship”", text)


def fight(out_dir: Path, zkill_path: Path, logs: list[Path]) -> None:
    anon = Anonymizer()
    parsed = []
    for path in logs:
        data = path.read_bytes()
        p = parse_log(data, anon.catalog)
        parsed.append((path, data, p))
        anon.collect(p)
    out_dir.mkdir(parents=True, exist_ok=True)
    real_chars: dict[str, int] = {}
    names = sorted(anon.names)
    for i in range(0, len(names), 500):
        for row in esi("universe/ids", names[i : i + 500]).get("characters") or []:
            real_chars[row["name"]] = row["id"]
    for real, fake in anon.names.items():
        if real in real_chars:
            anon.ids.setdefault(real_chars[real], anon.fake_id(real_chars[real]))
    for path, data, p in parsed:
        m = re.search(r"_(\d+)\.txt$", path.name)
        fake_cid = anon.fake_id(int(m.group(1))) if m else 0
        name = re.sub(r"_(\d+)\.txt$", f"_{fake_cid}.txt", path.name)
        text = anon.rewrite(data.decode("utf-8-sig"))
        with gzip.open(out_dir / (name + ".gz"), "wt", encoding="utf-8", newline="") as fh:
            fh.write(text)
    # Killmails in the fight's hour, with character IDs replaced.
    kms = json.loads(zkill_path.read_text(encoding="utf-8"))
    times = [e.t for _, _, p in parsed for e in p.events]
    lo, hi = min(times) - 3600, max(times) + 1800
    from datetime import datetime

    window = [
        km for km in kms if lo <= datetime.fromisoformat(km["killmail_time"].replace("Z", "+00:00")).timestamp() <= hi
    ]
    char_ids = sorted({e.get("character_id") for km in window for e in [km["victim"]] + km["attackers"]} - {None})
    id_names = {}
    for i in range(0, len(char_ids), 900):
        for row in esi("universe/names", char_ids[i : i + 900]):
            id_names[row["id"]] = row["name"]
    for cid in char_ids:
        real_name = id_names.get(cid)
        if real_name:
            anon.fake_name(real_name)
            real_chars.setdefault(real_name, cid)
    for km in window:
        for e in [km["victim"]] + km["attackers"]:
            if e.get("character_id"):
                e["character_id"] = anon.fake_id(e["character_id"])
    with gzip.open(out_dir / "zkill.json.gz", "wt", encoding="utf-8") as fh:
        json.dump(window, fh)
    # ESI mock: fake characters with their real affiliations, plus real corps and alliances.
    affiliations = {}
    ids = sorted(set(real_chars.values()))
    for i in range(0, len(ids), 1000):
        for row in esi("characters/affiliation", ids[i : i + 1000]):
            affiliations[row["character_id"]] = row
    characters = {}
    corp_ids, alliance_ids = set(), set()
    for real, fake in anon.names.items():
        cid = real_chars.get(real)
        if not cid:
            continue
        aff = affiliations.get(cid, {})
        characters[fake] = {
            "id": anon.fake_id(cid),
            "corporation_id": aff.get("corporation_id"),
            "alliance_id": aff.get("alliance_id"),
        }
        corp_ids.add(aff.get("corporation_id"))
        alliance_ids.add(aff.get("alliance_id"))
    for km in window:
        for e in [km["victim"]] + km["attackers"]:
            corp_ids.add(e.get("corporation_id"))
            alliance_ids.add(e.get("alliance_id"))
    corporations = {}
    for corp_id in sorted(c for c in corp_ids if c):
        data = esi(f"corporations/{corp_id}")
        corporations[str(corp_id)] = {
            "ticker": data["ticker"],
            "name": data["name"],
            "alliance_id": data.get("alliance_id"),
        }
        if data.get("alliance_id"):
            alliance_ids.add(data["alliance_id"])
    alliances = {}
    for alliance_id in sorted(a for a in alliance_ids if a):
        data = esi(f"alliances/{alliance_id}")
        alliances[str(alliance_id)] = {"ticker": data["ticker"], "name": data["name"]}
    (out_dir / "esi.json").write_text(
        json.dumps(
            {"characters": characters, "corporations": corporations, "alliances": alliances}, indent=1, sort_keys=True
        ),
        encoding="utf-8",
        newline="\n",
    )
    print(f"{len(logs)} logs, {len(window)} killmails, {len(anon.names)} pilots anonymized → {out_dir}")


def shapes(out_file: Path, folder: Path) -> None:
    anon = Anonymizer()
    picked: dict[str, tuple[str, str]] = {}
    for path in sorted(folder.glob("*_*_*.txt")):
        data = path.read_bytes()
        text = data.decode("utf-8", errors="replace")
        new_shapes = []
        for line in text.split("\n"):
            if "(combat)" not in line:
                continue
            msg = line.split("(combat) ", 1)[-1]
            shape = skeleton(clean(msg))
            if shape not in picked:
                new_shapes.append((shape, line.rstrip("\r")))
        if new_shapes:
            p = parse_log(data, anon.catalog)
            anon.collect(p)
            for shape, line in new_shapes:
                picked.setdefault(shape, (path.name, line))
    header = (
        "------------------------------------------------------------\n"
        "  Gamelog\n  Listener: Pilot Listener\n  Session Started: 2026.01.01 00:00:00\n"
        "------------------------------------------------------------\n"
    )
    lines = [anon.rewrite(line) for _, (_, line) in sorted(picked.items())]
    out_file.parent.mkdir(parents=True, exist_ok=True)
    body = (header + "\n".join(lines) + "\n").encode("utf-8")
    out_file.write_bytes(gzip.compress(body, mtime=0) if out_file.suffix == ".gz" else body)
    print(f"{len(lines)} shapes → {out_file}")


if __name__ == "__main__":
    if len(sys.argv) >= 5 and sys.argv[1] == "fight":
        fight(Path(sys.argv[2]), Path(sys.argv[3]), [Path(p) for p in sys.argv[4:]])
    elif len(sys.argv) == 4 and sys.argv[1] == "shapes":
        shapes(Path(sys.argv[2]), Path(sys.argv[3]))
    else:
        print(__doc__)
        sys.exit(2)
