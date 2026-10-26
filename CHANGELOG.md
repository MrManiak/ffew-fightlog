# Changelog

All notable changes to this project are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

Entries say when the **parser** changed. After upgrading to such a version, run `python manage.py fightlog_reparse` (see [install.md](docs/install.md#upgrading)).

## [0.1.0] - 2026-10-26

First release. Parser version 1, type catalog from SDE build 3646012.

### Added

- **Upload:** drag and drop gamelogs or a whole `Gamelogs` folder. The browser sends only files from the chosen date range that contain combat lines, and shows the result for each file. Identical re-uploads credit the extra uploader. A longer copy of a session replaces a shorter one.
- **Parser:**
  - Every combat line shape found in a 1.7M-line corpus: damage, misses, remote reps, cap transfers, neuts, nos, ECM, tackle and other ewar.
  - Overview label templates are learned per log.
  - The `&lt;`/`&gt;` escapes are decoded.
  - NPCs, drones, charges and structures are told apart from pilots.
- **Fights:**
  - Engagements are split on quiet gaps and grouped into fights across logs.
  - Clock offsets are estimated per log.
  - Hits seen from both ends are counted once.
  - Pilots' hulls are followed over time, and ship-only actors ("repped by Loki") are attributed to a pilot.
- **Sides:**
  - Sides are guessed from targeted hostility, reps, fleet membership and killmails. AoE hits are not counted as evidence.
  - Auth members and configured allies seed our side.
  - Groups the guesser isn't sure about go to Unknown, with a "likely friendly" hint for smartbomb-only contact.
  - Fights with none of ours are still split into sides.
- **Fight page:**
  - A timeline with damage, reps, cap, neuts, nos, jams and tackle in and out, from any side, pilot or log.
  - Per-side tables with hit % and hit quality.
  - A targets table, and a pilot drill-down with the raw events behind every number.
  - Per-viewer log filtering.
- **zKillboard:**
  - Killmails are linked to fights through character, corp and alliance queries.
  - The killmail window widens to take in related kills, and is re-checked later.
  - These fill in the system, losses and ISK.
- **Curation:**
  - Rename, notes and tags.
  - Merge and split fights (old links redirect), and move engagements between fights.
  - Fix sides and attributions. These survive rebuilds.
  - Exclude logs for everyone, set the system.
  - Every edit is recorded in the fight's history.
  - The Allies page for curators.
- **Discord:** users with `post_to_discord` post a fight summary to a webhook chosen from an admin-managed list. Nothing is posted automatically.
- **Admin and operations:**
  - A daily purge of hidden no-PvP logs.
  - The `fightlog_reparse` and `fightlog_rebuild` commands, with matching admin actions.
  - An [install guide](docs/install.md).

## Release checklist

Before the first release only:
- On pypi.org, add a pending trusted publisher: project `ffew-fightlog`, owner `MrManiak`, repository `ffew-fightlog`, workflow `ci-cd.yaml`, environment `pypi`.
- On GitHub, create the `pypi` environment in the repository settings.

Every release:

1. Rebuild the type catalog from the current SDE (`python tools/build_type_catalog.py`) and commit it. CI checks that it loads.
2. If Alliance Auth was upgraded, compare `ESI_COMPATIBILITY_DATE` in `fightlog/providers.py` with AA's own (`__esi_compatibility_date__` in `allianceauth/eveonline/__init__.py`).
3. If the parser changed, bump `PARSER_VERSION` in `fightlog/parser/__init__.py` and say so in this file.
4. Bump `__version__` in `fightlog/__init__.py`, date the section above, and tag `vX.Y.Z`. CI publishes the tag to PyPI through trusted publishing.

[0.1.0]: https://github.com/MrManiak/ffew-fightlog/releases/tag/v0.1.0
