# Response to the 2026-10-26 review

Date: 2026-10-26
Responds to: [review-2026-10-26.md](review-2026-10-26.md). For each finding I tried to disprove it. Where I couldn't, I propose a fix.

**Status:** applied on 2026-10-26. The [product spec](product-spec.md) and [technical plan](technical-plan.md) are now v1.1 and include every fix in §5. One decision goes beyond this document: the plan drops django-eveuniverse entirely. It uses a bundled SDE type catalog plus a small solar-system cache filled from ESI.

## How I checked

- **Whole corpus:** scripted passes over all 16,219 files in `Documents\EVE\logs\Gamelogs` (2022-10-04 to 2026-10-26).
- **Five logs of the same fight.** The reviewer used one. The user multiboxed the 2026-10-25 fight, and logs exist from Misterma Niak (the sample), Teamsesh, Undocks with PLEX, PaidProgramming and Censys. All five characters are in FFEW.
- **Live data:** ESI (`/universe/ids`, `/characters/affiliation`, `/corporations`, `/alliances`) and zKillboard `corporationID/840323545/pastSeconds/259200`. The latter returned 24 killmails between 20:00 and 21:00, 22 of them in J-space system 31001176.
- **celery-once source:** `celery_once/tasks.py` on GitHub, read together with AA's `ONCE` config in `project_template/.../celery.py`.
- **Two experiments:**
  - a pure-Python aggregation benchmark at the plan's design scale
  - a simulation of the plan §5.4 side guesser on the real fight, run once with the plan's weights and once with the corrected rules proposed below

## Verdicts

| # | Finding | Verdict | Fix (detail below) |
|---|---|---|---|
| 1.1 | Bracket ticker is the corp | **Confirmed** for `Pilot[TICK](Ship)`. "Logs only ever give corp tickers" is **disproved** | Parse alliance tickers when labels carry them; resolve alliances through ESI |
| 1.2 | Systems appear in logs | **Confirmed**. "Log first, zKill second" is **disproved** | zKill first; use the log as a guarded fallback |
| 1.3 | Non-combat lines matter | **Confirmed**. As the remedy for YAGAS it is **disproved** | Parse them, time-scoped |
| 1.4 | Format inventory incomplete | **Confirmed**, with three corrections | Corpus-driven M1 (already planned) |
| 1.5 | Neut direction only in colour | **Confirmed** | Keep the leading colour token |
| 1.6 | ECM data is sparse | **Confirmed** (the count was low) | Don't gate M3 on jam samples |
| 2.1 | YAGAS hostile at full confidence | **Confirmed**, and worse | AoE is never hostile evidence |
| 2.2 | Killmails poisoned by friendly fire | **Confirmed**. The proposed fix is **disproved** | Flag attackers whose weapon is their own hull as AoE-suspect |
| 2.3 | zKill window misses the start | **Confirmed**. The proposed fix needs a system guard | Expand the window by shared participants; mode decides the system |
| 2.4 | QueueOnce drops rebuilds | **Confirmed** | `unlock_before_run` + per-fight mutex + version check |
| 2.5 | 1 s skew assumption | **Partly confirmed**: one PC already needs ±2 s | Estimate an offset for each log; match within ±2 s |
| 2.6 | PvP filter misclassifies | Hull-name part **confirmed**. Pre-filter part mostly **disproved** | Bundled NPC catalog plus line-shape signals; looser pre-filter |
| 2.7 | Columns not observable | **Partly disproved**: tackle by logged pilots is observable | Cell states; hit % only for turrets, drones and fighters |
| 3.1 | Permissions disagree | **Partly disproved** (wording only). The edge cases are **confirmed** | Deletion, duplicate and seeding rules |
| 3.2 | Auto-merge destructive | **Partly disproved**: curated fights are already exempt. **Confirmed** that "curated" and merge semantics are undefined | `is_curated` = any human edit; merge rules; redirects |
| 3.3 | Listener's hull unknown | **Confirmed**, but other logs and killmails supply it | Hull timeline with a source for each entry |
| 3.4 | `ShipType [Pilot]` label setting | **Confirmed**, with caveats | Help-page headline; elimination using logged pilots |
| 3.5 | Install burden | Dropping the map load is **confirmed**. Dropping NPC types is **disproved** | Ship an SDE-derived type catalog in the package |
| 3.6 | Performance target optimistic | **Disproved** (0.14 s pure Python) | No numpy |
| 3.7 | Old logs are mainstream | **Disproved** | Name fallback stays an edge case |
| 3.8 | Smaller items | 4 confirmed, 2 disproved | See §3.8 |

## 1. Log contents

### 1.1 Corp vs alliance tickers

**Confirmed for damage labels.** ESI shows:
- Corporation 840323545 is The Forsakened Few [FFEW], in alliance 99011699, We Forsakened Few [WEFEW].
- Current affiliations match the log brackets: `Jao I[IMBCN]` belongs to Imbicillin [IMBCN] (Pyrolanc Squad), `LETUSDIG[YAGAS]` to Baba Yagas [YAGAS], and `Teamsesh[FFEW]` to FFEW.

**"Logs only ever give corp tickers" is disproved.**
- `You're jammed by Astrahus [FRT] [JN - Planet Fitness]` carries **[FRT]**, which ESI gives as the ticker of the alliance Fraternity. (alliance 99003581).
- A 2022 log has `Carlstarenf98[HIREV](Brutix)` in damage lines and `Brutix Carlstarenf98 (HIREV) [-ENIE] - "PotOMami"` in neut lines. HIREV is the corp, and pilots from two different corps (HIREV, FWS) share `[-ENIE]`, so that is an alliance ticker.
- The full overview label therefore includes the alliance ticker. The memory note `eve-gamelog-format-facts` repeats the wrong claim.

**Also new, and it matters for sides:**
- YAGAS is in **another alliance**, The Initiative. (alliance 1900696668).
- The FC whose fleet the listener joined after the fight, Azeroth Uluntil, is in **FFEW Associates**, a separate alliance.
- Alliance grouping alone will therefore never put YAGAS with FFEW.

**Fix:**
- Treat `[TICK]` in `Pilot[TICK](Ship)` as the corp ticker.
- Parse alliance tickers when a full label provides them.
- Resolve alliances through affiliation lookups, and add `GetCorporationsCorporationId` and `GetAlliancesAllianceId` to the ESI operations, caching tickers in our own table.
- Group by alliance when it is known, otherwise by corp.
- Correct the spec's naming.

### 1.2 Solar systems

**Confirmed:**
- There are 71,299 `(None) Jumping from` lines and 4,066 `Undocking from` lines.
- 262 of 2,960 2026 files contain a jump and 169 an undock, which matches the review.
- Wormhole jumps are not logged: none of the 71,299 jump lines involves a J-space system or Thera. The only Thera line in the whole corpus is an undock.

**The remedy order ("log first, zKill second") is disproved by the sample itself:**
- The sample has **no** system line before the fight.
- Its only system line is `Undocking from Nema X - Moon 6 - Hedion University to Nema solar system` at 21:00:46, after a trip out through wormholes.
- A "last known system" rule would put a J-space fight in a high-sec system.

**Fix**, in order of trust:
1. Manual entry by a curator.
2. The most common system among the fight's killmails.
3. A log system line, used only if it is the most recent one before the engagement and no w-space indicator appears between it and the fight. The UI labels it "from log".
4. "J-space, system unknown", when w-space indicators are present but nothing else is known.

W-space indicators found in the sample:
- `Local spatial phenomena may cause strange effects on your ship systems`
- `spatial disturbances caused by the wormhole 6000 m away`

### 1.3 Non-combat lines

**Confirmed.** The reviewer's counts are exact: Azeroth ×8, Adrianna ×6, PaidProgramming ×5, regroup ×2, disembark ×2, clone jump ×3.

**As the cure for YAGAS, disproved:**
- During the fight (20:19–20:45) the fleet lines name only Adrianna Utani and PaidProgramming. Both are FFEW, which is already friendly.
- The Azeroth Uluntil fleet was joined at 20:57:09, after the fight.
- Fleet lines are good friendly evidence, but they must be **time-scoped** and they won't resolve this fight.

**New details:**
- Fleet `(question)` lines embed character IDs: `showinfo:1379//2120273592`, `showinfo:1376//999340188`. These are exact IDs with no ESI lookup needed.
- `(notify) 425mm AutoCannon II deactivates as ✖‿✖ begins to explode` gives a kill timestamp straight from the log.

**Fix:** parse `(None)`, `(notify)` and `(question)` lines for:
- fleet invites, joins, warps and regroups (friendly edges, only within the time a fleet membership is valid)
- system lines (§1.2)
- reships and clone jumps (they close a hull interval, §3.3)
- kill notices (timeline markers when zKill is missing)
- w-space indicators

### 1.4 Format inventory

**Confirmed.** Every shape in the review exists in the corpus. There are three corrections and two additions:

- **The NPC "Orthrus" comes from a Sleeper site, not a Triglavian one.** `20250818_222430_2122740900.txt` (Sleepless Warden, Awakened Sentinel …) has 217 such lines.
- **Outgoing jams are not only from structures.** There are 111 lines of `Mercenary Crow jammed - BZ-N Scoped Gravimetric ECM`, a ship module.
- **The review's count of about 500 jam lines is low.** There are 979 (§1.6).
- **The bracket can hold a drone type instead of a pilot:** `remote armor repaired by Medium Armor Maintenance Bot I [Medium Armor Maintenance Bot I]` ×1,411.
- **A double-separator variant exists:** `Ikitursa [Cameron Utama] - - Centii A-Type …` ×1,079.

**Fix:** as planned, M1 builds the inventory from the corpus. The spec should list formats as "known so far" rather than as facts.

### 1.5 Neut direction

**Confirmed.** The texts are identical:
- `N GJ energy neutralized X - Module`, with leading colour `0xff7fffff`, is outgoing (4,858 lines).
- The same text with leading colour `0xffe57f7f` is incoming (36,598 lines).

**Fix:** the reader keeps the first `<color=…>` token of each line as a field, and `patterns.py` uses it for neuts. The other kinds also carry text direction, so the colour serves as a cross-check for them.

### 1.6 ECM

**Confirmed, though the count was low.** There are 979 jam lines across the corpus, about 100 of them from player ECM ships (Kitsune 19, Scorpion 18, Widow 17, Falcon 16, Tengu 15) and the rest NPC, drone or structure.

The plan doesn't gate any milestone on jams except §12's "ask fleet members for sample logs before M3".

**Fix:**
- Keep the jam features. The user asked for them.
- Collect samples opportunistically.
- Remove the pre-M3 dependency.

## 2. Design choices

### 2.1 YAGAS from logs alone

**Confirmed, and the problem is general.** Across all five logs:
- YAGAS ↔ our pilots: **5,206 AoE events and 0 targeted ones**.
- FFEW ↔ FFEW: 5,105 AoE events, also 0 targeted. This is the smartbomb fleet hitting itself.
- Every enemy corp except NPE. has targeted interactions with our pilots:
  - in both directions: LPCR, -G-F, IMBCN, NEDX, TRX26
  - only our fire: W-SPG, BP RU
  - only theirs: OCBU, SH-R, NINAC, T.-R.

With the plan's ×0.05 weighting, YAGAS goes hostile with confidence 1.0, as the review says.

**Fix:**
- AoE is **never** hostile evidence (agreed).
- Fleet lines count as friendly edges (agreed, time-scoped), though they don't help in this fight (§1.3).
- Acceptance: from logs alone, YAGAS is Unknown. Logs plus killmails put YAGAS on the friendly side, which the simulation in §2.2(b) reproduces.
- Optional weak-friendly signal: a group that exchanges **mutual** AoE with our pilots and receives **no** targeted fire from any of our logged pilots, while they are using targeted weapons on others at the same time. YAGAS fits this. NPE. does not: it was only hit by our smartbombs and never hit us.

### 2.2 Killmail friendly fire

**Confirmed.** The reviewer's attacker counts match exactly.
- FFEW+YAGAS attackers did 6–55% of the damage on their own side's ship losses. On ChuckNorriss bodygaurd's YAGAS Sleipnir at 20:40:23, YAGAS Sleipnirs did 28.6%, FFEW Sleipnirs 15.2% and other FFEW/YAGAS attackers 10.9%, 54.7% in total.
- Luigi mon-de-sound's FFEW capsule (20:32:04) was killed **entirely** by friendly smartbombs: all 3 attackers are FFEW or YAGAS.

**The proposed fix (detect smartbombs via `weapon_type_id`, weight by damage share) is disproved:**
- Of the 264 FFEW/YAGAS attacker entries on the fight's killmails, **201 record the attacker's own hull as the weapon** (`Sleipnir/Sleipnir`). Only 2 record `Domination Medium Proton Smartbomb`, and 61 record real modules.
- Our logs show that every hit these pilots landed on us was a smartbomb.
- Damage-share weighting doesn't help either, because own-side shares reach 55%, or 100% for the capsule.

**Corrected fix:**
- Mark a killmail attacker **AoE-suspect** if any of these hold:
  - `weapon_type_id == ship_type_id`
  - the weapon is a smartbomb or bomb type
  - every shot our logs recorded from that pilot was AoE
- Drop AoE-suspect attackers from killmail side edges, and weight the remaining ones by damage share.
- The cost is small: 15 of 245 enemy attacker entries are also weapon = hull and get dropped.

**Simulation** (corp-level groups, FFEW seeded friendly, the five logs plus 22 killmails in 31001176):

| Rules | Friendly side | Hostile side | Unknown |
|---|---|---|---|
| (a) Plan §5.4 as written | FFEW, YAGAS **and** -G-F, CK3, IMBCN, LPCR, NEDX, NINAC, NPE., OCBU, SH-R, T.-R., TRX26, W-SPG | BP RU, M-F-S | — |
| (b) Corrected (AoE ignored in logs, AoE-suspect attackers dropped, damage-share weights) | FFEW, YAGAS, M-F-S | -G-F, BP RU, CK3, IMBCN, LPCR, NEDX, NINAC, OCBU, SH-R, T.-R., TRX26, W-SPG | NPE. |

- The plan's co-attacker bonus (+3 per attacker pair) pulls almost every enemy corp onto our side. That is worse than the review predicted.
- M-F-S (Mongolian Fire Squad) appears only on the 20:06:46 Omen Navy Issue kill, 13 minutes before the fight. My test window started at 20:00, and the window rule in §2.3 would exclude that kill.

### 2.3 zKill window

**Confirmed.** Kmcpilot's FFEW Sabre died at 20:14:34, while the sample's first PvP event is at 20:19:25.

**The proposed fix needs a guard.** The Sabre died in **31002133**, a different J-space system, to three pilots (Mrs Lesso, De Santisima Trinidad, Velcro Xo) who then joined the main fight. Pulling it in is good for the post-mortem, but it must not decide the fight's system.

**Fix:**
1. Start from killmails inside the log window [start − 2 min, end + 5 min].
2. Expand in up to two rounds: add killmails within ±15 min that share at least two participants with the fight.
3. Set the fight's system from the most common system in step 1 only.
4. Show kills from other systems as "related".

This rule takes in the Sabre (three shared participants) and leaves out the 20:06:46 Omen kill (only one: the victim).

### 2.4 Rebuild coalescing

**Confirmed from source:**
- celery-once takes the lock in `apply_async` and clears it in `after_return`. With `unlock_before_run` at its default of `False`, the lock is held from enqueue until the task finishes.
- AA sets `ONCE = {"backend": …DjangoBackend, "settings": {}}`, so the lock timeout is the default of 1 hour.
- A request that arrives while a rebuild is running is rejected (graceful) or raises `AlreadyQueued`.
- Note: the reviewer's own fix, a task re-enqueuing itself at the end, would *also* be rejected, because the task's own lock is still held at that point.

**Fix:**
- Use `once={"keys": ["fight_id"], "graceful": True, "unlock_before_run": True}`. Requests that arrive while a rebuild is queued still merge into it, and a request during a run queues exactly one follow-up.
- Inside the task, take a non-blocking per-fight `cache.lock`. If it's held, call `self.retry(countdown=10)`.
- Add `Fight.data_version`, bumped by every ingest or edit, and `Fight.built_version`, set when a rebuild completes. A rebuild that finds them equal exits immediately.

### 2.5 Clock skew

**Partly confirmed:**
- Matching the five same-PC logs pairwise (A's "to B" against B's "from A") paired all 805 events. The time differences were 0 s for 788, ±1 s for 15, and **+2 s for 2**. So the plan's ±1 s already misses 0.25% of events on a single machine.
- Against server time: of 14 victims whose pilot and hull appear in our logs, none was hit after their `killmail_time`, and the four closest last hits came 0–1 s before death. So this PC's log clock is within about 1 s of the server.
- Whether gamelog timestamps come from the server or the PC's clock is still unknown, so skew between different PCs is unverified.

**Fix:**
- Estimate an offset for each log over ±10 s, mainly from matches with other logs.
- Sanity-check it against killmails: no hits after a victim's death.
- Match within ±2 s around the offset.
- Flag logs with |offset| > 2 s.

### 2.6 PvP classification

**Hull-name part confirmed, and harder than either doc assumes:**
- ESI **resolves NPC names to player characters**: "Emergent Warden", "Starving Damavik" and "Upgraded Avenger" are all real characters, and "Orthrus" is both a character and the ship type. Name resolution can never classify bare names.
- NPC missile lines carry weapon segments, e.g. `from Gist Cherubim - Nova Cruise Missile` (14,627 lines), and Nova Cruise Missile is also a player charge.
- The only reliable one-way signal is an **incoming line with no weapon segment**, which means NPC: 286,587 lines, and labelled actors never lack a weapon.

**Pre-filter part mostly disproved:**
- Bare-name players appear only in PvE logs in this corpus (ThinkBigShiite smartbombing in Sleeper sites).
- "Pre-late-2022 logs have no labels" is an artifact: the corpus starts on 2022-10-04, and labels appear in its first PvP logs (`Carlstarenf98[HIREV](Stiletto)`, 2022-10-21).
- The review's "2023-01-26 PvP log" (`20230126_000759_2120273592.txt`) is a Sleeper site with zero player-label, tackle, jam or drone-owner lines.

**Fix:**
- Classify in this order:
  1. Label shape: a labelled actor is player-owned, and its `(Ship)` part says whether it is a ship, drone, charge or structure.
  2. A bundled NPC type-name catalog (§3.5).
  3. Context: incoming lines without a weapon mean NPC, and a name seen labelled anywhere in the fight is a player.
- Browser pre-filter: session date range (default: last 3 days), at least one `(combat)` line, and English. The server decides what is PvP.

### 2.7 Observability

**Partly disproved:**
- **Tackle by logged pilots is observable.**
  - `Warp scramble attempt from you to Drake`-style lines appear in nearly every month since October 2022, up to 316 a month. Tackle on the listener reads `… to you!`. The markup sits between "to" and "you!", so search for it after stripping tags.
  - Second logs can also name the target of an observed tackle. At 20:26:30, Teamsesh's log shows `Nighthawk → Sleipnir` and Undocks with PLEX's log shows `Nighthawk → you!` in the same second.
  - So "points applied" and "points received" are fillable per pilot for our logged pilots. The sources stay ship-only.

**Confirmed:**
- Unlogged pilots' tackle, enemy reps, neuts and jams are only seen when they land on a logged pilot.
- Hit % is meaningless for missiles (210,072 lines, all "Hits") and smartbombs (109,049 lines, all "Hits").
- Logs alone are star graphs around friendly listeners, so third-party sides need killmails.

**Fix:**
- Every cell gets a state: observed, partial or not observable, based on `has_log` and how well the actor was resolved.
- Hit % only for turrets, drones and fighters, and n/a for everything else.
- The spec states that sides beyond friendly and hostile come from killmails.

## 3. Gaps

### 3.1 Permissions

**Partly disproved.** The two docs agree on the design: spec §8 and plan §8 both let contributors exclude logs and merge or split fights, which is the user's decision. Only the spec's log-panel sentence says "Curators".

**The edge cases are confirmed.** **Fix:**
1. Fix the wording.
2. A merge requires edit rights on both fights.
3. A log can be deleted by its uploader, by the owner of its listener character, or by a curator. Every deletion writes a `FightChange` entry.
4. A fight emptied by deletion is kept as an empty shell if it has any curation (manual title, notes, tags, history, Discord posts).
5. Duplicate uploads add the second uploader to a `CombatLogUpload` M2M, so the log shows on both users' "My logs".
6. Friendly seeding comes only from Auth-owned listeners and `FriendlyEntity`.

### 3.2 Auto-merge

**Partly disproved.** Plan §4.4 already never auto-merges a fight that is marked curated.

**Confirmed:**
- `is_curated` is only set by splits, so a renamed, annotated or side-fixed fight can still be absorbed.
- Merge semantics are undefined.

**Fix:** `is_curated` becomes "has any `FightChange`". When fights merge:

| Field | Rule |
|---|---|
| Surviving fight | The curated one; otherwise the one with more logs |
| Title | The manual title if there is one, otherwise regenerated |
| Notes | Concatenated, with a separator |
| Tags | Union of both |
| History and Discord posts | Moved to the surviving fight |
| Side overrides | Carried over by participant key; on conflict the surviving fight wins, and the conflict is logged |
| Old fight's ID | Kept in a `FightRedirect` row, so old links and Discord posts still resolve |

### 3.3 Listener's hull

**Confirmed** that a listener's own log never names their hull. **In practice it can be filled in:**
- Other logs name every listener's hull in this fight: Misterma Niak flies a Sleipnir (539 lines), Teamsesh a Sleipnir (545), Undocks with PLEX a Sleipnir (427), PaidProgramming a Loki (99).
- Killmails do too, through the attacker's `ship_type_id`.

**Fix:**
- Each participant gets a hull timeline: intervals with a source (peer log, killmail, inferred from modules).
- Reship and clone-jump lines close an interval.
- `ParticipantShip` gains `source`, `from` and `to` fields.

### 3.4 The `ShipType [PilotName]` setting

**Confirmed:** there are 53,728 bracket-label lines, and `Razeleao` resolves to an FFEW character.

**Caveats:**
- The bracket can hold a drone type instead of a pilot (§1.4).
- The setting only helps the listener who has it switched on.
- The logs don't tell us which client setting produces this format, so it needs an in-client test before the help page is written.

**New lever:** elimination using logged pilots.
- None of the five fight logs has a single outgoing rep line, and PaidProgramming (the only logged Loki) was itself receiving reps from a Loki.
- So the Loki repping Misterma Niak was unlogged. The best candidate is DR ZZZZ, the other FFEW Loki on the killmails (lost at 20:40:36).

**Fix:**
- Make the setting the help page's headline once the test identifies it.
- Parse the format, with tests.
- In ship-only resolution, remove a logged candidate when their own log shows no matching outgoing event at that second.

### 3.5 Install burden

**Dropping the map load is confirmed.** Systems can load lazily.

**Dropping NPC types is disproved:**
- "Identify NPCs by exclusion (no ticker, not a known ship, drone or charge type)" misclassifies both of the review's own examples: the NPC "Orthrus" *is* a ship-type name, and the bare-name player ThinkBigShiite *is not* a type.
- ESI can't stand in for the catalog either (§2.6).

**Fix:**
- At release time, `tools/build_type_catalog.py` generates a type-name catalog from CCP's SDE and bundles it in the package (gzipped JSON). It covers ships, weapon modules, charges, drones, fighters, deployables, structures and NPC entities, a few hundred KB in total.
- The admin runs no load commands.
- eveuniverse is used only for lazy `EveSolarSystem` lookups, or can be dropped in favour of a small cache model.

### 3.6 Performance

**Disproved.** A single-pass pure-Python aggregation computes, over 250,000 packed events:
- the timeline (16 series)
- per-pilot totals
- the targets table
- hit and miss counts
- weapon breakdowns
- dedupe across logs

With 50 logs, 45 of them active, and 300 actors, it takes **0.14 s** on this PC (Python 3.14). Python 3.10 on a small VPS might be 3–5× slower, which is still under 1 s. numpy isn't needed, and the 2 s target stands.

### 3.7 Old logs

**Disproved:**
- None of the 6,338 suffix-less files contains a `Listener:` line or a combat line.
  - 5,438 are header-only (≤ 200 B, character-select sessions).
  - The rest hold only quit prompts or connection errors.
- Only 1 of the 9,881 suffixed files lacks a listener line.
- The corpus starts on 2022-10-04.

**Fix:** keep the name-based fallback for renamed files as an edge case. The pre-filter still must not require ticker labels, for the reasons in §2.6.

### 3.8 Smaller items

| Item | Verdict | Fix |
|---|---|---|
| "FFEW" hard-coded in titles | Confirmed | `FIGHTLOG_FRIENDLY_LABEL` setting, defaulting to the friendly side's largest alliance ticker |
| "No PvP, not stored" vs re-parsing | Confirmed | Keep `no_pvp` logs for 30 days (a setting), re-check them on parser upgrades, then purge |
| Supersede check vs a partial last line | **Disproved** if bytes are compared. An append-only file copied mid-write is always a byte prefix of the later copy | Compare raw bytes, never decoded text. A new file that is a prefix of the stored one is a no-op |
| Ship-only keys include the side | Confirmed | Key ship-only actors by (log, raw label, interaction polarity). Overrides become attribution overrides (actor → pilot) keyed on (log, raw label) |
| Mac and Linux log paths | Confirmed | Research them in M8 |
| One pathological log overflowing its blob | **Disproved** | Nothing to change. The largest real log is 8.8 MB raw and the upload cap is 25 MB. At about 36 bytes per event against about 235 bytes per raw line, a capped log stays under 4 MB *uncompressed*, below the 8 MB limit on compressed blobs |

## 4. New findings this pass

1. **A five-log fixture of one fight exists.** It is the best M1–M3 test set we could ask for once anonymized: dedupe, offsets, sides, hulls and elimination can all be tested on it.
2. **Killmails usually record the hull as the weapon for smartbombers** (201 of 264 FFEW/YAGAS attacker entries, §2.2). This affects every killmail-based signal, not just sides. A pure friendly-fire kill (Luigi's capsule) exists in this fight.
3. **The plan's co-attacker bonus breaks the side guesser on real data** (§2.2a).
4. **ESI name resolution turns NPC names into players** (§2.6). Never use it to classify a bare name.
5. **Observed third-party lines repeat across logs.** They need dedupe, and when the tackled pilot logged, matching recovers the target (§2.7).
6. **Fleet `(question)` lines carry character IDs**, and `begins to explode` notices carry kill times (§1.3).
7. **None of the five fight logs contains an outgoing rep.** The fight's logi was unlogged, so "Logged?" coverage will be the norm for logi (§3.4).

## 5. Changes to make (replaces review §4)

1. **Corp and alliance tickers:** use corp tickers from damage labels and alliance tickers from full labels. Add the corporation and alliance info ESI operations. Group by alliance, falling back to corp. Fix the spec's naming of We Forsakened Few [WEFEW] and FFEW. (§1.1)
2. **Parser scope:** parse `(None)`, `(notify)` and `(question)` lines (fleet, system, reship, clone jump, kill notices, w-space indicators). Keep each combat line's leading colour token. (§1.2, §1.3, §1.5)
3. **Solar system:** zKill first, the log only behind the w-space guard. (§1.2)
4. **Side guessing:** AoE is never hostile evidence. Killmail attackers are AoE-suspect when their weapon is their own hull, a smartbomb or a bomb. Weight killmail edges by damage share. Fleet edges are time-scoped. The spec drops "N sides from logs". (§2.1, §2.2, §2.7)
5. **Rebuilds:** `unlock_before_run` plus a per-fight mutex plus `data_version`/`built_version`. (§2.4)
6. **Dedupe:** estimate an offset for each log, match within ±2 s, and dedupe observed third-party events too. (§2.5, §4.5)
7. **NPC/player classification:** a bundled SDE type catalog plus line-shape rules, and a looser browser pre-filter. No load commands for the admin. (§2.6, §3.5)
8. **Acceptance scenario 1 and M3:**
   - Sample log alone: FFEW friendly; every corp with targeted interactions hostile; YAGAS and NPE. Unknown.
   - All five logs plus killmails: YAGAS friendly; NPE. still Unknown.
   - The Sabre at 20:14:34 is shown as a related kill.
   - The system is 31001176. (§2.1–2.3)
9. **Permissions, merging and deletion** as in §3.1 and §3.2.
10. **Observability:** cell states, hit % only for turrets, drones and fighters, and logged-pilot tackle columns. (§2.7)
11. **Help page:** headline the label setting once an in-client test identifies it. Resolve ship-only actors by eliminating logged pilots. (§3.4)
12. **Unchanged:** the 2 s performance target with pure Python, raw-byte supersede checks, and the blob limits. (§3.6, §3.8)
