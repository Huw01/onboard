"""Deterministic validator: legacy export + mapping -> clean tables + exceptions.

No LLM in here. Every change is one of:

* ``fixed``   - an unambiguous, rule-based correction (stray whitespace, ISO or
  Excel-serial date, free-text frequency, ID casing...). The row is loaded.
* ``dropped`` - an exact duplicate row. Not loaded.
* ``review``  - something a person (or, later, the LLM with a person) must
  decide: blanks, orphans, near-duplicates, typos, unit conflicts. The row is
  held out of the clean files.
* ``held``    - the row is fine but depends on a held row (child of a held
  asset, PM on a held asset, WO on a held asset/PM). Held to keep the clean
  files referentially intact.
* ``warning`` - a column-level caveat (e.g. dates that are valid either way
  round in a column known to mix DD/MM and MM/DD).

The trust rule: a value in the clean output is either untouched, or fixed by
a rule that cannot be wrong for it. Anything else is surfaced, never guessed.
"""

from __future__ import annotations

import difflib
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from onboard.schema import FLOAT_COLUMNS, INT_COLUMNS, LOAD_ORDER, REQUIRED, TABLES

EXCEL_EPOCH = date(1899, 12, 30)
SERIAL_RANGE = (20000, 60000)          # 1954-10 .. 2064-04
CANONICAL_ASSET_ID = re.compile(r"^[A-Z]{2}-\d{4}(-[A-Z]+\d+)?$")
PM_LABOR_RATIO_MAX = 4.0               # actual / estimated hours on a PM work order
LABOR_HOURS_MAX = 40.0                 # single CM/EM work order

# --------------------------------------------------------------------------
# Value parsers (pure functions, unit-tested on their own)
# --------------------------------------------------------------------------

_ALT_FORMATS = {"%d/%m/%Y": "%m/%d/%Y", "%m/%d/%Y": "%d/%m/%Y"}


def parse_date(raw: str, fmt: str = "%d/%m/%Y") -> tuple[str | None, str | None]:
    """Return (iso_date, how). ``how`` is None when the value used the declared
    format, else 'iso_date' / 'excel_serial' / 'swapped_date', or 'unparseable'."""
    if raw == "":
        return None, None
    try:
        return datetime.strptime(raw, fmt).date().isoformat(), None
    except ValueError:
        pass
    if re.fullmatch(r"\d{5}(\.0+)?", raw):
        n = int(float(raw))
        if SERIAL_RANGE[0] <= n <= SERIAL_RANGE[1]:
            return (EXCEL_EPOCH + timedelta(days=n)).isoformat(), "excel_serial"
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        try:
            return date.fromisoformat(raw).isoformat(), "iso_date"
        except ValueError:
            pass
    alt = _ALT_FORMATS.get(fmt)
    if alt:
        try:
            return datetime.strptime(raw, alt).date().isoformat(), "swapped_date"
        except ValueError:
            pass
    return None, "unparseable"


def is_ambiguous_date(raw: str) -> bool:
    """DD/MM vs MM/DD can't be told apart: both parts <= 12 and different."""
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/\d{4}", raw)
    return bool(m) and int(m[1]) <= 12 and int(m[2]) <= 12 and m[1] != m[2]


_FREQ_WORDS = {"weekly": (7, "DAYS"), "fortnightly": (14, "DAYS"), "monthly": (30, "DAYS"),
               "quarterly": (90, "DAYS"), "semi-annual": (180, "DAYS"),
               "semiannual": (180, "DAYS"), "annual": (365, "DAYS"),
               "annually": (365, "DAYS"), "yearly": (365, "DAYS")}
_MONTHS = {1: 30, 3: 90, 6: 180, 12: 365}
_FREQ_RE = re.compile(
    r"^(?:every\s+)?(?P<n>\d+)?\s*(?P<k>k(?=\s+km$))?\s*"
    r"(?P<u>d|days?|w|wks?|weeks?|m|months?|y|yrs?|years?|h|hrs?|hours?|run\s+hrs|"
    r"km|cyc|cycles?|mwh)$")


def parse_frequency(raw: str) -> tuple[int, str | None] | None:
    """Parse a frequency. Plain numbers return (n, None): the unit comes from the
    unit column. Free text returns (value, unit). Unparseable returns None."""
    s = raw.strip().lower().replace(",", "")
    if re.fullmatch(r"\d+", s):
        return int(s), None
    if s in _FREQ_WORDS:
        return _FREQ_WORDS[s]
    m = _FREQ_RE.match(s)
    if not m:
        return None
    n = int(m["n"]) if m["n"] else 1
    if m["k"]:
        n *= 1000
    u = m["u"]
    if u in ("d", "day", "days"):
        return n, "DAYS"
    if u in ("w", "wk", "wks", "week", "weeks"):
        return n * 7, "DAYS"
    if u in ("m", "month", "months"):
        return _MONTHS.get(n, n * 30), "DAYS"
    if u in ("y", "yr", "yrs", "year", "years"):
        return n * 365, "DAYS"
    if u in ("h", "hr", "hrs", "hour", "hours") or u.startswith("run"):
        return n, "HOURS"
    if u == "km":
        return n, "KM"
    if u in ("cyc", "cycle", "cycles"):
        return n, "CYCLES"
    if u == "mwh":
        return n, "MWH"
    return None


def id_key(v: str) -> str:
    """Formatting-insensitive key: 'cv_1001-mtr1' and 'CV1001-MTR1' -> 'CV1001MTR1'."""
    return re.sub(r"[^A-Z0-9]", "", v.upper())


def location_key(v: str) -> str:
    """Like id_key but number segments lose leading zeros: 'GR-CONV-4' == 'GR CONV 04'."""
    parts = re.findall(r"[A-Z]+|\d+", v.upper())
    return "".join(str(int(p)) if p.isdigit() else p for p in parts)


# --------------------------------------------------------------------------
# Findings and result
# --------------------------------------------------------------------------

@dataclass
class Finding:
    table: str
    sheet: str
    row_index: int | None
    excel_row: int | None
    record_id: str
    field: str            # legacy column name ('' for row-level)
    target_field: str
    check: str
    action: str           # fixed | dropped | review | held | warning
    original: str
    value: str            # value after the fix, or the suggested value for review
    message: str


@dataclass
class Result:
    tables: dict[str, pd.DataFrame]
    findings: list[Finding]
    rows_in: dict[str, int]
    status: dict[str, list[str]] = field(default_factory=dict)   # per row: ok/dropped/...

    def exceptions(self) -> pd.DataFrame:
        cols = list(Finding.__dataclass_fields__)
        df = pd.DataFrame([asdict(f) for f in self.findings], columns=cols)
        df["row_index"] = df["row_index"].astype("Int64")
        df["excel_row"] = df["excel_row"].astype("Int64")
        return df

    def summary(self) -> dict:
        ex = self.exceptions()
        per_table = {}
        for table, n in self.rows_in.items():
            st = Counter(self.status[table])
            per_table[table] = {"rows_in": n, "clean_out": len(self.tables[table]),
                                "dropped_duplicates": st["dropped"],
                                "held_for_review": st["review"],
                                "held_dependency": st["held"]}
        fixed = ex[ex.action == "fixed"].check.value_counts().to_dict()
        review = ex[ex.action == "review"].check.value_counts().to_dict()
        return {"tables": per_table, "cells_fixed": fixed, "review_items": review,
                "warnings": int((ex.action == "warning").sum())}

    def summary_text(self) -> str:
        s = self.summary()
        lines = [f"{'table':<14}{'in':>7}{'clean':>8}{'dropped':>9}{'review':>8}{'held':>7}"]
        for t, v in s["tables"].items():
            lines.append(f"{t:<14}{v['rows_in']:>7}{v['clean_out']:>8}"
                         f"{v['dropped_duplicates']:>9}{v['held_for_review']:>8}"
                         f"{v['held_dependency']:>7}")
        lines.append("cells fixed automatically:")
        lines += [f"  {k:<26}{n:>6}" for k, n in sorted(s["cells_fixed"].items())]
        lines.append("needs a decision:")
        lines += [f"  {k:<26}{n:>6}" for k, n in sorted(s["review_items"].items())]
        lines.append(f"column warnings: {s['warnings']}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Validator
# --------------------------------------------------------------------------

class Validator:
    def __init__(self, mapping: dict, as_of: date | None = None):
        self.mapping = mapping
        self.fmt = mapping.get("source_date_format", "%d/%m/%Y")
        self.as_of = (as_of or date.today()).isoformat()
        self.findings: list[Finding] = []
        self.t: dict[str, pd.DataFrame] = {}
        self.sheet_of: dict[str, str] = {}
        self.legacy_col: dict[str, dict[str, str]] = {}
        self.status: dict[str, list[str]] = {}

    # ---- bookkeeping -----------------------------------------------------
    def _note(self, table: str, i: int | None, col: str, check: str, action: str,
              original="", value="", message="") -> None:
        df = self.t[table]
        rec = "" if i is None else str(df.at[i, TABLES[table][0]])
        self.findings.append(Finding(
            table, self.sheet_of[table], i, None if i is None else i + 2, rec,
            self.legacy_col[table].get(col, ""), col, check, action,
            "" if original is None else str(original), "" if value is None else str(value),
            message))
        if i is not None and action in ("review", "dropped", "held"):
            rank = {"ok": 0, "held": 1, "review": 2, "dropped": 3}
            if rank[action] > rank[self.status[table][i]]:
                self.status[table][i] = action

    def _rows(self, table: str, *, live: bool = True):
        st = self.status[table]
        return [i for i in range(len(self.t[table])) if not live or st[i] != "dropped"]

    # ---- pipeline --------------------------------------------------------
    def run(self, legacy: dict[str, pd.DataFrame]) -> Result:
        rows_in = {}
        for sheet, spec in self.mapping["sheets"].items():
            table = spec["table"]
            src = legacy[sheet]
            cols = {lc: tc for lc, tc in spec["columns"].items() if lc in src.columns}
            df = pd.DataFrame({tc: src[lc].map(lambda x: "" if x is None else str(x))
                               for lc, tc in cols.items()})
            for tc in TABLES[table]:
                if tc not in df:
                    df[tc] = ""
            self.t[table] = df[TABLES[table]].astype(object).reset_index(drop=True)
            self.sheet_of[table] = sheet
            self.legacy_col[table] = {tc: lc for lc, tc in cols.items()}
            self.status[table] = ["ok"] * len(df)
            rows_in[table] = len(df)

        tables = [t for t in LOAD_ORDER if t in self.t]
        for table in tables:
            self.strip_whitespace(table)
            self.drop_exact_duplicates(table)
            self.decode_values(table, self.mapping["sheets"][self.sheet_of[table]]["value_maps"])
            self.parse_dates(table)
        if "pm_schedules" in self.t:
            self.parse_frequencies()
        if "assets" in self.t:
            self.normalize_asset_ids()
        if "work_orders" in self.t:
            self.infer_work_order_fields()
        for table in tables:
            self.required_fields(table)
            self.duplicate_keys(table)
        self.references()
        self.consistency()
        self.cascade_holds()
        return Result(self.output_tables(), self.findings, rows_in, self.status)

    # ---- per-table checks ------------------------------------------------
    def strip_whitespace(self, table: str) -> None:
        df = self.t[table]
        for col in df.columns:
            for i in range(len(df)):
                v = df.at[i, col]
                s = v.strip()
                if s != v:
                    df.at[i, col] = s
                    self._note(table, i, col, "whitespace", "fixed", v, s,
                               "leading/trailing whitespace removed")

    def drop_exact_duplicates(self, table: str) -> None:
        df = self.t[table]
        seen: dict[tuple, int] = {}
        for i in range(len(df)):
            key = tuple(df.iloc[i])
            if key in seen:
                self._note(table, i, TABLES[table][0], "exact_duplicate", "dropped",
                           df.iat[i, 0], "", f"identical to excel_row {seen[key] + 2}")
            else:
                seen[key] = i

    def decode_values(self, table: str, value_maps: dict) -> None:
        df = self.t[table]
        for col, vmap in value_maps.items():
            upper = {str(k).upper(): v for k, v in vmap.items()}
            for i in self._rows(table):
                raw = df.at[i, col]
                if raw == "":
                    continue
                if raw in vmap:
                    df.at[i, col] = vmap[raw]
                elif raw.upper() in upper:
                    df.at[i, col] = upper[raw.upper()]
                    self._note(table, i, col, "code_casing", "fixed", raw, upper[raw.upper()])
                else:
                    df.at[i, col] = None
                    self._note(table, i, col, "unknown_code", "review", raw, "",
                               f"'{raw}' is not a known code for {col}")

    def parse_dates(self, table: str) -> None:
        df = self.t[table]
        for col in [c for c in df.columns if c.endswith("_date")]:
            swapped = 0
            ambiguous = 0
            for i in self._rows(table):
                raw = df.at[i, col]
                iso, how = parse_date(raw, self.fmt)
                if how is None:
                    df.at[i, col] = iso
                    ambiguous += is_ambiguous_date(raw)
                elif how == "unparseable":
                    df.at[i, col] = None
                    self._note(table, i, col, "unparseable_date", "review", raw, "",
                               "not a recognisable date")
                else:
                    df.at[i, col] = iso
                    swapped += how == "swapped_date"
                    msg = {"excel_serial": "Excel serial day number converted",
                           "iso_date": "ISO date accepted",
                           "swapped_date": "only valid as MM/DD, day and month swapped"}[how]
                    self._note(table, i, col, how, "fixed", raw, iso, msg)
            if swapped and ambiguous:
                self._note(table, None, col, "ambiguous_date_format", "warning", "", "",
                           f"{swapped} values were only valid as MM/DD, so the column mixes "
                           f"formats; {ambiguous} values valid either way were read as "
                           f"{self.fmt} and cannot be verified from the file alone")

    def parse_frequencies(self) -> None:
        table = "pm_schedules"
        df = self.t[table]
        for i in self._rows(table):
            raw = df.at[i, "frequency_value"]
            if raw == "":
                df.at[i, "frequency_value"] = None
                continue
            parsed = parse_frequency(raw)
            if parsed is None:
                df.at[i, "frequency_value"] = None
                self._note(table, i, "frequency_value", "unparseable_frequency", "review",
                           raw, "", "frequency text not understood")
                continue
            value, unit = parsed
            if unit is None:
                df.at[i, "frequency_value"] = value
                continue
            col_unit = df.at[i, "frequency_unit"]
            if col_unit not in (None, "") and col_unit != unit:
                df.at[i, "frequency_value"] = None
                self._note(table, i, "frequency_value", "frequency_unit_conflict", "review",
                           raw, f"{value} {unit}",
                           f"text says {unit}, unit column says {col_unit}")
                continue
            df.at[i, "frequency_value"] = value
            if col_unit in (None, ""):
                df.at[i, "frequency_unit"] = unit
            self._note(table, i, "frequency_value", "free_text_frequency", "fixed", raw,
                       f"{value} {unit}", "free-text frequency parsed")

    def normalize_asset_ids(self) -> None:
        table = "assets"
        df = self.t[table]
        live = self._rows(table)
        canonical = {id_key(df.at[i, "asset_id"]): df.at[i, "asset_id"] for i in live
                     if CANONICAL_ASSET_ID.match(df.at[i, "asset_id"])}
        for i in live:
            v = df.at[i, "asset_id"]
            if v == "" or CANONICAL_ASSET_ID.match(v):
                continue
            twin = canonical.get(id_key(v))
            if twin is not None:
                self._note(table, i, "asset_id", "near_duplicate", "review", v, twin,
                           f"same asset as {twin} written differently; merge into {twin}?")
            elif CANONICAL_ASSET_ID.match(v.upper()):
                df.at[i, "asset_id"] = v.upper()
                self._note(table, i, "asset_id", "id_casing", "fixed", v, v.upper())
            else:
                self._note(table, i, "asset_id", "nonstandard_id", "review", v, "",
                           "asset ID does not follow the site numbering pattern")

    def infer_work_order_fields(self) -> None:
        """Fill blanks that a domain rule determines exactly."""
        table = "work_orders"
        df = self.t[table]
        pm_asset = {}
        if "pm_schedules" in self.t:
            pm = self.t["pm_schedules"]
            pm_asset = {pm.at[j, "pm_id"]: pm.at[j, "asset_id"]
                        for j in self._rows("pm_schedules")}
        for i in self._rows(table):
            pm_id = df.at[i, "pm_id"]
            if df.at[i, "wo_type"] == "" and pm_id:
                df.at[i, "wo_type"] = "PM"
                self._note(table, i, "wo_type", "inferred_from_pm", "fixed", "", "PM",
                           "work order generated from a PM schedule is a PM")
            if df.at[i, "asset_id"] == "" and pm_asset.get(pm_id):
                df.at[i, "asset_id"] = pm_asset[pm_id]
                self._note(table, i, "asset_id", "inferred_from_pm", "fixed", "",
                           pm_asset[pm_id], "asset taken from the linked PM schedule")

    def required_fields(self, table: str) -> None:
        df = self.t[table]
        for col in REQUIRED[table]:
            for i in self._rows(table):
                if df.at[i, col] in ("", None) and not self._has_review(table, i, col):
                    df.at[i, col] = None
                    self._note(table, i, col, "blank_required", "review", "", "",
                               f"{col} is required")

    def _has_review(self, table: str, i: int, col: str) -> bool:
        return any(f.table == table and f.row_index == i and f.target_field == col
                   and f.action == "review" for f in self.findings[-200:])

    def duplicate_keys(self, table: str) -> None:
        df = self.t[table]
        pk = TABLES[table][0]
        first: dict[str, int] = {}
        for i in self._rows(table):
            k = df.at[i, pk]
            if not k:
                continue
            if k in first:
                self._note(table, i, pk, "duplicate_key", "review", k, "",
                           f"same key as excel_row {first[k] + 2} with different values")
            else:
                first[k] = i

    # ---- cross-table checks ----------------------------------------------
    def _ids(self, table: str) -> set[str]:
        df = self.t[table]
        pk = TABLES[table][0]
        return {df.at[i, pk] for i in self._rows(table) if df.at[i, pk]}

    def references(self) -> None:
        loc_ids = self._ids("locations") if "locations" in self.t else set()
        loc_by_key = {}
        for lid in loc_ids:
            loc_by_key.setdefault(location_key(lid), []).append(lid)

        if "locations" in self.t:
            df = self.t["locations"]
            for i in self._rows("locations"):
                p = df.at[i, "parent_location_id"]
                if p and p not in loc_ids:
                    self._note("locations", i, "parent_location_id", "orphan_parent",
                               "review", p, "", "parent location not in export")
                if not p:
                    df.at[i, "parent_location_id"] = None

        if "assets" in self.t:
            df = self.t["assets"]
            asset_ids = self._ids("assets")
            for i in self._rows("assets"):
                loc = df.at[i, "location_id"]
                if loc and loc not in loc_ids:
                    self._fix_location(i, loc, loc_ids, loc_by_key)
                p = df.at[i, "parent_asset_id"]
                if not p:
                    df.at[i, "parent_asset_id"] = None
                elif p not in asset_ids:
                    self._note("assets", i, "parent_asset_id", "orphan_parent", "review", p,
                               "", "parent asset not in export")

        asset_ids = self._ids("assets") if "assets" in self.t else set()
        pm_asset = {}
        if "pm_schedules" in self.t:
            pm = self.t["pm_schedules"]
            pm_asset = {pm.at[j, "pm_id"]: pm.at[j, "asset_id"]
                        for j in self._rows("pm_schedules")}
        for table in ("pm_schedules", "work_orders"):
            if table not in self.t:
                continue
            df = self.t[table]
            for i in self._rows(table):
                a = df.at[i, "asset_id"]
                if a and a not in asset_ids:
                    hint = pm_asset.get(df.at[i, "pm_id"], "") if table == "work_orders" else ""
                    self._note(table, i, "asset_id", "orphan_reference", "review", a, hint,
                               f"asset not in export; its PM points to {hint}" if hint
                               else "asset not in export")
        if "work_orders" in self.t:
            df = self.t["work_orders"]
            pm_ids = self._ids("pm_schedules") if "pm_schedules" in self.t else set()
            for i in self._rows("work_orders"):
                p = df.at[i, "pm_id"]
                if not p:
                    df.at[i, "pm_id"] = None
                elif p not in pm_ids:
                    self._note("work_orders", i, "pm_id", "orphan_reference", "review", p,
                               "", "PM schedule not in export")

    def _fix_location(self, i, loc, loc_ids, loc_by_key) -> None:
        df = self.t["assets"]
        if loc.upper() in loc_ids:
            df.at[i, "location_id"] = loc.upper()
            self._note("assets", i, "location_id", "location_casing", "fixed", loc,
                       loc.upper())
            return
        hits = loc_by_key.get(location_key(loc), [])
        if len(hits) == 1:
            df.at[i, "location_id"] = hits[0]
            self._note("assets", i, "location_id", "location_format", "fixed", loc, hits[0],
                       "same code with different separators / zero padding")
            return
        close = difflib.get_close_matches(location_key(loc), list(loc_by_key), n=2, cutoff=0.75)
        suggestion = loc_by_key[close[0]][0] if close else ""
        self._note("assets", i, "location_id", "unknown_location", "review", loc, suggestion,
                   f"not a known location; closest is {suggestion}" if suggestion
                   else "not a known location")

    def consistency(self) -> None:
        if "pm_schedules" in self.t and "assets" in self.t:
            pm, a = self.t["pm_schedules"], self.t["assets"]
            meter = {a.at[j, "asset_id"]: a.at[j, "meter_type"] for j in self._rows("assets")}
            for i in self._rows("pm_schedules"):
                trig, unit = pm.at[i, "trigger_type"], pm.at[i, "frequency_unit"]
                if not trig or not unit:
                    continue
                if trig == "TIME" and unit != "DAYS":
                    self._note("pm_schedules", i, "frequency_unit", "unit_mismatch", "review",
                               unit, "DAYS", "time-based PM must be scheduled in days")
                elif trig == "METER":
                    m = meter.get(pm.at[i, "asset_id"])
                    if m and m != unit:
                        self._note("pm_schedules", i, "frequency_unit", "unit_mismatch",
                                   "review", unit, m,
                                   f"meter PM in {unit} but the asset's meter is {m}")

        if "work_orders" in self.t:
            wo = self.t["work_orders"]
            pm = self.t.get("pm_schedules")
            est, pm_asset = {}, {}
            assets = self._ids("assets") if "assets" in self.t else set()
            if pm is not None:
                for j in self._rows("pm_schedules"):
                    pm_asset[pm.at[j, "pm_id"]] = pm.at[j, "asset_id"]
                    try:
                        est[pm.at[j, "pm_id"]] = float(pm.at[j, "est_labor_hours"])
                    except (TypeError, ValueError):
                        pass
            for i in self._rows("work_orders"):
                self._labor(wo, i, est)
                p, a = wo.at[i, "pm_id"], wo.at[i, "asset_id"]
                if (p and a and a in assets and p in pm_asset and pm_asset[p]
                        and pm_asset[p] != a):
                    self._note("work_orders", i, "asset_id", "pm_asset_mismatch", "review", a,
                               pm_asset[p], "work order asset differs from its PM's asset")
                rep, sch, comp = (wo.at[i, "reported_date"], wo.at[i, "scheduled_date"],
                                  wo.at[i, "completed_date"])
                if rep and comp and comp < rep:
                    for col in ("reported_date", "completed_date"):
                        self._note("work_orders", i, col, "date_order", "review",
                                   wo.at[i, col], "", "completed before reported")
                if rep and sch and sch < rep:
                    for col in ("reported_date", "scheduled_date"):
                        self._note("work_orders", i, col, "date_order", "review",
                                   wo.at[i, col], "", "scheduled before reported")

        for table, cols in (("assets", ["install_date"]), ("pm_schedules", ["last_done_date"]),
                            ("work_orders", ["reported_date", "completed_date"])):
            if table not in self.t:
                continue
            df = self.t[table]
            for col in cols:
                for i in self._rows(table):
                    v = df.at[i, col]
                    if v and v > self.as_of:
                        self._note(table, i, col, "future_date", "review", v, "",
                                   f"after the extract date {self.as_of}")

    def _labor(self, wo: pd.DataFrame, i: int, est: dict) -> None:
        raw = wo.at[i, "labor_hours"]
        if raw in ("", None):
            wo.at[i, "labor_hours"] = 0.0
            return
        try:
            h = float(raw)
        except ValueError:
            self._note("work_orders", i, "labor_hours", "not_a_number", "review", raw)
            return
        wo.at[i, "labor_hours"] = h
        e = est.get(wo.at[i, "pm_id"]) if wo.at[i, "wo_type"] == "PM" else None
        too_high = h > PM_LABOR_RATIO_MAX * e if e else h > LABOR_HOURS_MAX
        if too_high:
            self._note("work_orders", i, "labor_hours", "labor_outlier", "review", raw,
                       f"{h / 60:g}", "implausible hours; recorded in minutes?")

    def cascade_holds(self) -> None:
        def blocked(table):
            df, pk = self.t[table], TABLES[table][0]
            return {df.at[i, pk] for i, s in enumerate(self.status[table])
                    if s in ("review", "held") and df.at[i, pk]
                    and not self._is_duplicate_row(table, i)}

        def hold(table, col, bad, why):
            df = self.t[table]
            for i in self._rows(table):
                if self.status[table][i] == "ok" and df.at[i, col] in bad:
                    self._note(table, i, col, "held_dependency", "held", df.at[i, col], "",
                               why)

        if "assets" in self.t:
            if "locations" in self.t:
                hold("assets", "location_id", blocked("locations"), "location held for review")
            hold("assets", "parent_asset_id", blocked("assets"), "parent asset held for review")
        if "pm_schedules" in self.t:
            hold("pm_schedules", "asset_id", blocked("assets"), "asset held for review")
        if "work_orders" in self.t:
            hold("work_orders", "asset_id", blocked("assets"), "asset held for review")
            if "pm_schedules" in self.t:
                hold("work_orders", "pm_id", blocked("pm_schedules"), "PM held for review")

    def _is_duplicate_row(self, table: str, i: int) -> bool:
        return any(f.table == table and f.row_index == i
                   and f.check in ("near_duplicate", "duplicate_key") for f in self.findings)

    # ---- output ------------------------------------------------------------
    def output_tables(self) -> dict[str, pd.DataFrame]:
        out = {}
        for table, df in self.t.items():
            keep = [i for i, s in enumerate(self.status[table]) if s == "ok"]
            o = df.iloc[keep].reset_index(drop=True).replace({"": None})
            for col in o.columns:
                if col in INT_COLUMNS:
                    o[col] = o[col].astype(int)
                elif col in FLOAT_COLUMNS:
                    o[col] = o[col].astype(float)
            out[table] = o
        return out


def validate(legacy: dict[str, pd.DataFrame], mapping: dict,
             as_of: date | None = None) -> Result:
    return Validator(mapping, as_of).run(legacy)


def write_result(result: Result, out_dir: Path) -> None:
    out_dir = Path(out_dir)
    (out_dir / "clean").mkdir(parents=True, exist_ok=True)
    for name, df in result.tables.items():
        df.to_csv(out_dir / "clean" / f"{name}.csv", index=False, lineterminator="\n")
    result.exceptions().to_csv(out_dir / "exceptions.csv", index=False, lineterminator="\n")
    (out_dir / "summary.txt").write_text(result.summary_text() + "\n")
