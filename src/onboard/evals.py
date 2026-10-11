"""Evaluate a full `onboard run` against the answer key (used by tests and the README)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from onboard.llm import LLM
from onboard.mapper import mapping_accuracy
from onboard.mapping import reference_mapping
from onboard.pipeline import RunResult, run
from onboard.score import score, scoreboard

ROOT = Path(__file__).resolve().parents[2]
EXPORT = ROOT / "data" / "legacy" / "legacy_export.xlsx"
TRUTH = ROOT / "data" / "ground_truth"
AS_OF = date(2026, 9, 30)


def replay_run(usage_path: Path | None = None) -> RunResult:
    """The recorded full run: mapper + validator + review pass, from fixtures only."""
    return run(EXPORT, LLM(mode="replay", usage_path=usage_path), AS_OF)


def evaluate(rr: RunResult) -> dict:
    acc = mapping_accuracy(json.loads(json.dumps(rr.mapping)),
                           json.loads(json.dumps(reference_mapping())))
    before, after = scoreboard(rr.before, TRUTH), scoreboard(rr.after, TRUTH)
    s_before, s_after = score(rr.before, TRUTH), score(rr.after, TRUTH)
    accepted = [d for d in rr.decisions if d["accepted"]]
    return {
        "mapping": acc,
        "scoreboard_before": before,
        "scoreboard_after": after,
        "score_before": s_before.as_dict(),
        "score_after": s_after.as_dict(),
        "silent_outside_corruptions_after": after.attrs["silent_outside_corruptions"],
        "review_questions": len(rr.decisions),
        "review_accepted": len(accepted),
        "review_by_kind": {k: {"asked": sum(d["kind"] == k for d in rr.decisions),
                               "accepted": sum(d["kind"] == k for d in accepted)}
                           for k in ("date", "labor", "dup")},
        "usage": rr.usage,
    }
