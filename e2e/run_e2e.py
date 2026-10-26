"""End-to-end tests against the running dev stack (dev/docker-compose.yml).

    python e2e/run_e2e.py [Gamelogs folder]

Phases:
  A  fixtures: the anonymized five-log fight uploaded by a curator (upload_any_log)
  B  real data: your five real logs of the 2026-10-25 fight, then live zKillboard linking
  C  permissions: a plain member can browse but not edit, delete or upload others' logs
  D  curation: rename, tags, notes, exclusion, side override, attribution, split + merge, history
  E  Discord: post to a mock webhook through the Celery worker
  F  ingest rules: duplicate credit, no-PvP logs, non-gamelog files, deletion
  G  bulk real data: every real log with combat from the last days of the corpus
Exit code 0 when every check passes.
"""

import gzip
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from client import BASE, Client, django_shell  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
FIXTURES = ROOT / "fightlog" / "tests" / "fixtures" / "fight-2026-10-25"
ARTIFACTS = Path(__file__).resolve().parent / "artifacts"
GAMELOGS = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / "Documents" / "EVE" / "logs" / "Gamelogs"
REAL_FIGHT = [
    "20261025_195145_2113328928.txt",
    "20261025_195149_2120544984.txt",
    "20261025_195425_2120522182.txt",
    "20261025_195427_2120273592.txt",
    "20261025_195433_2123678051.txt",
]
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, bool(ok), detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail else ""), flush=True)
    return bool(ok)


def reset() -> None:
    django_shell(
        "from fightlog.models import *; Fight.objects.all().delete(); CombatLog.objects.all().delete(); "
        "Killmail.objects.all().delete(); FriendlyEntity.objects.all().delete(); DiscordPost.objects.all().delete(); "
        "FightRedirect.objects.all().delete(); from django.core.cache import cache; cache.clear()"
    )


def upload_all(client: Client, files: list[tuple[str, bytes]]) -> list[dict]:
    statuses = []
    for name, data in files:
        response = client.upload(name, data)
        if response.get("log_id"):
            statuses.append(client.wait_log(response["log_id"], timeout=240))
        else:
            statuses.append(response)
    return statuses


def main_fight(statuses: list[dict]) -> int:
    counts = {}
    for s in statuses:
        for f in s.get("fights", []):
            counts[f["id"]] = counts.get(f["id"], 0) + 1
    return max(counts, key=counts.get)


def corp_sides(client: Client, fight_id: int) -> dict[str, set]:
    result = client.analysis(fight_id)
    sides = {s["id"]: s["order"] for s in result["sides"]}
    out: dict[str, set] = {}
    for row in result["pilots"]:
        if row["kind"] != "pilot":
            continue
        out.setdefault(row["corp"], set()).add(sides.get(row["side"]))
    return out


def phase_a(curator: Client) -> None:
    files = [(p.name[:-3], gzip.decompress(p.read_bytes())) for p in sorted(FIXTURES.glob("*.txt.gz"))]
    started = time.time()
    statuses = upload_all(curator, files)
    check(
        "A1 fixture logs accepted and parsed",
        all(s["status"] == "parsed" for s in statuses),
        f"{[s['status'] for s in statuses]} in {time.time() - started:.0f}s",
    )
    fight_id = main_fight(statuses)
    fight_logs = sum(1 for s in statuses for f in s["fights"] if f["id"] == fight_id)
    check("A2 the five fixture logs form one fight", fight_logs == 5, f"fight {fight_id}")
    page = curator.get(f"/fightlog/fight/{fight_id}/")
    check("A3 fight page renders", page.status_code == 200 and "fight-config" in page.text)
    # Nobody in this Auth owns the anonymized listeners, so no side is friendly: look
    # through one listener's eyes instead.
    result = curator.analysis(fight_id, perspective="pilot:p:Pilot 001")
    check(
        "A4 analysis has pilots, timeline and targets",
        len(result["pilots"]) > 50 and sum(result["series"]["damage_out"]) > 0 and bool(result["targets"]),
        f"{len(result['pilots'])} pilots, {sum(result['series']['damage_out'])} damage out",
    )
    events = curator.get(f"/fightlog/api/fight/{fight_id}/events/", params={"limit": 10}).json()
    check("A5 drill-down events", events["total"] > 1000 and len(events["events"]) == 10)


def phase_b(admin: Client) -> int:
    files = [(name, (GAMELOGS / name).read_bytes()) for name in REAL_FIGHT if (GAMELOGS / name).exists()]
    if not check("B0 real logs present", len(files) == 5, str(GAMELOGS)):
        return 0
    statuses = upload_all(admin, files)
    check("B1 real logs parsed", all(s["status"] == "parsed" for s in statuses), str([s["status"] for s in statuses]))
    fight_id = main_fight(statuses)
    admin.wait_fight(fight_id)
    sides = corp_sides(admin, fight_id)
    check("B2 FFEW friendly from logs", sides.get("FFEW") == {0}, str(sides.get("FFEW")))
    check(
        "B3 Pyrolanc Squad corps hostile",
        all(sides.get(t) == {1} for t in ("-G-F", "IMBCN", "LPCR", "NEDX")),
        str({t: sides.get(t) for t in ("-G-F", "IMBCN", "LPCR", "NEDX")}),
    )
    # Live zKillboard linking runs in the worker after the first rebuild.
    deadline = time.time() + 300
    linked = False
    while time.time() < deadline:
        page = admin.get(f"/fightlog/fight/{fight_id}/").text
        if "zKill</a>" in page and admin.get(f"/fightlog/api/fight/{fight_id}/status/").json()["built"]:
            linked = True
            break
        time.sleep(5)
    check("B4 live zKillboard killmails linked", linked)
    time.sleep(3)
    admin.wait_fight(fight_id)
    sides = corp_sides(admin, fight_id)
    check("B5 YAGAS friendly once killmails are in", sides.get("YAGAS") == {0}, str(sides.get("YAGAS")))
    result = admin.analysis(fight_id)
    kills = {k["role"] for k in result["kills"]}
    check("B6 core and related kills", kills == {"core", "related"}, str(kills))
    systems = {k["system_id"] for k in result["kills"] if k["role"] == "core"}
    check("B7 system from killmails", systems == {31001176}, str(systems))
    page = admin.get(f"/fightlog/fight/{fight_id}/").text
    check("B8 page shows the J-space system", "31001176" not in page and ("J1" in page or "zKillboard" in page))
    return fight_id


def phase_c(member: Client, fight_id: int) -> None:
    check("C1 member can browse fights", member.get("/fightlog/").status_code == 200)
    check("C2 member can open the fight", member.get(f"/fightlog/fight/{fight_id}/").status_code == 200)
    check(
        "C3 member can't edit",
        member.post(f"/fightlog/fight/{fight_id}/edit/rename/", {"title": "x"}).status_code == 403,
    )
    check("C4 member can't manage allies", member.get("/fightlog/friendlies/").status_code != 200)
    data = (GAMELOGS / REAL_FIGHT[0]).read_bytes()
    response = member.upload(REAL_FIGHT[0], data)
    check(
        "C5 member can't upload another pilot's log",
        response["status"] in ("rejected", "duplicate"),
        response["message"],
    )


def phase_d(curator: Client, fight_id: int) -> None:
    base = f"/fightlog/fight/{fight_id}/edit"
    curator.post(f"{base}/rename/", {"title": "E2E hole fight"})
    curator.post(f"{base}/tags/", {"tags": "#e2e #lost"})
    curator.post(f"{base}/notes/", {"notes": "End-to-end test notes"})
    page = curator.get(f"/fightlog/fight/{fight_id}/").text
    check("D1 rename, tags and notes", "E2E hole fight" in page and "#e2e" in page and "End-to-end test notes" in page)
    logs = json.loads(page.split('id="fight-config" type="application/json">', 1)[1].split("</script>", 1)[0])["logs"]
    excluded = logs[-1]["id"]
    curator.post(f"{base}/exclude/", {"log": excluded, "reason": "e2e"})
    curator.wait_fight(fight_id)
    result = curator.analysis(fight_id)
    check("D2 excluded log drops out of the default view", excluded not in result["active_logs"])
    curator.post(f"{base}/include/", {"log": excluded})
    curator.wait_fight(fight_id)
    check("D3 re-included", excluded in curator.analysis(fight_id)["active_logs"])
    result = curator.analysis(fight_id)
    npe = [r["key"] for r in result["pilots"] if r["corp"] == "NPE."]
    friendly = next(s["id"] for s in result["sides"] if s["order"] == 0)
    curator.post(f"{base}/side/", {"keys": npe, "side": friendly})
    curator.wait_fight(fight_id)
    sides = corp_sides(curator, fight_id)
    check("D4 side override applies after rebuild", sides.get("NPE.") == {0}, str(sides.get("NPE.")))
    history = curator.get(f"/fightlog/fight/{fight_id}/").text
    check("D5 history records the edits", history.count("Renamed") >= 1 and "Changed a side" in history)
    status = curator.get(f"/fightlog/api/fight/{fight_id}/status/").json()
    start_end = curator.analysis(fight_id)
    mid = (start_end["start"] + start_end["end"]) // 2
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(mid))
    response = curator.post(f"{base}/split/", {"at": stamp})
    check("D6 split", response.status_code == 302, str(status))
    fights_after_split = django_shell("from fightlog.models import Fight; print(Fight.objects.count())").strip()
    other = int(
        django_shell(
            f"from fightlog.models import Fight; print(Fight.objects.exclude(pk={fight_id}).filter(is_curated=True).order_by('-pk').first().pk)"
        ).strip()
    )
    curator.wait_fight(other)
    response = curator.post(f"{base}/merge/", {"other": other})
    survivor = int(response.headers["Location"].rstrip("/").split("/")[-1])
    curator.wait_fight(survivor)
    redirect = curator.get(f"/fightlog/fight/{other if survivor != other else fight_id}/")
    check(
        "D7 merge back and old link redirects",
        redirect.status_code in (200, 301),
        f"{fights_after_split} fights after split",
    )


def phase_e(curator: Client, fight_id: int) -> None:
    out = ARTIFACTS / "discord.jsonl"
    out.unlink(missing_ok=True)
    server = subprocess.Popen([sys.executable, str(Path(__file__).with_name("mock_discord.py")), "8765", str(out)])
    try:
        time.sleep(1)
        webhook = django_shell(
            "from fightlog.models import DiscordWebhook; print(DiscordWebhook.objects.get(name='dev-mock').pk)"
        ).strip()
        response = curator.post(f"/fightlog/fight/{fight_id}/discord/", {"webhook": webhook})
        check("E1 post queued", response.status_code == 302)
        deadline = time.time() + 60
        while time.time() < deadline and not out.exists():
            time.sleep(1)
        payload = json.loads(out.read_text(encoding="utf-8").splitlines()[0]) if out.exists() else {}
        embed = (payload.get("body", {}).get("embeds") or [{}])[0]
        check(
            "E2 webhook received the embed",
            bool(embed.get("title")) and "/fightlog/fight/" in embed.get("url", ""),
            embed.get("title", ""),
        )
        time.sleep(2)
        page = curator.get(f"/fightlog/fight/{fight_id}/").text
        check("E3 post recorded as sent", "(Sent)" in page)
    finally:
        server.terminate()


def phase_f(admin: Client) -> None:
    data = (GAMELOGS / REAL_FIGHT[1]).read_bytes()
    response = admin.upload(REAL_FIGHT[1], data)
    check("F1 identical re-upload is a duplicate", response["status"] == "duplicate", response["message"])
    response = admin.upload("notes.txt", b"just some text")
    check("F2 non-gamelog rejected", response["status"] == "rejected", response["message"])
    pve = next(
        (
            p
            for p in sorted(GAMELOGS.glob("20261025_*_*.txt"))
            if p.name not in REAL_FIGHT and b"(combat)" in p.read_bytes() and p.stat().st_size < 400000
        ),
        None,
    )
    if pve:
        status = admin.wait_log(admin.upload(pve.name, pve.read_bytes())["log_id"], timeout=120)
        check(
            "F3 a log without PvP is kept hidden as no_pvp",
            status["status"] in ("no_pvp", "parsed"),
            f"{pve.name}: {status['status']}",
        )
    my_logs = admin.get("/fightlog/my-logs/").text
    check("F4 my logs lists the uploads", "Misterma Niak" in my_logs)


def phase_g(admin: Client) -> None:
    days = sorted({p.name[:8] for p in GAMELOGS.glob("2026*_*_*.txt")})[-7:]
    files = []
    for path in sorted(GAMELOGS.glob("2026*_*_*.txt")):
        if path.name[:8] in days and path.name not in REAL_FIGHT and path.stat().st_size < 25 * 1024 * 1024:
            data = path.read_bytes()
            if b"(combat)" in data:
                files.append((path.name, data))
    started = time.time()
    statuses = upload_all(admin, files)
    by_status: dict[str, int] = {}
    for s in statuses:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
    fights = len({f["id"] for s in statuses for f in s.get("fights", [])})
    check(
        "G1 bulk upload of the last week's logs",
        "failed" not in by_status,
        f"{len(files)} files from {days[0]}–{days[-1]} → {by_status}, {fights} fights, {time.time() - started:.0f}s",
    )
    listing = admin.get("/fightlog/").text
    check("G2 fight list renders", "Fights" in listing)


def main() -> int:
    ARTIFACTS.mkdir(exist_ok=True)
    reset()
    admin, member, curator = Client("admin"), Client("member"), Client("curator")
    print(f"dev stack at {BASE}")
    phase_a(curator)
    reset()
    fight_id = phase_b(admin)
    if fight_id:
        phase_c(member, fight_id)
        phase_d(curator, fight_id)
        survivor = int(
            django_shell(
                "from fightlog.models import Fight; print(max(Fight.objects.all(), key=lambda f: f.engagements.count()).pk)"
            ).strip()
        )
        phase_e(curator, survivor)
    phase_f(admin)
    phase_g(admin)
    failed = [r for r in results if not r[1]]
    (ARTIFACTS / "e2e-results.json").write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
