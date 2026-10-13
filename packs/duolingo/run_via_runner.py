"""mano · Duolingo via the thin runner — A/B harness against baseline run.py.

Same lesson, same evidence layout (step-NN.png/xml + steps.jsonl + summary.json),
but driven through mano.runner + DuolingoPack instead of the in-file loop. The
baseline packs/duolingo/run.py is left completely untouched.

Note: the thin runner v0 has no solver routing, so matching / ordering exercises
run through the pure VLM loop here. Cost estimation lives in this caller, not in
the runner.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from mano import runner
from mano.adb_executor import Adb
from mano.gui_plus_client import GuiPlus
from packs.duolingo.pack import DuolingoPack


PACKAGE = "com.duolingo"
MAX_STEPS = 40
STEP_TIMEOUT_S = 900
SETTLE_S = 1.0


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="mano · Duolingo via thin runner (A/B)")
    parser.add_argument("--device", default=os.getenv("MANO_DEVICE_SERIAL", ""))
    parser.add_argument("--output", default=str(_HERE / "runs"))
    parser.add_argument("--cold-start", action="store_true", help="冷启动多邻国(默认否:由人类先进到课起点)")
    args = parser.parse_args(argv)

    serial = args.device or ""
    if not serial:
        raise SystemExit("需要设备 serial:设 MANO_DEVICE_SERIAL 或传 --device(如 127.0.0.1:16384)")
    adb = Adb(serial)
    runner.ensure_adb_ready(adb)
    client = GuiPlus()
    wall_start = time.time()
    run_dir = Path(args.output).resolve() / f"duo-{_now_id()}"
    run_dir.mkdir(parents=True, exist_ok=False)
    (run_dir / "environment.json").write_text(
        json.dumps(
            {
                "device": serial,
                "package": PACKAGE,
                "model": client.model,
                "endpoint": client.endpoint,
                "max_steps": MAX_STEPS,
                "cold_start": bool(args.cold_start),
                "engine": "mano.runner",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    if args.cold_start:
        adb.cold_start(PACKAGE)
        time.sleep(2)

    res = runner.run(
        adb,
        client,
        DuolingoPack(),
        run_dir,
        max_steps=MAX_STEPS,
        step_timeout_s=STEP_TIMEOUT_S,
        settle_s=SETTLE_S,
    )

    price_in = float(os.getenv("MANO_PRICE_IN_PER_1K", "0") or 0)
    price_out = float(os.getenv("MANO_PRICE_OUT_PER_1K", "0") or 0)
    est_cost = round(
        res.prompt_tokens / 1000 * price_in + res.completion_tokens / 1000 * price_out, 6
    )
    total_tokens = res.prompt_tokens + res.completion_tokens
    (run_dir / "summary.json").write_text(
        json.dumps(
            {
                "result": res.result,
                "total_steps": res.total_steps,
                "model": client.model,
                "endpoint": client.endpoint,
                "vlm_calls": res.vlm_calls,
                "retries": res.retries,
                "prompt_tokens": res.prompt_tokens,
                "completion_tokens": res.completion_tokens,
                "total_tokens": total_tokens,
                "actions_executed": res.actions_executed,
                "est_cost": est_cost,
                "wall_seconds": round(time.time() - wall_start, 1),
                "run_dir": str(run_dir),
                "engine": "mano.runner",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(
        f"RESULT={res.result} steps={res.total_steps} vlm_calls={res.vlm_calls} "
        f"tokens={total_tokens} retries={res.retries} "
        f"sec={res.wall_seconds} cost={est_cost} run_dir={run_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
