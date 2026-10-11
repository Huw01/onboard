"""Score a validation run against the answer key.

Used by the tests now and by the eval suite in milestone 5.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from onboard.generate import write_tables
from onboard.validate import Result


@dataclass
class Score:
    corruptions: int
    detected: int
    missed: pd.DataFrame          # logged corruptions with no finding on that cell/row
    false_positives: pd.DataFrame  # fixed/review/dropped findings on untouched rows
    silent_errors: pd.DataFrame    # clean-output cells that differ from ground truth
    rows_recovered: int            # clean-output rows identical to ground truth
    rows_total: int                # ground-truth rows

    def as_dict(self) -> dict:
        return {
            "corruptions": self.corruptions,
            "detected": self.detected,
            "detection_rate": round(self.detected / self.corruptions, 4),
            "missed_by_type": self.missed.corruption_type.value_counts().to_dict(),
            "false_positives": len(self.false_positives),
            "silent_errors": len(self.silent_errors),
            "rows_recovered": self.rows_recovered,
            "rows_total": self.rows_total,
            "row_recovery_rate": round(self.rows_recovered / self.rows_total, 4),
        }


def _read(d: Path, name: str) -> pd.DataFrame:
    return pd.read_csv(d / f"{name}.csv", dtype=str, keep_default_na=False)


def score(result: Result, truth_dir: Path) -> Score:
    truth_dir = Path(truth_dir)
    log = _read(truth_dir, "corruptions")
    log["row_index"] = log.row_index.astype(int)
    ex = result.exceptions()
    ex = ex[ex.row_index.notna()]
    cells = set(zip(ex.sheet, ex.row_index.astype(int), ex.field))

    # A corruption counts as caught if its cell has a finding, or if its whole row
    # was kept out of the clean output (dropped duplicate, or held for review).
    sheet_table = dict(zip(ex.sheet, ex.table))
    not_loaded = {(s, i) for s, t in sheet_table.items()
                  for i, st in enumerate(result.status.get(t, [])) if st != "ok"}
    hit = [(s, i, f) in cells or (s, i) in not_loaded
           for s, i, f in zip(log.sheet, log.row_index, log.field)]
    missed = log[[not h for h in hit]]

    corrupted_rows = set(zip(log.sheet, log.row_index))
    acting = ex[ex.action.isin(["fixed", "review", "dropped"])]
    fp = acting[[(s, int(i)) not in corrupted_rows
                 for s, i in zip(acting.sheet, acting.row_index)]]

    # Compare the clean output with the ground truth, cell by cell, via CSV text.
    silent, recovered, total = [], 0, 0
    with tempfile.TemporaryDirectory() as tmp:
        write_tables(result.tables, Path(tmp))
        for name in result.tables:
            out = _read(Path(tmp), name)
            gt = _read(truth_dir, name)
            pk = gt.columns[0]
            total += len(gt)
            out, gt = out.set_index(pk), gt.set_index(pk)
            unknown = set(out.index) - set(gt.index)
            for rid in unknown:
                silent.append((name, rid, pk, rid, ""))
            same = out.loc[[r for r in out.index if r in gt.index]]
            ref = gt.loc[same.index, same.columns]
            diff = same.ne(ref)
            recovered += int((~diff.any(axis=1)).sum())
            for r, c in zip(*diff.values.nonzero()):
                silent.append((name, same.index[r], same.columns[c], same.iat[r, c],
                               ref.iat[r, c]))
    silent_df = pd.DataFrame(silent, columns=["table", "record_id", "field", "value", "truth"])
    return Score(len(log), len(log) - len(missed), missed, fp, silent_df, recovered, total)


OUTCOMES = ["fixed", "removed", "held", "silent"]


def scoreboard(result: Result, truth_dir: Path) -> pd.DataFrame:
    """Per corruption type: what happened to each injected corruption.

    fixed   - row loaded and the cell equals ground truth
    removed - duplicate row kept out of the load (the right outcome for duplicates)
    held    - row held for review (not loaded)
    silent  - row loaded with a value that differs from ground truth
    """
    truth_dir = Path(truth_dir)
    log = _read(truth_dir, "corruptions")
    log["row_index"] = log.row_index.astype(int)
    s = score(result, truth_dir)
    wrong = set(zip(s.silent_errors.record_id, s.silent_errors.field))
    ex = result.exceptions()
    sheet_table = dict(zip(ex.sheet, ex.table))
    rows = []
    for r in log.itertuples():
        table = sheet_table.get(r.sheet, r.target_table)
        st = result.status[table][r.row_index]
        if st == "dropped":
            outcome = "removed"
        elif st in ("review", "held"):
            outcome = "held"
        else:
            outcome = "silent" if (r.record_id, r.target_field) in wrong else "fixed"
        rows.append((r.corruption_type, outcome))
    df = pd.DataFrame(rows, columns=["corruption_type", "outcome"])
    board = pd.crosstab(df.corruption_type, df.outcome).reindex(columns=OUTCOMES, fill_value=0)
    board.insert(0, "injected", board.sum(axis=1))
    board.loc["TOTAL"] = board.sum()
    extra = len(s.silent_errors) - int(board.loc["TOTAL", "silent"])
    board.attrs["silent_outside_corruptions"] = extra
    return board
