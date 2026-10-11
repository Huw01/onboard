"""Validator: parsers, zero findings on clean data, known findings on dirty data."""

import json
from datetime import date

import pandas as pd
import pytest

from onboard.corrupt import _free_text_options, read_legacy_xlsx, to_legacy
from onboard.generate import write_tables
from onboard.mapping import load_mapping, reference_mapping
from onboard.schema import LOAD_ORDER
from onboard.score import score
from onboard.validate import (LABOR_HOURS_MAX, id_key, is_ambiguous_date, location_key,
                              parse_date, parse_frequency, validate)

from conftest import DATA, ROOT

AS_OF = date(2026, 9, 30)


# ---- parsers ---------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("26/06/2009", ("2009-06-26", None)),
    ("2009-06-26", ("2009-06-26", "iso_date")),
    ("39990", ("2009-06-26", "excel_serial")),
    ("39990.0", ("2009-06-26", "excel_serial")),
    ("12/29/2019", ("2019-12-29", "swapped_date")),
    ("", (None, None)),
    ("31/02/2020", (None, "unparseable")),
    ("next tuesday", (None, "unparseable")),
    ("99999", (None, "unparseable")),
])
def test_parse_date(raw, expected):
    assert parse_date(raw, "%d/%m/%Y") == expected


def test_ambiguous_dates():
    assert is_ambiguous_date("08/09/2017")
    assert not is_ambiguous_date("13/09/2017")
    assert not is_ambiguous_date("05/05/2017")


@pytest.mark.parametrize("raw,expected", [
    ("30", (30, None)), ("monthly", (30, "DAYS")), ("1M", (30, "DAYS")),
    ("30d", (30, "DAYS")), ("every 4 wks", (28, "DAYS")), ("quarterly", (90, "DAYS")),
    ("6M", (180, "DAYS")), ("12M", (365, "DAYS")), ("1Y", (365, "DAYS")),
    ("500 hrs", (500, "HOURS")), ("8,000 h", (8000, "HOURS")), ("500H", (500, "HOURS")),
    ("every 500 hours", (500, "HOURS")), ("250 run hrs", (250, "HOURS")),
    ("5,000 km", (5000, "KM")), ("5k km", (5000, "KM")), ("every 5,000 km", (5000, "KM")),
    ("300 cyc", (300, "CYCLES")), ("every 500 MWh", (500, "MWH")),
    ("when it breaks", None),
])
def test_parse_frequency(raw, expected):
    assert parse_frequency(raw) == expected


def test_every_free_text_spelling_the_corruptor_uses_parses_back(clean):
    pms = clean["pm_schedules"][["frequency_value", "frequency_unit"]].drop_duplicates()
    for value, unit in pms.itertuples(index=False):
        for text in _free_text_options(int(value), unit):
            assert parse_frequency(text) == (value, unit), text


def test_keys():
    assert id_key("cv_1001-mtr1") == id_key("CV1001 MTR1") == "CV1001MTR1"
    assert location_key("GR-CONV-04") == location_key("gr conv 4") == location_key("GRCONV-04")
    assert location_key("GR-CONV-04") != location_key("GR-CNOV-04")


def test_reference_mapping_file_is_current():
    on_disk = load_mapping(ROOT / "mappings" / "legacy_cmms.json")
    assert on_disk == json.loads(json.dumps(reference_mapping()))


# ---- clean data: nothing to find, nothing changed ----------------------------

def test_clean_legacy_round_trip_has_zero_findings(clean, tmp_path):
    legacy = to_legacy(clean)
    result = validate(legacy, reference_mapping(), AS_OF)
    assert result.findings == []
    write_tables(result.tables, tmp_path)
    for name in LOAD_ORDER:
        assert (tmp_path / f"{name}.csv").read_text() == \
            (DATA / "clean" / f"{name}.csv").read_text(), name


# ---- dirty data: known findings ---------------------------------------------

@pytest.fixture(scope="module")
def dirty():
    result = validate(read_legacy_xlsx(DATA / "legacy" / "legacy_export.xlsx"),
                      reference_mapping(), AS_OF)
    return result, score(result, DATA / "ground_truth")


def test_no_false_positives(dirty):
    _, s = dirty
    assert s.false_positives.empty, s.false_positives.head()


def test_only_undetectable_corruptions_are_missed(dirty):
    """Missed = day/month swaps that are valid either way round, and labor in minutes
    small enough to look like plausible hours. Nothing else may slip through."""
    _, s = dirty
    m = s.missed
    ambiguous = (m.corruption_type == "date_mmdd") & (m.detail == "ambiguous (day<=12)")
    tiny_minutes = pd.Series([t == "unit_mixup" and f == "LABOR_HRS"
                              and float(v) <= LABOR_HOURS_MAX
                              for t, f, v in zip(m.corruption_type, m.field,
                                                 m.corrupted_value)], index=m.index, dtype=bool)
    assert (ambiguous | tiny_minutes).all(), m[~(ambiguous | tiny_minutes)]
    assert s.detected / s.corruptions > 0.9


def test_silent_errors_are_exactly_the_missed_cells(dirty):
    _, s = dirty
    target = {"INSTALL_DT": "install_date", "LAST_DONE_DT": "last_done_date",
              "OPEN_DT": "reported_date", "SCHED_DT": "scheduled_date",
              "COMP_DT": "completed_date", "LABOR_HRS": "labor_hours"}
    missed = {(r.record_id, target[r.field]) for r in s.missed.itertuples()}
    silent = set(zip(s.silent_errors.record_id, s.silent_errors.field))
    assert silent == missed


def test_ambiguity_is_surfaced_as_column_warnings(dirty):
    result, s = dirty
    ex = result.exceptions()
    warned = set(ex[ex.check == "ambiguous_date_format"].target_field)
    assert {f for f in s.silent_errors.field if f.endswith("_date")} <= warned


def test_duplicates_and_orphans(dirty):
    result, _ = dirty
    ex = result.exceptions()
    assert (ex.check == "exact_duplicate").sum() == 27
    assert set(ex[ex.check == "exact_duplicate"].action) == {"dropped"}
    assert (ex.check == "near_duplicate").sum() == 18
    assert (ex.check == "orphan_parent").sum() == 20
    assert (ex.check == "orphan_reference").sum() == 12


def test_clean_output_is_referentially_intact(dirty):
    result, _ = dirty
    t = result.tables
    for name, df in t.items():
        assert df.iloc[:, 0].is_unique
    assets = set(t["assets"].asset_id)
    assert set(t["assets"].parent_asset_id.dropna()) <= assets
    assert set(t["assets"].location_id) <= set(t["locations"].location_id)
    assert set(t["pm_schedules"].asset_id) <= assets
    assert set(t["work_orders"].asset_id) <= assets
    assert set(t["work_orders"].pm_id.dropna()) <= set(t["pm_schedules"].pm_id)


def test_every_review_and_fix_has_a_reason(dirty):
    result, _ = dirty
    ex = result.exceptions()
    assert set(ex.action) <= {"fixed", "dropped", "review", "held", "warning"}
    assert (ex[ex.action == "review"].message != "").all()


def test_summary_counts_add_up(dirty):
    result, _ = dirty
    for table, v in result.summary()["tables"].items():
        assert v["rows_in"] == (v["clean_out"] + v["dropped_duplicates"]
                                + v["held_for_review"] + v["held_dependency"])
