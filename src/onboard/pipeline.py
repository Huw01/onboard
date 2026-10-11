"""`onboard run`: mapper -> validator -> LLM review pass -> validator again -> outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd

from onboard.corrupt import read_legacy_xlsx
from onboard.llm import LLM
from onboard.mapper import propose_mapping
from onboard.mapping import load_mapping, write_mapping
from onboard.review import CandidateFinder, ask, to_resolutions
from onboard.validate import Result, validate, write_result


@dataclass
class RunResult:
    mapping: dict
    mapping_report: dict
    before: Result                      # rules only
    after: Result                       # rules + accepted LLM resolutions, re-validated
    decisions: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)


def run(export: Path | dict, llm: LLM, as_of: date | None = None,
        mapping_path: Path | None = None, review: bool = True) -> RunResult:
    legacy = read_legacy_xlsx(export) if not isinstance(export, dict) else export
    if mapping_path:
        mapping, report = load_mapping(mapping_path), {"source": str(mapping_path)}
    else:
        mapping, report = propose_mapping(llm, legacy)
    before = validate(legacy, mapping, as_of)
    decisions: list[dict] = []
    after = before
    if review:
        candidates = CandidateFinder(legacy, mapping, before).find()
        if candidates:
            decisions = ask(llm, candidates)
            overrides, drops, decisions = to_resolutions(candidates, decisions)
            after = validate(legacy, mapping, as_of, overrides, drops)
    return RunResult(mapping, report, before, after, decisions, llm.totals())


def write_run(rr: RunResult, out: Path) -> None:
    out = Path(out)
    write_mapping(rr.mapping, out / "mapping.json")
    (out / "mapping_report.json").write_text(
        json.dumps(rr.mapping_report, indent=2, ensure_ascii=False, default=str) + "\n")
    pd.DataFrame(rr.decisions).to_csv(out / "review_decisions.csv", index=False,
                                      lineterminator="\n")
    questions = [d for d in rr.mapping_report.get("decisions", []) if d["decision"] == "review"]
    pd.DataFrame(questions, columns=["kind", "sheet", "item", "decision", "detail", "confidence",
                                     "reason"]).to_csv(out / "mapping_questions.csv",
                                                       index=False, lineterminator="\n")
    write_result(rr.after, out)
    (out / "run.json").write_text(json.dumps({"usage": rr.usage}, indent=2) + "\n")
