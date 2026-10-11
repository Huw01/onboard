"""Review pass plumbing, offline: candidates, acceptance rules, re-validation.

The oracle answers from the answer key; it shows the pipeline turns correct
answers into correct data. Real-model results are in test_evals.py."""

from datetime import date

import pytest

from onboard.corrupt import read_legacy_xlsx
from onboard.llm import LLM
from onboard.mapping import reference_mapping
from onboard.pipeline import run
from onboard.review import CandidateFinder, MIN_CONFIDENCE, to_resolutions
from onboard.score import score, scoreboard
from onboard.validate import is_ambiguous_date, validate

from conftest import DATA
from oracle import FakeTransport, oracle_answer, truth_lookup

AS_OF = date(2026, 9, 30)


@pytest.fixture(scope="module")
def legacy():
    return read_legacy_xlsx(DATA / "legacy" / "legacy_export.xlsx")


@pytest.fixture(scope="module")
def first(legacy):
    r = validate(legacy, reference_mapping(), AS_OF)
    return r, CandidateFinder(legacy, reference_mapping(), r).find()


def test_candidates_are_closed_questions(legacy, first):
    _, cands = first
    assert len({c.id for c in cands}) == len(cands)
    kinds = {c.kind for c in cands}
    assert kinds == {"date", "labor", "dup"}
    for c in cands:
        assert len(c.options) == 2
        if c.kind == "date":
            assert is_ambiguous_date(str(legacy[c.sheet].at[c.row_index, c.field]).strip())


def test_unsure_or_low_confidence_changes_nothing(legacy, first, tmp_path):
    r, cands = first
    decisions = [{"id": c.id, "decision": c_opt, "confidence": MIN_CONFIDENCE - 0.01,
                  "reason": "meh"} for c in cands for c_opt in [list(c.options)[1]]]
    overrides, drops, _ = to_resolutions(cands, decisions)
    assert overrides == [] and drops == []
    llm = LLM(mode="live", fixtures_dir=tmp_path, usage_path=None,
              transport=FakeTransport(legacy))  # answers "unsure" to everything
    rr = run(legacy, llm, AS_OF, mapping_path=DATA.parent / "mappings" / "legacy_cmms.json")
    assert [vars(f) for f in rr.after.findings] == [vars(f) for f in r.findings]


def test_correct_answers_only_improve_the_result(legacy, first, tmp_path):
    r, cands = first
    answer = oracle_answer({c.id: c for c in cands}, truth_lookup())
    llm = LLM(mode="live", fixtures_dir=tmp_path, usage_path=None,
              transport=FakeTransport(legacy, answer))
    rr = run(legacy, llm, AS_OF)
    before = scoreboard(rr.before, DATA / "ground_truth")
    after = scoreboard(rr.after, DATA / "ground_truth")
    assert after.loc["TOTAL", "silent"] < before.loc["TOTAL", "silent"]
    assert after.loc["TOTAL", "fixed"] + after.loc["TOTAL", "removed"] > \
        before.loc["TOTAL", "fixed"] + before.loc["TOTAL", "removed"]
    assert after.attrs["silent_outside_corruptions"] == 0
    assert score(rr.after, DATA / "ground_truth").false_positives.empty
    assert after.loc["near_duplicate", "removed"] == 36
    ex = rr.after.exceptions()
    assert (ex.check == "external_resolution").sum() > 0
    assert ex[ex.check == "external_resolution"].message.str.contains("confidence").all()


def test_llm_answers_are_revalidated(legacy, first, tmp_path):
    """Even a confident wrong answer can't skip the rules: swapping a date that makes
    a work order complete before it was reported is caught and held again."""
    r, cands = first
    llm = LLM(mode="live", fixtures_dir=tmp_path, usage_path=None,
              transport=FakeTransport(legacy, lambda item: (list(item["options"])[1], 0.99)))
    rr = run(legacy, llm, AS_OF, mapping_path=DATA.parent / "mappings" / "legacy_cmms.json")
    t = rr.after.tables
    wo = t["work_orders"].dropna(subset=["completed_date"])
    assert (wo.completed_date >= wo.reported_date).all()
    assert (t["work_orders"].scheduled_date >= t["work_orders"].reported_date).all()
