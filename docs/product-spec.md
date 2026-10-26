# ffew-fightlog — Product Spec (v1.1)

Revised 2026-10-26 after the [review](review-2026-10-26.md) and the [response to it](review-2026-10-26-response.md). The revision changes:
- what the logs are known to contain
- side guessing and how killmails are used
- where the solar system comes from
- permissions, merging and deletion
- the acceptance scenarios

## Context

**ffew-fightlog** is built for We Forsakened Few [WEFEW] ([alliance 99011699](https://zkillboard.com/alliance/99011699/)), whose home corporation is The Forsakened Few [FFEW]. The alliance wants post-mortems based on what actually happened in a fight, not on how people remember it.

Every EVE client already writes a log for each character session (`Documents\EVE\logs\Gamelogs\<date>_<time>_<characterID>.txt`). ffew-fightlog is an Alliance Auth plugin:
- Members upload those logs.
- The plugin rebuilds each fight from them: graphs, per-pilot tables, side detection.
- The fights are kept so anyone can browse and inspect them later.

The goal is to understand *why* a fight was won or lost: whether the damage landed, whether logi kept up, who got jammed or neuted and when, and how fast the primaries died.

The plugin is public, so nothing in it is specific to FFEW. The friendly side is whoever is registered in the Auth, plus allies the Auth configures. For FFEW that could be the sister alliance FFEW Associates, or coalition partners such as Baba Yagas [YAGAS], which is in The Initiative.

This document is the product spec. The design that implements it is the [technical plan](technical-plan.md).

## What the logs give us (known so far)

This section was checked against about 16,000 local logs (2022–2026), five logs of the same fight on 2026-10-25, ESI and zKillboard. New line formats keep turning up, so treat it as a starting point. The parser's format list is built from the log corpus (technical plan, M1).

### Files

- **Filename and header.** The filename is `YYYYMMDD_HHMMSS_<characterID>.txt`. The header gives `Listener: <character name>` and `Session Started: <EVE time>`.
- **Empty files.** Files without a character ID in the name are empty character-select sessions, with no listener and no combat. They are ignored.
- **Timestamps** are EVE time (UTC) at 1-second resolution.
  - Logs written on the same PC agree within ±2 s, and they match the server's killmail times to within about a second.
  - We don't yet know how far clocks drift between different players' PCs, so the plugin estimates each log's clock offset and aligns logs before combining them (see the log panel in Feature 4, and De-duplication under Metric definitions).

### Combat lines

- **Damage out and in:** `351 to Pilot[CORP](Ship) - Weapon - Grazes`, and the same with `from`.
  - The hit-quality word is one of Wrecks, Smashes, Penetrates, Hits, Glances Off or Grazes.
  - Missiles and smartbombs always say "Hits".
  - Structure names can contain " - ", and hits on structures can end in `(N deflected)`.
- **Misses:** `Your group of X misses Y completely`, `X misses you completely`, `Drone belonging to Pilot misses you completely`.
- **Remote reps and cap transfers:**
  - Reps: `remote shield boosted by/to`, `remote armor repaired by/to`, and hull. `by` means incoming, `to` means outgoing.
  - Cap transfers: `remote capacitor transmitted by/to`.
- **Neuts:** `N GJ energy neutralized X - Module`. The text is identical in both directions. Only the line's colour tells outgoing from incoming.
- **Nos:** `+N GJ energy drained from X` is outgoing, `-N GJ energy drained to X` is incoming.
- **ECM:** `You're jammed by X - Module` is incoming, `X jammed - Module` is outgoing. ECM is rare: under 1,000 lines in the whole corpus, most of them from NPCs and structures.
- **Tackle** (warp scramble and disruption attempts) comes in three forms:
  - `from you to X`: the listener tackling.
  - `from X to you!`: the listener being tackled.
  - `from X to Y`: tackle between two other ships that the listener saw. The same third-party tackle often appears in several logs.

### Who is who

- **Damage lines** (hits and misses) label pilots `Pilot[CORP](Ship)` on every client. The bracket holds the **corporation** ticker.
  - The ticker and ship are always written to the file. The combat log's "show ship" and "show ticker" options only change the popup on screen.
  - Only the message text changes with the client's language.
- **Rep, cap, neut, jam and tackle lines** name actors with the listener's **overview ship label**, so they look different from client to client:
  - The label is stored per EVE account and shared by that account's characters. Importing an overview profile can replace it.
  - The stock label is roughly `Sabre <FERRA>[ECCON] Feson`: ship type, alliance ticker in angle brackets, corporation ticker in square brackets, then the pilot's name.
  - Custom labels seen in the corpus: ship type only (`boosted by Loki`), `Ship [Pilot]`, and a full label with ship, pilot, `(corp)`, `[alliance]` and ship name.
  - Drones get a label too, with their own name in the pilot slot (`Acolyte II [BCAUS] [KS88] [Acolyte II] -`).
  - Tickers can contain spaces (`[J  H]`), and angle brackets are written to the file as `&lt;` and `&gt;`.
- **NPCs** get no label, just their name.
  - NPC names can match player hull names (an NPC called "Orthrus" in a Sleeper site).
  - They can also match real character names ("Emergent Warden" is also a player).
  - A few players also appear as bare names in damage lines (2025 Sleeper-site logs), for reasons we don't know yet.
  - Telling them apart needs EVE's own type list plus context (technical plan §4.3).
- **A listener's own log never names their own hull.** Other logs and killmails do.

### Other lines

- **System changes (`(None)` lines).** These record k-space system changes: `Jumping from Jita to Perimeter`, `Undocking from … to Nema solar system`. **Wormhole jumps are never logged**, so a fight in J-space has no system line.
- **`(notify)` and `(question)` lines** record:
  - Fleet membership: invites and fleet joins, which include the character ID, plus `Following X in warp` and `Regrouping to X`.
  - Reships (`Disembarking from ship`) and clone jumps.
  - Kill notices (`… begins to explode`).
  - Wormhole-space hints (`Local spatial phenomena may cause strange effects…`).

### What logs can't show

- Interactions that don't involve an uploader, such as enemy-on-enemy reps, or damage between two pilots who didn't upload.
- Who a ship-type-only actor was, unless another log, a killmail or a process of elimination settles it (Feature 3).

### Messy realities

- **The same hit appears twice** when both the shooter and the target uploaded.
- **Non-ship targets** show up: drones, missiles in flight, interdiction probes, breacher pods.
- **Friendly fire is common in smartbomb fleets.** In the 2026-10-25 fight, FFEW and YAGAS Sleipnirs exchanged thousands of smartbomb hits. They did up to 55% of the damage on some of their own losses, and killed one FFEW capsule outright.
- **Killmails record a smartbomber's hull as their weapon**, not the smartbomb.

## Roles & permissions

| Permission | Who | Can |
|---|---|---|
| `basic_access` | Members | Browse and search all fights. Upload logs whose listener is one of their own characters. Delete logs they uploaded, or whose listener is one of their characters |
| `upload_any_log` | FCs, leadership | Upload logs for characters they don't own (logs collected from fleet members or allies) |
| `curate_fights` | FCs, directors | Edit any fight, delete any log, manage the list of allies |
| `post_to_discord` | Admins, directors, or a custom group | Post a fight to Discord |

- **Contributors.** Anyone who uploaded a log that is part of a fight can edit that fight. Merging two fights needs edit rights on both.
- **History.** Every edit and every deletion is recorded in the fight's history.
- **Logs for other people's characters** (uploaded with `upload_any_log`) never count as evidence for which side is friendly.

## Core concepts

- **Log**: one uploaded file. It has one listener character and a time range. If the same file is uploaded twice, both uploaders get credit.
- **Engagement**: one stretch of PvP combat inside a single log. A gap with no combat starts a new engagement.
- **Fight**: one or more engagements, from one or more logs, that cover the same battle.
- **Participant**: a character, an NPC, or an unresolved ship-type-only actor such as "Unknown Loki", together with their ship(s) and a side.
- **Side**: one of:
  - Friendly
  - Hostile
  - Extra hostile sides, when killmails show a third party
  - Unknown
- **Related kill**: a killmail connected to the fight's pilots that happened just outside the fight's time window or in a neighbouring system.

## Features

### 1. Upload

- **Drag and drop** one or many gamelogs, or a whole Gamelogs folder. The browser only sends files that:
  - have a session in a chosen date range (default: the last 3 days)
  - contain at least one combat line
  - are in English

  Empty character-select files are skipped. The server decides what counts as PvP.
- **Per-file results:** *joined fight X*, *created new fight*, *no PvP found*, *rejected: listener not your character*, or *duplicate (credited to you too)*.
- **Duplicates:**
  - An identical file is stored once, and the second uploader is credited too.
  - A longer copy of the same session replaces the shorter one.
  - A shorter copy changes nothing.
- **Logs with no PvP** are kept hidden for 30 days, so that a parser upgrade can still find PvP in them. After that they are deleted.
- **Raw files are kept**, so fights can be re-parsed when the parser improves. The UI shows the combat events, plus the fleet and system context the analysis used.
- **Deleting a log** recalculates its fights and is recorded in their history. If a fight loses its last log:
  - It is deleted, unless someone curated it.
  - Curated means it has notes, tags, a manual title, edits or Discord posts. A curated fight stays as an empty record.
- **A help page** explains:
  - where the Gamelogs folder is on Windows, Mac and Linux
  - the overview ship label, which decides how reps, cap, neuts, jams and tackle name pilots. This is the single most useful setting a fleet can change.
    - Any label that includes the pilot's name works, and the stock label does.
    - A label that shows only the ship type leaves those actions unattributed.
    - The label is per EVE account and can come from an overview profile, so an alliance overview profile with a good label is the easiest fix.

### 2. Fight detection & grouping (automatic, editable)

- **Engagements.** In each log, PvP combat separated by more than a configurable quiet gap (default about 5 minutes) becomes a separate engagement.
- **Joining.** Engagements from different logs join the same fight when their time windows overlap (within a tolerance) and they share participants. New uploads join existing fights automatically.
- **Automatic merging never touches a curated fight**, meaning one that anyone has edited.
- **Auto title:** `YYYY-MM-DD HH:MM — <friendly label> vs <largest hostile alliance or corp> [— System]`. The friendly label comes from a setting, and defaults to the friendly side's largest alliance ticker.
- **Curation.** Curators and contributors can rename a fight, merge fights, split a fight at a timestamp, and move an engagement to another fight.
- **Merging** keeps one fight: the curated one, otherwise the one with more logs.
  - A manual title wins.
  - Notes are combined and tags are merged.
  - History and Discord posts move over.
  - The absorbed fight's link redirects to the surviving one.

### 3. Side guessing

**Evidence:**
- **Hostile:** targeted damage, neuts, jams and tackle between two groups.
- **Not evidence:** area-of-effect damage (smartbombs, bombs). It hits friends and enemies alike.
- **Friendly:**
  - reps and cap transfers
  - being in the same fleet at the time of the fight (fleet invites, joins, warps, regroups)
- **Killmails:**
  - The victim against the attackers, and the attackers against each other, weighted by each attacker's share of the damage.
  - Attackers who were probably smartbombing are left out. That means their killmail weapon is their own hull, a smartbomb or a bomb, or our logs only ever saw AoE damage from them.
- **Groups:** pilots are grouped by alliance (looked up through EVE's API), otherwise by corporation.
- **Starting points:**
  - Characters registered in this Auth, and allies configured by curators, are friendly.
  - Logs from characters nobody in the Auth owns don't count as a starting point.

**Results:**
- **Output:** Friendly, Hostile and Unknown.
  - Extra hostile sides (third parties) appear only when killmails show hostile groups fighting each other.
  - Anything the evidence doesn't settle goes to Unknown, never to a guessed side.
- **Likely-friendly hint.** A group that only ever exchanged smartbomb hits with us, in both directions, while we were shooting other groups, gets the hint "likely friendly (smartbomb contact only)". It stays Unknown until killmails, an ally setting or a curator places it.
- **With a single log**, sides are Friendly, Hostile or Unknown. Killmails often settle the rest (see the acceptance scenarios).

**Ship-type-only actors** ("repped by Loki"):
- **Crediting.** The actor is credited to a pilot only when exactly one candidate remains: on the same side, flying that ship at the time, and not ruled out by their own log. A pilot whose log shows no matching rep didn't send it.
- **Otherwise** it is shown as *Unknown Loki (friendly)*.
- **Manual attribution.** Curators can attribute an unknown actor to a pilot.

**Manual changes:** curators can move participants or whole groups between sides and rename sides. These changes survive re-parses and new uploads.

### 4. Fight page

- **Header:**
  - title, EVE-time window and duration
  - the system and where it came from: zKillboard, a log, or a curator, or "J-space, system unknown"
  - sides with pilot counts and ship-class mix
  - losses and ISK per side, plus related kills
  - number of logs and coverage (for example "12 of 41 friendly pilots uploaded")
- **Timeline chart:** time-bucketed series (5s / 10s / 30s / 1m selectable) for damage, reps, neuts/nos, jams and tackle, each split into in and out, plus friendly fire as its own series.
  - "In" and "out" are relative to the selected perspective: a whole side (default: friendly), one pilot, or one log.
  - Killmail markers appear on the time axis, with related kills marked as such. Clicking a bucket shows the underlying events.
- **Per-side tables:** one row per pilot, sortable, with a totals row. Columns:
  - Ship(s)
  - Damage dealt and taken, and friendly fire dealt
  - Hit % plus a hit-quality breakdown, for turrets, drones and fighters only. Missiles, smartbombs and bombs never miss, so they show "n/a"
  - Reps given and received (shield/armor/hull) and cap transfers
  - Neut and nos GJ, out and in
  - Jams, out and in
  - Points and scrams applied and received
  - Logged? (whether this pilot uploaded a log)
  - Expanding a row shows breakdowns by weapon and by target.
- **Cell states.** Every cell shows how complete it is. A legend explains the four states:
  - *complete*: the pilot uploaded a log
  - *partial*: only what touched uploaders
  - *n/a*: the metric doesn't apply
  - *unattributed*: ship-type-only actions that couldn't be credited to anyone
- **Targets table:** for each pilot that took damage, it shows the following. This shows whether focus fire, and the logi answering it, worked.
  - time from first hit to death or last hit
  - total damage taken
  - number of shooters
  - reps received over that window
- **Per-pilot drill-down:** that pilot's own timeline, targets, weapons, who repped them and who ewar'd them.
- **Log panel:** every contributing log with its listener, uploaders, time range, event counts, parse warnings, and estimated clock offset (flagged when large).
  - Any viewer can toggle logs on or off. This is a local view filter and changes nothing for other people.
  - Curators and contributors can mark a log *excluded* for everyone, with a reason. Viewers can still re-include it locally.
  - All numbers, charts and tables recalculate from the active log set.

### 5. Browse & search

- **The fight list** is newest first. Each row shows date, title, system, duration, pilots per side, losses and ISK, and number of logs.
- **Filters:**
  - Date range
  - System or region
  - Participating character, corp or alliance (name or ticker)
  - Ship type
  - Uploader
  - Text search on title, notes and tags
  - Minimum size
  - Whether our side was involved
- **Notes and tags.** Fights have free-text notes and tags (e.g. `#homedefense`, `#lost`) that curators and contributors can edit.

### 6. zKillboard linking

- **Finding kills.** For each fight, find killmails that involve its resolved participants and fall inside the time window covered by its logs.
- **Widening the window.** Kills within 15 minutes of the fight that share at least two pilots with it are added as well, for up to two rounds. This catches the tackler who died just before the main fight.
- **What kills provide:** the system (the most common one among kills inside the log window), losses, ISK destroyed, side evidence, and markers on the timeline. Each kill links to its zKillboard page.
- **Related kills.** Kills in other systems, or outside the log window, are shown as related.
- **Where the system comes from**, in order of trust:
  1. A curator.
  2. zKillboard.
  3. A log, but only if no wormhole jump could have happened since the log's last system line.
  4. "J-space, system unknown".
- **Re-checks.** Recent fights are checked again later, because kills reach zKillboard with a delay.

### 7. Discord posting (explicit only)

- Users with `post_to_discord` get a **Post to Discord** button on a fight.
- Clicking it posts a summary (title, sides, losses, top numbers, link) to a channel chosen from a list the admin configures.
- Nothing is ever posted automatically.

### 8. Curation & history

- **Contributors and curators can:**
  - rename, merge or split fights
  - move engagements between fights
  - fix sides
  - attribute an unknown ship-type-only actor to a pilot
  - exclude or re-include logs
  - set the system
  - edit notes and tags
- **Curators can also** delete any log and manage the list of allies.
- **Each change** records who made it, when, and what changed. That includes log deletions and merges; a merge also records where the absorbed fight now redirects.

## Metric definitions

- **Damage:** the sum of landed hit amounts.
  - Damage to drones, missiles, probes and deployables is tracked separately and left out of pilot totals by default (toggle available).
  - Friendly fire (damage between pilots on the same side) is shown separately and doesn't count as damage dealt.
- **Hit %:** landed hits ÷ (landed hits + misses), per weapon group, for turrets, drones and fighters only. It is shown with the hit-quality distribution. For pilots who didn't upload, incoming hit % is partial.
- **Reps:** HP restored by remote shield, armor and hull reps. **Cap transfers** are shown separately.
- **Neuts / nos:** GJ neutralized or drained.
- **Jams:** the number of successful jam events in and out. Jams are shown on the timeline as spans when the cycle length can be inferred.
- **Tackle:** warp scramble and warp disruption attempts, counted overall and as unique targets. Counts are exact for pilots who uploaded and partial for everyone else.
- **De-duplication:** logs are first aligned in time. Then any event seen by more than one log is counted once. That includes third-party tackle that several pilots saw.

## Out of scope for v1

Desktop auto-uploader, public or unlisted share links, live in-fight streaming, fit analysis, chat logs, and automated "why we lost" narratives. These are candidates for later versions.

## Acceptance scenarios (to verify once built)

The fight of 2026-10-25 in J-space system 31001176 is the reference. Five of its logs exist locally.

1. **The sample log alone** (`20261025_195145_2113328928.txt`, listener Misterma Niak):
   - One fight, from 20:19 to about 20:45 EVE time.
   - **Friendly:** FFEW.
   - **Hostile:**
     - the six Pyrolanc Squad [-PS-] corps: -G-F, IMBCN, LPCR and NEDX on their own evidence, NINAC and OCBU through their alliance
     - TRX26 and W-SPG
   - **Unknown:** YAGAS, SH-R, T.-R. and NPE. All four only exchanged smartbomb hits with us. YAGAS, whose smartbombs also hit us, carries the "likely friendly" hint.
   - BP RU, with only 2 targeted shots, may come out either Hostile or Unknown.
   - Smartbomb damage to missiles in flight is left out of pilot totals.
   - The "boosted by Loki" lines count as reps in, from a friendly Loki.
2. **All five logs:**
   - They form one fight.
   - The 805 hits between our own pilots are each counted once.
   - Every listener's hull is filled in from the other logs: Sleipnirs for Misterma Niak, Teamsesh and Undocks with PLEX, a Loki for PaidProgramming.
   - The estimated clock offsets are all close to 0 s.
   - PaidProgramming is never credited with the reps Misterma Niak received, because PaidProgramming's own log shows no outgoing reps.
3. **All five logs plus zKillboard:**
   - YAGAS becomes Friendly.
   - SH-R, T.-R., BP RU and CK3 become Hostile.
   - NPE. stays Unknown.
   - The system is 31001176.
   - Kmcpilot's Sabre, lost at 20:14:34 in J-space system 31002133, appears as a related kill. The 20:06:46 Omen Navy Issue loss does not belong to the fight.
   - Luigi mon-de-sound's capsule appears as a friendly-fire loss.
4. **Upload rights.** A log for a character you don't own is rejected without `upload_any_log` and accepted with it. That log never makes its listener's side friendly.
5. **Log toggles.** Toggling a log off as a viewer changes the numbers for you only. Excluding it as a curator changes them for everyone, and the history records it.
6. **Search.** Searching by an enemy corp ticker, an enemy alliance ticker (-PS-) or a ship type finds the fight.
7. **Merging and deleting:**
   - Merging two fights where one has notes keeps the notes, and the absorbed fight's link redirects.
   - Deleting the only log of a curated fight leaves an empty fight with its history.
8. **Discord.** With `post_to_discord`, posting produces one message. Without the permission, the button is not shown.

## Open questions

- The exact click path for the overview ship label setting, for the help page.
- Why some damage lines show a player's bare name instead of `Pilot[CORP](Ship)`.
- How clocks behave across different players' PCs. Per-log offsets handle it either way, but we want to know how often it matters.
- Where the Gamelogs folder is on Mac and Linux clients.
- More ECM samples. Logs from fleet members who fly ECM would help.
