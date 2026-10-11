"""Mapper plumbing and guards, offline (oracle proposal, not a model result)."""

import copy
import json
from datetime import date

import pytest

from onboard.corrupt import read_legacy_xlsx
from onboard.mapper import (apply_guards, build_prompt, mapping_accuracy, profile_export,
                            propose_mapping, response_schema)
from onboard.mapping import reference_mapping
from onboard.llm import LLM
from onboard.validate import validate

from conftest import DATA
from oracle import FakeTransport, oracle_proposal


@pytest.fixture(scope="module")
def legacy():
    return read_legacy_xlsx(DATA / "legacy" / "legacy_export.xlsx")


def _norm(m):
    return json.loads(json.dumps(m))


def test_profile_is_deterministic_and_informative(legacy):
    a, b = profile_export(legacy), profile_export(legacy)
    assert a == b
    eq = next(p for p in a if p["sheet"] == "EQUIPMENT")
    crit = next(c for c in eq["columns"] if c["name"] == "CRIT")
    assert set(crit["values"]) == {"A", "B", "C"}
    assert len(build_prompt(a)) < 40_000


def test_response_schema_is_strict():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)
    walk(response_schema())


def test_correct_proposal_reproduces_the_reference_mapping(legacy):
    mapping, _ = apply_guards(oracle_proposal(legacy), legacy)
    assert _norm(mapping) == _norm(reference_mapping())
    acc = mapping_accuracy(_norm(mapping), _norm(reference_mapping()))
    assert acc["column_accuracy"] == 1.0 and acc["code_accuracy"] == 1.0
    assert acc["codes_wrong"] == 0 and acc["date_format_correct"]


def test_guards_reject_bad_proposals(legacy):
    p = oracle_proposal(legacy)
    eq = next(s for s in p["sheets"] if s["sheet"] == "EQUIPMENT")
    pm = next(s for s in p["sheets"] if s["sheet"] == "PM_SCHED")
    eq["columns"].append({"source": "MFR", "target": "wo_id", "confidence": 0.99,
                          "reason": "wrong table"})
    eq["columns"].append({"source": "NOPE", "target": "model", "confidence": 0.99,
                          "reason": "no such column"})
    next(c for c in eq["columns"] if c["source"] == "SERIAL_NO")["confidence"] = 0.3
    pm["codes"] += [
        {"source_column": "FREQ_UOM", "code": "W", "value": "DAYS", "relation": "needs_conversion",
         "confidence": 0.9, "reason": "weeks need x7"},
        {"source_column": "FREQ_UOM", "code": "W", "value": "DAYS", "relation": "same_meaning",
         "confidence": 0.5, "reason": "weeks?"},
        {"source_column": "FREQ_UOM", "code": "FORTNIGHT", "value": "DAYS",
         "relation": "same_meaning", "confidence": 0.99, "reason": "not in data"},
        {"source_column": "PM_TYPE", "code": "T", "value": "SOMETIMES",
         "relation": "same_meaning", "confidence": 0.99, "reason": "not allowed"},
    ]
    mapping, log = apply_guards(p, legacy)
    assert "MFR" in mapping["sheets"]["EQUIPMENT"]["columns"]  # original kept
    assert mapping["sheets"]["EQUIPMENT"]["columns"]["MFR"] == "manufacturer"
    assert "SERIAL_NO" not in mapping["sheets"]["EQUIPMENT"]["columns"]
    uom = mapping["sheets"]["PM_SCHED"]["value_maps"]["frequency_unit"]
    assert "W" not in uom and "FORTNIGHT" not in uom
    assert mapping["sheets"]["PM_SCHED"]["value_maps"]["trigger_type"]["T"] == "TIME"
    decided = {(e["item"], e["decision"], e["detail"]) for e in log}
    assert ("FREQ_UOM: W -> DAYS", "review", "needs_conversion, left for review") in decided
    assert ("FREQ_UOM: W -> DAYS", "review", "low confidence, left for review") in decided
    decided = {(e["item"], e["decision"]) for e in log}
    assert ("FREQ_UOM: FORTNIGHT -> DAYS", "rejected") in decided


def test_untranslated_codes_go_to_review_not_into_the_load(legacy):
    p = oracle_proposal(legacy)
    wo = next(s for s in p["sheets"] if s["sheet"] == "WORK_ORDERS")
    wo["codes"] = [k for k in wo["codes"] if k["code"] != "CAN"]  # forget one status code
    mapping, _ = apply_guards(p, legacy)
    r = validate(legacy, mapping, date(2026, 9, 30))
    ex = r.exceptions()
    unknown = ex[(ex.check == "unknown_code") & (ex.target_field == "status")]
    assert len(unknown) > 0
    assert set(r.tables["work_orders"].status) <= {"OPEN", "IN_PROGRESS", "COMPLETED"}


def test_propose_mapping_through_the_wrapper(legacy, tmp_path):
    llm = LLM(mode="live", fixtures_dir=tmp_path, usage_path=None,
              transport=FakeTransport(legacy))
    mapping, report = propose_mapping(llm, legacy)
    assert _norm(mapping) == _norm(reference_mapping())
    assert report["decisions"] and llm.totals()["calls"] == 1


def test_accuracy_counts_errors():
    ref = _norm(reference_mapping())
    bad = copy.deepcopy(ref)
    bad["sheets"]["EQUIPMENT"]["columns"]["MFR"] = "model"
    bad["sheets"]["EQUIPMENT"]["value_maps"]["criticality"]["A"] = 3
    bad["sheets"]["PM_SCHED"]["value_maps"]["frequency_unit"]["MI"] = "KM"
    acc = mapping_accuracy(bad, ref)
    assert acc["columns_correct"] == acc["columns_total"] - 1
    assert acc["codes_correct"] == acc["codes_total"] - 1
    assert acc["codes_wrong"] == 2  # A -> 3 and the invented MI -> KM
    del bad["sheets"]["EQUIPMENT"]["value_maps"]["criticality"]["A"]
    assert mapping_accuracy(bad, ref)["codes_missing"] == 1
