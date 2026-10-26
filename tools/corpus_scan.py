"""Run the parser over a Gamelogs folder and report what it understood.

Usage:
    python tools/corpus_scan.py "C:\\Users\\<you>\\Documents\\EVE\\logs\\Gamelogs" [report.md]

The report contains real pilot names, so it is written under tools/out/ (gitignored)
unless another path is given. Use it to find line shapes the parser doesn't know yet.
"""

import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fightlog.catalog import load_catalog  # noqa: E402
from fightlog.parser import ActorKind, Flag, parse_log  # noqa: E402


def scan(folder: Path):
    catalog = load_catalog()
    totals = Counter()
    warnings = Counter()
    warning_examples = {}
    kinds = Counter()
    actor_kinds = Counter()
    shapes = Counter()
    unknown_names = Counter()
    context = Counter()
    non_english = []
    for path in sorted(folder.glob("*.txt")):
        parsed = parse_log(path.read_bytes(), catalog)
        totals["files"] += 1
        if not parsed.listener:
            totals["files_without_listener"] += 1
            continue
        totals["combat_lines"] += parsed.combat_lines
        totals["matched_lines"] += parsed.matched_lines
        if not parsed.is_english:
            non_english.append(path.name)
        for shape, n in parsed.warnings.items():
            warnings[shape] += n
            warning_examples.setdefault(shape, path.name)
        shapes.update(parsed.label_shapes)
        for ctx in parsed.context:
            context[ctx.kind] += 1
        for ev in parsed.events:
            kinds[ev.kind.name] += 1
            if ev.flags & Flag.OBSERVED:
                totals["observed_events"] += 1
            for actor in (ev.src, ev.tgt):
                actor_kinds[actor.kind] += 1
                if actor.kind == ActorKind.UNKNOWN:
                    unknown_names[actor.name or actor.raw] += 1
    return totals, warnings, warning_examples, kinds, actor_kinds, shapes, unknown_names, context, non_english


def main(argv):
    folder = Path(argv[1])
    out = Path(argv[2]) if len(argv) > 2 else Path(__file__).resolve().parent / "out" / "corpus-report.md"
    start = time.time()
    totals, warnings, examples, kinds, actor_kinds, shapes, unknown, context, non_english = scan(folder)
    matched = totals["matched_lines"] / max(totals["combat_lines"], 1)
    lines = [
        "# Corpus report",
        "",
        f"- files: {totals['files']} ({totals['files_without_listener']} without a listener)",
        f"- combat lines: {totals['combat_lines']}, matched: {totals['matched_lines']} ({matched:.4%})",
        f"- observed third-party events: {totals['observed_events']}",
        f"- non-English files: {len(non_english)} {non_english[:10]}",
        f"- scan time: {time.time() - start:.0f} s",
        "",
        "## Unmatched line shapes (count ≥ 1)",
        "",
    ]
    lines += [f"- {n} × `{shape}` (e.g. {examples[shape]})" for shape, n in warnings.most_common(60)]
    lines += ["", "## Events by kind", ""] + [f"- {k}: {n}" for k, n in kinds.most_common()]
    lines += ["", "## Actors by kind", ""] + [f"- {k}: {n}" for k, n in actor_kinds.most_common()]
    lines += ["", "## Overview label shapes", ""] + [f"- `{k}`: {n}" for k, n in shapes.most_common(30)]
    lines += ["", "## Context events", ""] + [f"- {k}: {n}" for k, n in context.most_common()]
    lines += ["", "## Most common unknown actors", ""] + [f"- {n} × {k}" for k, n in unknown.most_common(40)]
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"matched {matched:.4%} of {totals['combat_lines']} combat lines; "
        f"{sum(1 for n in warnings.values() if n >= 3)} unmatched shapes with ≥ 3 lines; report: {out}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
