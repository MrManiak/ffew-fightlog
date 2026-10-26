# Gamelog formats

This is the inventory the parser (`fightlog/parser/`) is built against. It was taken from a corpus of 16,219 English-client gamelogs (2022-10 to 2026-10, 1,728,346 combat lines). All examples are anonymized with `tools/anonymize.py`: pilot and structure names are fake, while ship types, modules, NPCs and corp and alliance tickers are real.

To reproduce the numbers on your own logs:

```
python tools/corpus_scan.py <Gamelogs folder>             # → tools/out/corpus-report.md
python tools/anonymize.py shapes <out.txt.gz> <Gamelogs folder>
```

The second command writes one anonymized line per line shape. The committed copy (`fightlog/tests/fixtures/lines/shapes.txt.gz`, 2,082 shapes) must parse with no warnings (`ShapesFixtureTest`).

## File

- **Name:** `YYYYMMDD_HHMMSS_<characterID>.txt` (session start in EVE time, listener's character ID). Very old logs have no character ID.
- **Encoding:** UTF-8, sometimes with a BOM. Older clients wrote cp1252, which the reader falls back to.
- **Header:**

  ```
  ------------------------------------------------------------
    Gamelog
    Listener: Pilot 001
    Session Started: 2026.10.25 19:51:45
  ------------------------------------------------------------
  ```

  6,339 files in the corpus have no `Listener:` line and no combat (the client was closed before a character was picked). The upload page skips them.
- **Lines:** `[ YYYY.MM.DD HH:MM:SS ] (type) message`. The time is EVE time (UTC) at 1 s resolution. The types are `combat`, `notify`, `question`, `info`, `hint`, `mining` and `None`.

## Markup

Messages carry client markup: `<color=0xAARRGGBB>`, `<b>`, `<u>`, `<font size=N>`, and `<a href="showinfo:TYPE//ID">`. The reader:

1. Reads the **leading colour** before stripping anything. Neut direction exists only there (below).
2. Strips the tags, *then* decodes `&lt;` and `&gt;`. Overview labels write angle brackets as entities (`Loki &lt;SPORK&gt;[SLAPD] PilotCharlie`), so decoding first would let the tag stripper eat the alliance ticker.
3. Collapses runs of spaces outside brackets only. Tickers may contain spaces (`[J  H]`).

## Actor labels

Two different label styles name the other party.

**Damage and miss lines** always use `Pilot[CORP](Ship)`, whatever the client's settings. The ticker in brackets is the corporation's. Non-pilots use the same shape:

| Label | Actor |
|---|---|
| `Pilot 9377[SMDP](Ferox)` | pilot in a Ferox |
| `'Augmented' Hammerhead[VENIO]('Augmented' Hammerhead)` | drone (name = type) |
| `Advanced Heavy Assault Missile[IMBCN](Scourge Rage Heavy Assault Missile)` | charge hit by a smartbomb |
| `DO6H-Q - Some Hive[B.CI](Keepstar)` | player structure |
| `'Packrat' Mobile Tractor Unit[CRYBB]('Packrat' Mobile Tractor Unit)` | deployable |
| `Centii Plague` | NPC: a bare name |

A bare name can also be a player. One corpus player appears bare in damage lines while using player smartbombs, so a bare name is a pilot when its module is a player module. Otherwise the catalog's NPC names decide (`fightlog/parser/classify.py`).

**All other lines** use the listener's *overview ship label*. It is configured per EVE account (`core_user_<id>.dat`), and an overview profile can bring its own. The fields, their order and their brackets vary. The layouts seen in the corpus, by shape (`S` ship type, `[`/`<`/`(` bracketed token, `b` bare word, `q` quoted ship name):

| Shape | Count | Example | Fields |
|---|---|---|---|
| `S` | 12,056 | `Loki` | ship type only (the default for many players) |
| `S[[[` | 9,425 | `Augoror [PS] [SLUP] [Pilot 051] -` | ship [alliance] [corp] [pilot] |
| `S[` | 5,050 | `Gnosis [Pilot 103]` | ship [pilot] |
| `S[[` | 4,499 | `Basilisk [VI.TA] [Pilot 001]` | ship [ticker] [pilot]: alliance or corp, learned per log |
| `Sb([q` | 45 | `Brutix Pilot 011 (HIREV) [-ENIE] - “Ship”` | ship pilot (corp) [alliance] - “ship name” |
| `S<[b` | 4 | `Loki <SPORK>[SLAPD] PilotCharlie` | the client's stock label: ship <alliance>[corp] pilot |

Special cases:
- **Structures** put their name in the pilot slot: `Astrahus [FRT] [AB - Some Citadel]`.
- **Drones** put their own type there: `Acolyte II [BCAUS] [KS88] [Acolyte II] -`.
- **NPCs** get no label: `Guristas Murderer`.
- **Empty labels:** some rep, cap and nos lines name nobody (`repaired by - <module>`).

`LabelTemplate` learns, per log, which bracket holds which field. It uses pilots whose damage lines identify them exactly. The listener's settings apply to every line of a log, so one template per log is enough.

## Combat lines

`you` is the listener. The examples are tag-stripped.

| Event | Format | Example |
|---|---|---|
| Damage out | `N to <damage label> - <weapon> - <quality>` | `244 to Pilot 9377[SMDP](Ferox) - Heavy Neutron Blaster II - Grazes` |
| Damage in | `N from <damage label> - <weapon> - <quality>` | `33 from Pilot 9495[INOU](Claymore) - Domination Medium Proton Smartbomb - Hits` |
| Damage to a structure | the segment may repeat, plus `(N deflected)` | `211 to DO6H-Q - Some Hive[B.CI](Keepstar) - 250mm Railgun II - Hits - 250mm Railgun II - Hits (211 deflected)` |
| Miss out | `Your <weapon> misses <name> completely - <weapon>` | `Your 'Integrated' Warrior misses Pilot 8469 completely - 'Integrated' Warrior` |
| Miss out (grouped) | `Your group of <weapon> misses <name> completely - <weapon>` | `Your group of 125mm Gatling AutoCannon II misses Pilot 9089 completely - 125mm Gatling AutoCannon II` |
| Miss in | `<name> misses you completely - <weapon>` | `Pilot 5172 misses you completely - Light Neutron Blaster II` |
| Drone miss in | `<drone> belonging to <owner> misses you completely - <drone>` | `'Integrated' Vespa belonging to Pilot 7751 misses you completely - 'Integrated' Vespa` |
| Remote rep in / out | `N remote shield boosted by\|to`, `N remote armor repaired by\|to`, `N remote hull repaired by\|to` + `<label> - <module>` | `308 remote armor repaired by Augoror [PS] [SLUP] [Pilot 051] - - Medium Solace Scoped Remote Armor Repairer` |
| Cap transfer in / out | `N remote capacitor transmitted by\|to <label> - <module>` | `311 remote capacitor transmitted to Basilisk [VI.TA] [Pilot 001] - Large Inductive Compact Remote Capacitor Transmitter` |
| Neut out / in | `N GJ energy neutralized <label> - <module>`: the same text both ways | `50 GJ energy neutralized Jaguar [FRT] [CSQY] [Pilot 075] - - Small Gremlin Compact Energy Neutralizer` |
| Nos out | `+N GJ energy drained from <label> - <module>` | `+8 GJ energy drained from Mercenary Cheetah - Small Ghoul Compact Energy Nosferatu` |
| Nos in | `-N GJ energy drained to <label> - <module>` | `-0 GJ energy drained to Praxis [4CRAB] [Pilot 7042] - - Heavy 'Vrykolakas' Energy Nosferatu` |
| Jammed | `You're jammed by <label> - <module>` | `You're jammed by Blackbird [FRT-U] [VRGOD] [Pilot 194] - - Radar ECM II` |
| Jam out | `<label> - jammed - <module>` | `Draugur [VAULT] [S-4U] [Pilot 795] - jammed - Gloom Enduring Radar ECM` |
| Burst jammer in / out | `Your target locks broken by <label> - <module>` / `<label> target locks broken - <module>` | `Your target locks broken by Tempest - Burst Jammer II` |
| Tackle out | `Warp scramble\|disruption attempt from you to <label>` | `Warp disruption attempt from you to Pilot 5923` |
| Tackle in | `Warp scramble\|disruption attempt from <label> to you!` | `Warp disruption attempt from Gnosis [Pilot 103] - to you!` |
| Tackle seen | `Warp scramble\|disruption attempt from <label> to <label>`: neither side is you | `Warp disruption attempt from Stiletto [CMWTH] [Pilot 279] - to Pilot 5923` |

Details that matter:
- **Hit quality words:** `Wrecks`, `Smashes`, `Penetrates`, `Hits`, `Glances Off`, `Grazes`.
- **Neut direction comes only from the leading colour:** `0xff7fffff` is out (you neuted them), `0xffe57f7f` is in. A neut line in any other colour is not used.
- **Nos direction comes from the sign and the preposition:** `+N … from X` means you drained X, `-N … to X` means X drained you. Amounts of `0` and `-0` are common, because a nos only drains a target whose capacitor percentage is higher than its owner's.
- **Rep and cap amounts** can carry a sign (`-0 remote capacitor transmitted…`).
- **Separators:** `Label - Module` and `Label - - Module` both appear. The double dash comes from labels that end in ` -`. The parser splits on the last ` - ` outside brackets, so names containing ` - ` (structures) survive.
- **The listener's own ship** never appears in its own log. It comes from other logs, killmails and ship-only attribution.
- **Wormhole jumps** are never logged.

Every one of the corpus's 1,728,346 combat lines parses, and so do all 2,082 shapes in the fixture.

Events by kind in the corpus: damage 1,219,976; miss 190,333; cap 79,288; armor reps 59,582; scram 58,257; neut 41,456; shield reps 32,285; disrupt 27,866; nos 17,997; jam 1,306.

## Context lines

Non-combat lines the analysis uses (`fightlog/parser/context.py`):

| Type | Message | Used for |
|---|---|---|
| `None` | `Jumping from <system> to <system>` | the system the listener is in |
| `None` | `Undocking from <station> to <system> solar system.` | the same |
| `question` | `<a href="showinfo:1379//<id>">Name</a> wants you to join their fleet, do you accept?` | fleet invite: side evidence while valid |
| `question` | `Are you sure you want to join the fleet commanded by <a href="showinfo:1376//<id>">Name</a>?` | fleet join |
| `notify` | `Following <name> in warp` | fleet warp |
| `notify` | `Regrouping to <name>` | regroup |
| `notify` | `<name> has been invited to your fleet as …` | fleet member |
| `notify` | `Disembarking from ship` | reship: the listener's hull changes |
| `notify` | `Starting clone jumping` | clone jump |
| `notify` | `<module> deactivates as <victim> begins to explode.` | a kill timestamp straight from the log |
| `notify` | `Local spatial phenomena may cause strange effects…` | wormhole-space hint |
| `notify` | wormhole mentions (`entering a wormhole`, `nearby Wormhole`…) | wormhole hint |

## Non-English clients

The client translates the message text (`Schaden … an …`). The parser only knows English:
- The upload page skips a file with no English-looking combat line.
- The server rejects a log, with a message asking for the English client, when fewer than 20% of its 20 or more combat lines match.

No non-English log was in the corpus.
