"""Live (uncached) evaluation runs against xAI, for docs/eval.md.

    python scripts/live_eval.py --runs 3 --out out/variance            # main export
    python scripts/live_eval.py --runs 1 --out out/heldout \\
        --export data/heldout/legacy_export.xlsx --truth data/heldout/ground_truth \\
        --reference data/heldout/reference_mapping.json

Each run calls the API (mode "live": fixtures are neither read nor written) and saves
``run_<n>/eval.json`` with the scores, mapping accuracy, cost and measured wall time,
plus the normal run outputs. Costs real money: about $0.77 per run on the main export.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import date
from pathlib import Path

import pandas as pd

from onboard.evals import AS_OF, EXPORT, TRUTH, evaluate
from onboard.llm import LLM
from onboard.mapping import load_mapping
from onboard.pipeline import run, write_run


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=1)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--export", type=Path, default=EXPORT)
    ap.add_argument("--truth", type=Path, default=TRUTH)
    ap.add_argument("--reference", type=Path, default=None)
    ap.add_argument("--mode", default="live", help="live (default) or replay, to dry-run")
    ap.add_argument("--as-of", type=date.fromisoformat, default=AS_OF)
    a = ap.parse_args()
    reference = load_mapping(a.reference) if a.reference else None
    for n in range(1, a.runs + 1):
        out = a.out / f"run_{n}"
        out.mkdir(parents=True, exist_ok=True)
        llm = LLM(mode=a.mode, usage_path=out / "usage.csv")
        t0 = time.perf_counter()
        rr = run(a.export, llm, a.as_of)
        elapsed = time.perf_counter() - t0
        write_run(rr, out)
        e = evaluate(rr, a.truth, reference)
        e["measured_wall_s"] = round(elapsed, 1)
        js = {k: (v.to_dict(orient="index") if isinstance(v, pd.DataFrame) else v)
              for k, v in e.items()}
        (out / "eval.json").write_text(json.dumps(js, indent=1, default=str) + "\n")
        sa = e["scoreboard_after"].loc["TOTAL"]
        print(f"run {n}: {elapsed:.0f}s ${e['usage']['cost_usd']:.3f} "
              f"cols {e['mapping']['columns_correct']}/{e['mapping']['columns_total']} "
              f"fixed {sa['fixed']} removed {sa['removed']} held {sa['held']} "
              f"silent {sa['silent']}", flush=True)


if __name__ == "__main__":
    main()
