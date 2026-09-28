#!/usr/bin/env python3
"""Aggregate mano run metrics from a runs/ directory of summary.json files.

Usage:  python3 scripts/run-metrics.py [runs_dir]
        (default runs_dir = packs/duolingo/runs)
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path


def main(argv: list[str]) -> int:
    runs_dir = Path(argv[1]) if len(argv) > 1 else Path("packs/duolingo/runs")
    summaries = sorted(runs_dir.glob("*/summary.json"))
    if not summaries:
        print(f"no summary.json under {runs_dir}")
        return 1

    rows = []
    for path in summaries:
        try:
            data = json.loads(path.read_text())
        except json.JSONDecodeError:
            continue
        data["_run"] = path.parent.name
        rows.append(data)

    header = f"{'run':<22}{'result':<26}{'steps':>6}{'calls':>6}{'tokens':>8}{'retry':>6}{'sec':>7}{'cost':>9}"
    print(header)
    print("-" * len(header))
    for r in rows:
        print(
            f"{r.get('_run',''):<22}{str(r.get('result','')):<26}"
            f"{r.get('total_steps',0):>6}{r.get('vlm_calls',0):>6}"
            f"{r.get('total_tokens',0):>8}{r.get('retries',0):>6}"
            f"{r.get('wall_seconds',0):>7}{r.get('est_cost',0):>9}"
        )

    n = len(rows)
    ok = sum(1 for r in rows if r.get("result") == "terminate_success")

    def avg(key: str) -> float:
        return sum((r.get(key) or 0) for r in rows) / n if n else 0.0

    print("-" * len(header))
    print(f"runs={n}  unattended_success={ok}/{n} ({ok / n * 100:.0f}%)")
    print(
        f"avg  calls={avg('vlm_calls'):.1f}  tokens={avg('total_tokens'):.0f}  "
        f"retries={avg('retries'):.1f}  sec={avg('wall_seconds'):.1f}  cost={avg('est_cost'):.4f}"
    )
    print("result breakdown (non-success = guard/limit stops):", dict(Counter(r.get("result", "?") for r in rows)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
