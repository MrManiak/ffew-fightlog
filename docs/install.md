# Installing ffew-fightlog

This guide is for the admin of an Alliance Auth install. It assumes a working Auth 5.5 or later (Django 5.2, Python 3.10–3.14) on MariaDB or MySQL, with the usual Celery worker, Celery beat and Redis.

The plugin needs no data loads. The ship and module catalog it uses ships inside the package.

## 1. Install the package

**Bare-metal install** (the [official AA guide](https://allianceauth.readthedocs.io/en/latest/installation/allianceauth.html) layout):

```bash
sudo su allianceserver
source /home/allianceserver/venv/auth/bin/activate
cd /home/allianceserver/myauth/
pip install ffew-fightlog
```

**Docker install** (AA's [custom image](https://allianceauth.readthedocs.io/en/latest/installation-containerized/docker.html#using-a-custom-docker-image) setup): add this line to `conf/requirements.txt`. The image is rebuilt in step 3.

```
ffew-fightlog==0.2.0
```

## 2. Settings

Add the app and its daily clean-up task to `myauth/settings/local.py` (Docker: `conf/local.py`):

```python
from celery.schedules import crontab

INSTALLED_APPS += [
    "fightlog",
]

# ffew-fightlog: delete hidden no-PvP logs once their retention period is over.
CELERYBEAT_SCHEDULE["fightlog_purge_expired_logs"] = {
    "task": "fightlog.tasks.purge_expired_logs",
    "schedule": crontab(minute=17, hour=4),
    "apply_offset": True,
}

# Optional: how our side is named in fight titles (default: its largest alliance ticker).
FIGHTLOG_FRIENDLY_LABEL = "WEFEW"
```

All other settings have working defaults. They are listed in [Settings](#settings) below.

## 3. Database, static files, restart

**Bare metal:**

```bash
python manage.py migrate
python manage.py collectstatic --noinput
supervisorctl restart myauth:
```

**Docker:**

```bash
docker compose --env-file=.env up -d --build
docker compose exec allianceauth_gunicorn bash
auth migrate
auth collectstatic --noinput
exit
```

`up -d --build` recreates the worker and beat containers too, so they load the new tasks and the beat entry.

## 4. Let big gamelogs through the web server

Members upload one gamelog per request. Most are small, but busy sessions run to several MB: on one member's PC, 75 of 16,219 logs were over 1 MB and the largest was 8.8 MB. The plugin accepts up to 25 MB (`FIGHTLOG_MAX_UPLOAD_MB`).

nginx rejects request bodies over **1 MB** by default, with a `413` error, and neither of AA's nginx configs changes that. Add this line to the `server` block that proxies to gunicorn:

```nginx
client_max_body_size 25m;
```

- **Bare metal:** your Auth site config from [AA's nginx guide](https://allianceauth.readthedocs.io/en/latest/installation/nginx.html), then `sudo systemctl reload nginx`.
- **Docker:** the `server` block in `conf/nginx.conf`, then `docker compose restart nginx`.

Apache 2.4 allows 1 GB by default (`LimitRequestBody`), so it normally needs no change. A proxy or CDN in front of the server has its own limit. Cloudflare's free plan, for example, allows 100 MB.

## 5. Permissions

Every permission sits under *fightlog | general* in the admin. Assign them to states or groups as usual.

| Permission | Give it to | It allows |
|---|---|---|
| `basic_access` — Can browse fights and upload own logs | The Member state | The menu item; browsing and searching every fight; uploading logs of their own characters; deleting their own uploads; editing fights they contributed a log to |
| `upload_any_log` — Can upload logs for characters they don't own | FCs | Uploading logs collected from fleet members or allies |
| `curate_fights` — Can edit any fight, delete any log and manage allies | FCs, directors | Editing any fight, excluding logs for everyone, deleting any log, the *Allies* page |
| `post_to_discord` — Can post fights to Discord | Directors, admins | The *Post to Discord* button. Nothing is ever posted automatically |

A log for a character nobody in the Auth owns is accepted from `upload_any_log` users, but it never makes its listener's side friendly.

## 6. Who counts as friendly

Our side is seeded from characters registered in the Auth, so alliance members are friendly without any setup. Allies who aren't in the Auth can be added by a curator on *Fight Logs → Allies*, or by an admin under *Fightlog → Friendly entities*. A fight in which none of ours took part still gets its sides split, with the friendly side left empty.

## 7. Discord (optional)

1. In Discord, create a webhook for each channel fights may go to (*Channel settings → Integrations → Webhooks*).
2. In the Auth admin, add each one under *Fightlog → Discord webhooks*: a name people will recognise, and the webhook URL.

Users with `post_to_discord` then see a *Post to Discord* button on each fight, with a choice of channel. Each post is logged under *Fightlog → Discord posts*.

## 8. Capacity

These figures come from the development stack (Celery threads pool, 4 threads):

- **Parsing** a log takes 0.1–1 s per worker thread.
- **Rebuilding** a fight after an upload: a synthetic 50-log, 300-pilot fight with 269k combat events rebuilt in about 5 s. The first build took about 15 s, because it resolved 300 new names through ESI. Worker memory peaked about 60 MB above idle.
- **Viewing**: the first analysis of that fight took 0.27 s and a cached one 0.02 s. Each web worker keeps up to 4 decoded fights in memory, a few MB each.
- **Storage**: raw logs are stored gzipped, at about 3% of their size. The 269k-event fight packs to 1.9 MB.

AA's standard worker setup is enough. When a whole fleet uploads at once, each fight has at most one rebuild queued at a time, not one per log.

The plugin calls three outside services:
- **ESI**, through django-esi with the Auth's own ESI settings, to resolve names.
- **zKillboard**, at most one request per second across all workers, to link killmails.
- **Discord** webhooks, only when someone clicks *Post to Discord*.

## 9. MariaDB / MySQL

Fight data is written in chunks of at most 8 MB, so the server's `max_allowed_packet` must be at least 16 MB. That is the default on MariaDB (16 MB) and MySQL 8 (64 MB). Check it with:

```sql
SHOW VARIABLES LIKE 'max_allowed_packet';
```

If it was lowered, raise it under `[mysqld]` in the server config and restart the database.

## 10. Check the install

1. Log in as a member and open *Fight Logs*. The fight list is empty.
2. On *Upload*, pick a gamelog from a recent fight (`Documents\EVE\logs\Gamelogs`). Within a few seconds its row reads *Parsed* and links to a fight.
3. Open the fight: the timeline and per-side tables appear. A minute or two later the zKillboard kills and the system fill in, if zKillboard has the kills.

If a row stays on *Queued for processing.*, the Celery worker isn't running or hasn't loaded the plugin: restart it. If it reads *Rejected: HTTP 413*, see step 4.

## Settings

All optional. Set them in `local.py`.

| Setting | Default | Meaning |
|---|---|---|
| `FIGHTLOG_MAX_UPLOAD_MB` | `25` | Largest gamelog accepted, in MB |
| `FIGHTLOG_UPLOAD_DEFAULT_DAYS` | `3` | Default session date range on the upload page |
| `FIGHTLOG_NO_PVP_RETENTION_DAYS` | `30` | How long logs without PvP are kept (hidden) before the daily purge deletes them |
| `FIGHTLOG_FRIENDLY_LABEL` | `""` | Our side's name in fight titles. Empty: its largest alliance ticker |
| `FIGHTLOG_ENGAGEMENT_GAP_SECONDS` | `300` | A quiet gap longer than this starts a new engagement |
| `FIGHTLOG_MERGE_TOLERANCE_SECONDS` | `120` | How far apart two engagements may be and still join one fight |
| `FIGHTLOG_MIN_PVP_EVENTS` | `5` | Engagements with fewer PvP events are dropped |
| `FIGHTLOG_CLOCK_OFFSET_SEARCH_SECONDS` | `10` | Largest clock difference searched for between two PCs |
| `FIGHTLOG_DEDUPE_TOLERANCE_SECONDS` | `2` | Two logs' copies of one hit may be this far apart |
| `FIGHTLOG_SIDE_MIN_CONFIDENCE` | `0.3` | Groups placed with less confidence go to Unknown |
| `FIGHTLOG_ZKB_ENABLED` | `True` | Link fights to zKillboard killmails |
| `FIGHTLOG_ZKB_REQUEST_DELAY_MS` | `1000` | Minimum delay between zKillboard requests, across all workers |
| `FIGHTLOG_ZKB_MAX_ENTITY_QUERIES` | `8` | Characters, corps or alliances one fight may query zKillboard for |
| `FIGHTLOG_ZKB_EXPANSION_MINUTES` | `15` | Kills this close to a fight that share two pilots with it join as related kills |
| `FIGHTLOG_ZKB_EXPANSION_ROUNDS` | `2` | How many times the killmail window may widen |
| `FIGHTLOG_ANALYSIS_CACHE_SECONDS` | `86400` | How long a computed fight view stays in the cache |
| `FIGHTLOG_STORE_CHUNK_EVENTS` | `100000` | Events per stored chunk |
| `FIGHTLOG_MAX_BLOB_BYTES` | `8388608` | Largest compressed chunk written to the database |
| `FIGHTLOG_DISCORD_TIMEOUT_SECONDS` | `10` | HTTP timeout for Discord posts |

## Upgrading

```bash
pip install -U ffew-fightlog
python manage.py migrate
python manage.py collectstatic --noinput
supervisorctl restart myauth:
```

(Docker: bump the version in `conf/requirements.txt`, rebuild, then `auth migrate` and `auth collectstatic`.)

When the [CHANGELOG](../CHANGELOG.md) says the parser changed, re-read the stored logs with the new parser. The work runs in Celery:

```bash
python manage.py fightlog_reparse
```

## Maintenance commands

| Command | What it does |
|---|---|
| `fightlog_reparse [--all] [--sync]` | Re-parse logs written by an older parser (`--all`: every log), then rebuild the fights they touch |
| `fightlog_rebuild [fight IDs…] [--sync]` | Rebuild the given fights, or all of them |

Both queue Celery tasks unless `--sync` is given. The admin pages for fights and logs have the same actions.

## Uninstalling

```bash
python manage.py migrate fightlog zero
pip uninstall ffew-fightlog
```

Then remove `"fightlog"` and the `fightlog_purge_expired_logs` beat entry from `local.py`, and restart Auth, the worker and beat.

## Data and privacy

Gamelogs contain other players' character names and what happened in each fight. Every member with `basic_access` can see every fight. Members can delete their own uploads at any time. Curators can delete any log. Logs without PvP are hidden, and deleted after `FIGHTLOG_NO_PVP_RETENTION_DAYS`.
