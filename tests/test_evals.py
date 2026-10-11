"""Evals of the real model run, replayed from tests/fixtures/llm (no key needed).

Skipped if the fixtures have not been recorded yet:
    XAI_API_KEY=... onboard run data/legacy/legacy_export.xlsx --as-of 2026-09-30 --llm-mode record
"""

import pytest

from onboard.evals import evaluate, replay_run
from onboard.llm import MissingFixture
from onboard.validate import LABOR_HOURS_MAX


@pytest.fixture(scope="module")
def ev(tmp_path_factory):
    try:
        rr = replay_run(tmp_path_factory.mktemp("usage") / "usage.csv")
    except MissingFixture as e:
        pytest.skip(f"no recorded LLM run: {e}")
    return rr, evaluate(rr)


def test_mapping_matches_the_hand_written_one(ev):
    _, e = ev
    m = e["mapping"]
    assert m["sheets_correct"] == m["sheets_total"]
    assert m["date_format_correct"]
    assert m["column_accuracy"] >= 0.95, m["errors"]
    assert m["code_accuracy"] >= 0.9, m["errors"]
    assert m["codes_wrong"] == 0, m["errors"]  # untranslated is fine (review), wrong is not


def test_review_pass_never_makes_things_worse(ev):
    _, e = ev
    b, a = e["scoreboard_before"], e["scoreboard_after"]
    assert a.loc["TOTAL", "silent"] <= b.loc["TOTAL", "silent"]
    assert a.loc["TOTAL", "fixed"] + a.loc["TOTAL", "removed"] >= \
        b.loc["TOTAL", "fixed"] + b.loc["TOTAL", "removed"]


def test_zero_silent_corruption_rule(ev):
    """The LLM never changes a correct cell into a wrong one, the only extra findings on
    clean rows are codes the mapper left for a person, and whatever is still wrong in the
    load is of the two undetectable kinds."""
    rr, e = ev
    from onboard.score import score
    from onboard.evals import TRUTH
    assert e["silent_outside_corruptions_after"] == 0
    s = score(rr.after, TRUTH)
    left_open = {i.split(": ", 1)[1].split(" -> ")[0]
                 for i in (d["item"] for d in rr.mapping_report["decisions"]
                           if d["kind"] == "code" and d["decision"] == "review")}
    fp = s.false_positives
    assert set(fp.check) <= {"unknown_code"} and set(fp.original) <= left_open, fp
    assert e["score_after"]["false_positives"] == e["score_before"]["false_positives"]
    m = s.missed
    for r in m.itertuples():
        ok = (r.corruption_type == "date_mmdd" and r.detail.startswith("ambiguous")) or (
            r.corruption_type == "unit_mixup" and r.field == "LABOR_HRS"
            and float(r.corrupted_value) <= LABOR_HOURS_MAX)
        assert ok, r


def test_every_llm_change_is_explained(ev):
    rr, _ = ev
    ex = rr.after.exceptions()
    llm = ex[ex.check.isin(["external_resolution", "external_duplicate"])]
    assert len(llm) == sum(1 for d in rr.decisions if d["applied"])
    assert llm.message.str.contains("confidence").all()


def test_run_cost_is_modest(ev):
    _, e = ev
    assert e["usage"]["cost_usd"] < 2.0
