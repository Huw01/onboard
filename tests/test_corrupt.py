"""The legacy export contains exactly the corruptions the answer key says it does."""

import pandas as pd
import pytest

from onboard.corrupt import COLUMN_MAP, read_legacy_csv, read_legacy_xlsx, to_legacy

from conftest import DATA

EXPECTED_TYPES = {
    "exact_duplicate", "near_duplicate", "orphan_parent", "orphan_reference",
    "date_mmdd", "date_iso", "date_excel_serial", "free_text_frequency", "unit_mixup",
    "location_typo", "location_casing", "blank_required", "whitespace",
}


@pytest.fixture(scope="module")
def legacy(corrupted):
    _, base = corrupted
    return read_legacy_xlsx(base / "legacy" / "legacy_export.xlsx")


@pytest.fixture(scope="module")
def log(corrupted):
    _, base = corrupted
    return pd.read_csv(base / "ground_truth" / "corruptions.csv", dtype=str,
                       keep_default_na=False)


def test_legacy_columns_are_legacy_names(legacy):
    assert set(legacy) == set(COLUMN_MAP)
    for sheet, (_, cols) in COLUMN_MAP.items():
        assert list(legacy[sheet].columns) == [c[0] for c in cols]
    assert "EQUIP_NO" in legacy["EQUIPMENT"] and "WO_NUM" in legacy["WORK_ORDERS"]


def test_all_corruption_types_present(log):
    assert set(log.corruption_type) == EXPECTED_TYPES
    assert len(log) > 1000


def test_every_logged_corruption_is_in_the_xlsx(legacy, log):
    missing = []
    for r in log.itertuples():
        actual = legacy[r.sheet].at[int(r.row_index), r.field]
        if actual != r.corrupted_value:
            missing.append((r.sheet, r.excel_row, r.field, actual, r.corrupted_value))
    assert not missing, missing[:5]


def test_every_logged_corruption_is_in_the_csvs(corrupted, log):
    _, base = corrupted
    for sheet, rows in log.groupby("sheet"):
        df = read_legacy_csv(base / "legacy" / f"{sheet}.csv")
        actual = [df.at[int(i), f] for i, f in zip(rows.row_index, rows.field)]
        assert actual == rows.corrupted_value.tolist()


def test_cell_corruptions_actually_change_something(log):
    cells = log[log.corruption_type != "exact_duplicate"]
    assert (cells.original_value != cells.corrupted_value).all()


def test_no_unlogged_changes(clean, legacy, log, corrupted):
    """Every cell not in the log equals the baseline legacy rendering of its record."""
    c, _ = corrupted
    baseline = to_legacy(clean)
    logged = set(zip(log.sheet, log.row_index.astype(int), log.field))
    for sheet, df in legacy.items():
        state = c.sheets[sheet]
        base = baseline[sheet].map(str)
        base.index = base.iloc[:, 0]
        for i, rec in enumerate(state.record_ids):
            expected = base.loc[rec]
            for col in df.columns:
                if (sheet, i, col) in logged:
                    continue
                assert df.at[i, col] == expected[col], (sheet, i + 2, col)


def test_exact_duplicates_are_exact(legacy, corrupted):
    _, base = corrupted
    rm = pd.read_csv(base / "ground_truth" / "row_map.csv", dtype=str, keep_default_na=False)
    dups = rm[rm.row_kind == "exact_duplicate"]
    assert len(dups) > 0
    for r in dups.itertuples():
        df = legacy[r.sheet]
        assert df.iloc[int(r.row_index)].tolist() == \
            df.iloc[int(r.duplicate_of_excel_row) - 2].tolist()


def test_orphans_point_nowhere(legacy, log, clean):
    ids = set(clean["assets"].asset_id)
    for r in log[log.corruption_type.isin(["orphan_parent", "orphan_reference"])].itertuples():
        assert r.corrupted_value not in ids
        assert r.clean_value in ids


def test_corruption_is_deterministic(clean_dir, tmp_path, log):
    from onboard.corrupt import corrupt
    c = corrupt(clean_dir, tmp_path / "l", tmp_path / "g", seed=42)
    again = pd.read_csv(tmp_path / "g" / "corruptions.csv", dtype=str, keep_default_na=False)
    pd.testing.assert_frame_equal(again, log)


def test_committed_answer_key_is_current(log):
    committed = pd.read_csv(DATA / "ground_truth" / "corruptions.csv", dtype=str,
                            keep_default_na=False)
    pd.testing.assert_frame_equal(committed, log)
    committed_x = read_legacy_xlsx(DATA / "legacy" / "legacy_export.xlsx")
    for r in committed.itertuples():
        assert committed_x[r.sheet].at[int(r.row_index), r.field] == r.corrupted_value
