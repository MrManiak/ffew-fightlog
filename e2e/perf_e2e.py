"""Technical plan M4: a synthetic 50-log fight on the dev stack, timed through the HTTP API.

    python e2e/perf_e2e.py

Builds 50 gamelogs for a 300-pilot battle (about 250k combat lines, a sixth of the hits seen from
both ends), uploads them as the admin, waits for the fight to build, then times the analysis
endpoint: the first uncached request must take under 2 s and a cached one under 200 ms.
"""

import random
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client import Client, django_shell  # noqa: E402

START = datetime(2026, 10, 20, 18, 0, 0, tzinfo=timezone.utc)
SECONDS = 1200
EVENTS_PER_SECOND = 178
LISTENERS_PER_SIDE = 25
PILOTS_PER_SIDE = 150
FIRST_ID = 95000001

HEADER = (
    "------------------------------------------------------------\n"
    "  Gamelog\n  Listener: {name}\n  Session Started: 2026.10.20 17:55:00\n"
    "------------------------------------------------------------\n"
)
DMG_OUT = (
    "<color=0xff00ffff><b>{n}</b> <color=0x77ffffff><font size=10>to</font> <b><color=0xffffffff>{label}</b>"
    "<font size=10><color=0x77ffffff> - {weapon} - {q}"
)
DMG_IN = (
    "<color=0xffcc0000><b>{n}</b> <color=0x77ffffff><font size=10>from</font> <b><color=0xffffffff>{label}</b>"
    "<font size=10><color=0x77ffffff> - {weapon} - {q}"
)
REP_IN = (
    "<color=0xffccff66><b>{n}</b><color=0x77ffffff><font size=10> remote shield boosted by </font><b>"
    "<color=0xffffffff><font size=12><color=0xFFFFB900> <u><b>{label}</b></u></color></font> </b>"
    "<color=0x77ffffff><font size=10> - {module}</font>"
)
HULLS = ["Loki", "Legion", "Proteus", "Tengu", "Muninn", "Eagle", "Cerberus", "Sacrilege"]
LOGI = ["Scimitar", "Basilisk"]
WEAPONS = ["Heavy Pulse Laser II", "425mm AutoCannon II", "Heavy Neutron Blaster II", "720mm Howitzer Artillery II"]
QUALITIES = ["Hits", "Penetrates", "Smashes", "Grazes", "Glances Off", "Wrecks"]

results: list[tuple[str, bool, str]] = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)


def build_logs() -> dict[str, bytes]:
    rng = random.Random(42)
    pilots = []
    for side, corp in enumerate(("SYNA", "SYNB")):
        for i in range(PILOTS_PER_SIDE):
            hull = LOGI[i % 2] if i % 10 == 9 else HULLS[i % len(HULLS)]
            pilots.append(
                {
                    "name": f"Synthpilot {corp} {i:03d}",
                    "corp": corp,
                    "ship": hull,
                    "side": side,
                    "id": FIRST_ID + side * PILOTS_PER_SIDE + i,
                }
            )
    by_side = [[p for p in pilots if p["side"] == s] for s in (0, 1)]
    listeners = by_side[0][:LISTENERS_PER_SIDE] + by_side[1][:LISTENERS_PER_SIDE]
    listener_names = {p["name"] for p in listeners}
    lines: dict[str, list[str]] = {p["name"]: [] for p in listeners}

    def stamp(t: int) -> str:
        return (START + timedelta(seconds=t)).strftime("[ %Y.%m.%d %H:%M:%S ]")

    def damage_label(p) -> str:
        return f"{p['name']}[{p['corp']}]({p['ship']})"

    for t in range(SECONDS):
        ts = stamp(t)
        for _ in range(EVENTS_PER_SECOND):
            listener = rng.choice(listeners)
            other = rng.choice(by_side[1 - listener["side"]])
            src, tgt = (listener, other) if rng.random() < 0.5 else (other, listener)
            n, weapon, q = rng.randrange(80, 1500), rng.choice(WEAPONS), rng.choice(QUALITIES)
            if src["name"] in listener_names:
                lines[src["name"]].append(
                    f"{ts} (combat) " + DMG_OUT.format(n=n, label=damage_label(tgt), weapon=weapon, q=q)
                )
            if tgt["name"] in listener_names:
                lines[tgt["name"]].append(
                    f"{ts} (combat) " + DMG_IN.format(n=n, label=damage_label(src), weapon=weapon, q=q)
                )
        if t % 3 == 0:
            for listener in listeners:
                logi = rng.choice([p for p in by_side[listener["side"]] if p["ship"] in LOGI])
                lines[listener["name"]].append(
                    f"{ts} (combat) "
                    + REP_IN.format(
                        n=rng.randrange(200, 900),
                        label=f"{logi['ship']} [{logi['name']}]",
                        module="Large Remote Shield Booster II",
                    )
                )
    return {
        f"20261020_175500_{p['id']}.txt": (HEADER.format(name=p["name"]) + "\n".join(lines[p["name"]]) + "\n").encode(
            "utf-8"
        )
        for p in listeners
    }


def timed(client: Client, fight_id: int, **params) -> tuple[float, dict]:
    started = time.perf_counter()
    result = client.analysis(fight_id, **params)
    return time.perf_counter() - started, result


def main() -> int:
    logs = build_logs()
    total_lines = sum(blob.count(b"(combat)") for blob in logs.values())
    print(
        f"{len(logs)} synthetic logs, {total_lines} combat lines, {sum(map(len, logs.values())) / 1e6:.1f} MB",
        flush=True,
    )

    # Start from scratch so the fight gets a new ID and nothing about it is cached yet.
    removed = django_shell(
        "from fightlog.models import CombatLog, Fight; "
        "f = Fight.objects.filter(participants__key__startswith='p:Synthpilot ').distinct().delete()[0]; "
        f"l = CombatLog.objects.filter(listener_character_id__gte={FIRST_ID}, "
        f"listener_character_id__lt={FIRST_ID + 2 * PILOTS_PER_SIDE}).delete()[0]; print(f, l)"
    )
    print(f"removed rows from an earlier run: {removed}", flush=True)

    admin = Client("admin")
    started = time.time()
    log_ids = []
    for name, blob in logs.items():
        reply = admin.upload(name, blob)
        if reply.get("log_id"):
            log_ids.append(reply["log_id"])
    check("P1 all 50 synthetic logs accepted", len(log_ids) == 50, f"{len(log_ids)} log ids")
    statuses = [admin.wait_log(log_id, timeout=900) for log_id in log_ids]
    fight_ids = {f["id"] for s in statuses for f in s["fights"]}
    check("P2 they form one fight", len(fight_ids) == 1, f"fights {sorted(fight_ids)}")
    fight_id = max(fight_ids, key=lambda fid: sum(fid in {f["id"] for f in s["fights"]} for s in statuses))
    admin.wait_fight(fight_id, timeout=900)
    print(f"ingest and build took {time.time() - started:.0f}s", flush=True)
    events = django_shell(
        "from fightlog.models import Fight; from fightlog.services.analysis import load_fight_store; "
        f"f = Fight.objects.get(pk={fight_id}); s = load_fight_store(f); "
        "print(len(s), f.data.chunks.count(), sum(len(c.store) for c in f.data.chunks.all()))"
    )
    count, chunks, size = (int(x) for x in events.split())
    check(
        "P3 the fight holds every event (copies are merged at read time)",
        count == total_lines,
        f"{count} events in {chunks} chunks, {size / 1e6:.1f} MB packed",
    )

    cold, result = timed(admin, fight_id)
    check(
        "P4 first uncached analysis request under 2 s",
        cold < 2.0,
        f"{cold * 1000:.0f} ms, {len(result['pilots'])} pilots",
    )
    warm, _ = timed(admin, fight_id)
    check("P5 cached analysis request under 200 ms", warm < 0.2, f"{warm * 1000:.0f} ms")
    subset = ",".join(str(i) for i in sorted(log_ids)[5:])
    partial, result = timed(admin, fight_id, logs=subset)
    check(
        "P6 uncached request with 45 of 50 logs active under 2 s",
        partial < 2.0 and len(result["active_logs"]) == 45,
        f"{partial * 1000:.0f} ms",
    )
    for bucket in (5, 60):
        elapsed, _ = timed(admin, fight_id, bucket=bucket, perspective=f"pilot:p:Synthpilot SYNB {bucket:03d}")
        check(
            f"P7 uncached {bucket}s buckets from one pilot's perspective under 2 s",
            elapsed < 2.0,
            f"{elapsed * 1000:.0f} ms",
        )
    mid = int(START.timestamp()) + SECONDS // 2
    started = time.perf_counter()
    page = admin.get(
        f"/fightlog/api/fight/{fight_id}/events/", params={"start": mid, "end": mid + 9, "limit": 500}
    ).json()
    elapsed = time.perf_counter() - started
    check(
        "P8 events in one 10 s bucket under 2 s",
        elapsed < 2.0 and page["total"] > 0,
        f"{elapsed * 1000:.0f} ms, {page['total']} events",
    )
    started = time.perf_counter()
    pilot = admin.get(f"/fightlog/api/fight/{fight_id}/pilot/", params={"key": "p:Synthpilot SYNA 000"}).json()
    elapsed = time.perf_counter() - started
    check("P9 pilot drill-down under 2 s", elapsed < 2.0 and pilot.get("weapons"), f"{elapsed * 1000:.0f} ms")
    started = time.perf_counter()
    html = admin.get(f"/fightlog/fight/{fight_id}/")
    elapsed = time.perf_counter() - started
    check("P10 fight page HTML under 2 s", html.status_code == 200 and elapsed < 2.0, f"{elapsed * 1000:.0f} ms")

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} performance checks passed (fight {fight_id})")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
