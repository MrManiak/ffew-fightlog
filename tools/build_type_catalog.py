"""Build fightlog/data/types.json.gz from CCP's static data export (SDE, JSONL format).

Usage:
    python tools/build_type_catalog.py                 # downloads the latest SDE
    python tools/build_type_catalog.py path/to/sde.zip # uses a local copy

The catalog holds the English names of every type the combat-log parser needs to
classify (ships, modules, charges, drones, fighters, deployables, structures,
starbases, orbitals and NPC entities), plus solar systems, constellations and
regions. Run it before each release and commit the result.
"""

import gzip
import io
import json
import sys
import urllib.request
import zipfile
from pathlib import Path

SDE_URL = "https://developers.eveonline.com/static-data/eve-online-static-data-latest-jsonl.zip"
USER_AGENT = "ffew-fightlog catalog builder (+https://github.com/MrManiak/ffew-fightlog)"
OUT = Path(__file__).resolve().parent.parent / "fightlog" / "data" / "types.json.gz"

# Ship, Module, Charge, Entity (NPCs), Drone, Deployable, Starbase, Sovereignty
# Structures, Orbitals, Structure, Structure Module, Fighter
CATEGORIES = {6, 7, 8, 11, 18, 22, 23, 40, 46, 65, 66, 87}


def _rows(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as fh:
        for line in io.TextIOWrapper(fh, encoding="utf-8"):
            line = line.strip()
            if line:
                yield json.loads(line)


def _en(record: dict) -> str:
    name = record.get("name") or {}
    return name.get("en", "") if isinstance(name, dict) else str(name)


def build(zip_bytes: bytes) -> dict:
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    sde = next(_rows(zf, "_sde.jsonl"))
    categories = {r["_key"]: _en(r) for r in _rows(zf, "categories.jsonl") if r["_key"] in CATEGORIES}
    groups = {
        r["_key"]: [_en(r), r["categoryID"]] for r in _rows(zf, "groups.jsonl") if r.get("categoryID") in CATEGORIES
    }
    types = []
    for r in _rows(zf, "types.jsonl"):
        group_id = r.get("groupID")
        if group_id not in groups:
            continue
        name = _en(r).strip()
        if not name:
            continue
        types.append([r["_key"], name, group_id, 1 if r.get("published") else 0])
    regions = {r["_key"]: _en(r) for r in _rows(zf, "mapRegions.jsonl")}
    constellations = {r["_key"]: _en(r) for r in _rows(zf, "mapConstellations.jsonl")}
    systems = [
        [
            r["_key"],
            _en(r),
            round(float(r.get("securityStatus", 0.0)), 3),
            r.get("constellationID"),
            r.get("regionID"),
        ]
        for r in _rows(zf, "mapSolarSystems.jsonl")
    ]
    return {
        "version": 1,
        "sde_build": sde.get("buildNumber"),
        "sde_release": sde.get("releaseDate"),
        "categories": categories,
        "groups": groups,
        "types": sorted(types),
        "regions": regions,
        "constellations": constellations,
        "systems": sorted(systems),
    }


def main(argv: list[str]) -> int:
    if len(argv) > 1:
        data = Path(argv[1]).read_bytes()
    else:
        req = urllib.request.Request(SDE_URL, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=300) as resp:  # nosec - fixed CCP URL
            data = resp.read()
    catalog = build(data)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(catalog, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    with gzip.GzipFile(OUT, "wb", mtime=0) as fh:
        fh.write(payload)
    print(
        f"wrote {OUT} ({OUT.stat().st_size // 1024} KB): sde build {catalog['sde_build']}, "
        f"{len(catalog['types'])} types, {len(catalog['groups'])} groups, {len(catalog['systems'])} systems"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
