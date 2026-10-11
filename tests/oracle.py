"""Offline stand-ins for the LLM, used to test the plumbing without a key.

These are *not* model results. The oracle answers from the answer key, so tests
can check that correct answers flow through the guards and the validator, and
that wrong or unsure answers can't corrupt anything.
"""

from __future__ import annotations

import json
import re

import pandas as pd

from onboard.llm import Usage
from onboard.mapping import reference_mapping
from onboard.schema import CODED_FIELDS

from conftest import DATA


def oracle_proposal(legacy) -> dict:
    ref = reference_mapping()
    sheets = []
    for sheet, spec in ref["sheets"].items():
        table = spec["table"]
        cols = [{"source": s, "target": t, "confidence": 0.97, "reason": "oracle"}
                for s, t in spec["columns"].items()]
        codes = []
        for tgt, vm in spec["value_maps"].items():
            src = next(s for s, t in spec["columns"].items() if t == tgt)
            observed = {str(v).strip() for v in legacy[sheet][src]}
            for code, val in vm.items():
                if code in observed:
                    codes.append({"source_column": src, "code": code, "value": str(val),
                                  "relation": "same_meaning", "confidence": 0.95,
                                  "reason": "oracle"})
        sheets.append({"sheet": sheet, "table": table, "confidence": 0.99, "reason": "oracle",
                       "columns": cols, "codes": codes})
    return {"date_format": "%d/%m/%Y", "date_format_confidence": 0.9,
            "date_format_reason": "oracle", "sheets": sheets}


def truth_lookup():
    log = pd.read_csv(DATA / "ground_truth" / "corruptions.csv", dtype=str,
                      keep_default_na=False)
    return {(r.sheet, int(r.row_index), r.field): r for r in log.itertuples()}


class FakeTransport:
    """Answers mapping calls with the oracle proposal and review calls with
    ``answer(item) -> (decision, confidence)``."""

    def __init__(self, legacy, answer=None, usage=Usage(1000, 200, 300, 500)):
        self.legacy = legacy
        self.answer = answer
        self.usage = usage
        self.calls = 0

    def __call__(self, model, messages, response_format, **opts):
        self.calls += 1
        name = response_format["json_schema"]["name"]
        if name == "mapping_proposal":
            return json.dumps(oracle_proposal(self.legacy)), self.usage
        user = messages[1]["content"]
        items = json.loads(user.split("\n", 1)[1])
        out = []
        for it in items:
            decision, conf = self.answer(it) if self.answer else ("unsure", 0.0)
            out.append({"id": it["id"], "decision": decision, "confidence": conf,
                        "reason": "fake"})
        return json.dumps({"decisions": out}), self.usage


def oracle_answer(candidates_by_id, truth):
    """Answer each review question correctly using the answer key."""
    def answer(item):
        c = candidates_by_id[item["id"]]
        hit = truth.get((c.sheet, c.row_index, c.field))
        if c.kind == "date":
            swapped = hit is not None and hit.corruption_type == "date_mmdd"
            return ("swap" if swapped else "keep"), 0.95
        if c.kind == "labor":
            return ("minutes" if hit is not None and hit.corruption_type == "unit_mixup"
                    else "hours"), 0.95
        if c.kind == "dup":
            return "same", 0.95
        return "unsure", 0.0
    return answer
