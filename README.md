# ffew-fightlog

An [Alliance Auth](https://gitlab.com/allianceauth/allianceauth) plugin for post-mortems of EVE Online fights, built for We Forsakened Few [WEFEW]. Members upload the combat logs their game clients already write. The plugin rebuilds each fight from all of them, and keeps it for everyone to browse:

- **Fights, found automatically.** Uploads from many pilots are grouped into fights, with the clocks lined up and hits seen from both ends counted once.
- **Sides, guessed.** Who shot, repped, neuted or tackled whom, fleet membership and zKillboard killmails. Anything uncertain goes to Unknown.
- **Timeline.** Damage, reps, cap, neuts, jams and tackle in and out, from any side, pilot or log. Click a point to see the raw events behind it.
- **Tables.** Per pilot: damage dealt and taken, hit % and hit quality, reps, neuts, jams, points. A targets table shows how long each primary lasted and what logi did about it.
- **Per-log filtering.** Toggle a bad log off for yourself, or exclude it for everyone as a curator.
- **zKillboard and Discord.** Losses, ISK and the system come from zKillboard. Fights are posted to Discord only when someone with the permission clicks the button.

## Install

See **[docs/install.md](https://github.com/MrManiak/ffew-fightlog/blob/master/docs/install.md)**: the package, settings, permissions, the web-server upload limit, Discord webhooks, and capacity notes.

```bash
pip install ffew-fightlog
```

Requires Alliance Auth 5.5+ (Django 5.2, Python 3.10–3.14) with MariaDB or MySQL, Celery and Redis.

## Documentation

- [Install guide](https://github.com/MrManiak/ffew-fightlog/blob/master/docs/install.md)
- [Product spec](https://github.com/MrManiak/ffew-fightlog/blob/master/docs/product-spec.md)
- [Technical plan](https://github.com/MrManiak/ffew-fightlog/blob/master/docs/technical-plan.md)
- [Gamelog formats](https://github.com/MrManiak/ffew-fightlog/blob/master/docs/log-formats.md)
- [Changelog](https://github.com/MrManiak/ffew-fightlog/blob/master/CHANGELOG.md)

## Development

You need Python 3.10+, Docker and a Redis on `127.0.0.1:6379` for the tests (`docker run -d -p 127.0.0.1:6379:6379 redis:8`).

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[test]"
DJANGO_SETTINGS_MODULE=testauth.settings_aa5.local python runtests.py fightlog
tox                                    # the CI matrix, with coverage
```

**Local Alliance Auth.** `dev/` runs AA 5.5 with MariaDB, Redis, a Celery worker and beat. The plugin is mounted from the checkout, so code changes reach `runserver` straight away. Restart the worker after changing tasks or analysis code.

```bash
cd dev && cp .env.example .env         # put your character IDs in DEV_CHARACTER_IDS
docker compose up -d --build
docker compose exec auth python manage.py shell -c "exec(open('/home/allianceauth/seed.py').read())"
```

Then sign in at <http://localhost:8000/admin/> as `admin` (password `DEV_ADMIN_PASSWORD`) and open <http://localhost:8000/fightlog/>. The seed also creates `member` (`basic_access` only) and `curator` (every permission).

**End-to-end tests** run against that stack. They need Playwright for the browser part (`pip install playwright && playwright install chromium`).

```bash
python e2e/run_e2e.py [Gamelogs folder]   # API: fixtures, your real logs, permissions, curation, Discord
python e2e/browser_e2e.py                 # the pages and their JavaScript in Chromium
python e2e/perf_e2e.py                    # a synthetic 50-log, 300-pilot fight, timed
```

`run_e2e.py` wipes the dev database's fights and logs first, and starts a stand-in Discord webhook on port 8765. Its real-data checks expect the 2026-10-25 fight in your Gamelogs folder (default `~/Documents/EVE/logs/Gamelogs`).

**Tools.** Real logs never leave your machine: only anonymized output is committed.

- `tools/corpus_scan.py <Gamelogs folder>` checks the parser against every log you have.
- `tools/anonymize.py` builds test fixtures.
- `tools/build_type_catalog.py` refreshes the bundled ship and module catalog from CCP's SDE.

## License and notices

GPL-3.0. See [LICENSE](https://github.com/MrManiak/ffew-fightlog/blob/master/LICENSE).

EVE Online and the EVE logo are the registered trademarks of CCP hf. All rights are reserved worldwide. All other trademarks are the property of their respective owners. EVE Online, the EVE logo, EVE and all associated logos and designs are the intellectual property of CCP hf. All artwork, screenshots, characters, vehicles, storylines, world facts or other recognizable features of the intellectual property relating to these trademarks are likewise the intellectual property of CCP hf. ffew-fightlog is not affiliated with CCP hf, and CCP hf is not responsible for the content or functioning of this program.
