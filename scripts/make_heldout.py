"""Build the held-out legacy export used in docs/eval.md ("Held-out").

Written to test the pipeline on data it was not developed against: nothing in the
mapper, validator, review pass or LLM fixtures has seen this file. Compared with the
main export (data/legacy/legacy_export.xlsx) it has

* a different seed (7) for both the clean data and the corruptions;
* different sheet names and order, SAP-style column names, a different column order;
* different legacy codes (asset classes, statuses, units, criticality, ...);
* a merged MAKE_MODEL column ("<manufacturer> / <model>") instead of two columns;
* two corruption types the validator was not written for: comma decimals in hours
  ("2,5") and thousands separators in PM intervals ("2,000", and "8.000" European style).

Outputs, under data/heldout/:
    legacy_export.xlsx              the export
    reference_mapping.json          the correct mapping, for scoring the mapper only
    ground_truth/                   clean CSVs, corruptions.csv, row_map.csv, column_mapping.csv

    python scripts/make_heldout.py
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from onboard import corrupt as c
from onboard.generate import read_tables, write_tables
from onboard.mapping import reference_mapping

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "heldout"
SEED = 7

SHEETS = {"EQUIPMENT": "ASSET_REG", "WORK_ORDERS": "JOB_HIST", "PM_SCHED": "MAINT_PLAN",
          "LOCATIONS": "FUNC_LOC"}  # also the new sheet order
COLUMNS = {
    "LOCATIONS": {"LOC_CD": "FLOC", "LOC_DESC": "FLOC_TXT", "PARENT_LOC": "SUP_FLOC",
                  "SITE_CD": "PLANT", "LOC_TYPE": "FLOC_CAT"},
    "EQUIPMENT": {"EQUIP_NO": "TAG", "EQUIP_DESC": "TAG_TXT", "EQ_CLASS": "OBJ_TYPE",
                  "PARENT_EQ": "SUP_TAG", "LOC_CD": "FLOC", "CRIT": "ABC_IND",
                  "SERIAL_NO": "SER_NR", "INSTALL_DT": "START_UP", "METER_TYPE": "CNTR_TYP",
                  "EQ_STATUS": "SYS_STAT"},
    "PM_SCHED": {"PM_NO": "PLAN_ID", "EQUIP_NO": "TAG", "PM_DESC": "PLAN_TXT",
                 "PM_TYPE": "CYCLE_KIND", "PM_FREQ": "INTERVAL", "FREQ_UOM": "INT_UNIT",
                 "CRAFT": "WORK_CTR", "EST_HRS": "PLAN_HRS", "LAST_DONE_DT": "LAST_CALL"},
    "WORK_ORDERS": {"WO_NUM": "JOB_NO", "EQUIP_NO": "TAG", "PM_NO": "PLAN_ID",
                    "WO_TYPE": "JOB_TYPE", "WO_STATUS": "USR_STAT", "PRIORITY": "PRIO",
                    "WO_DESC": "SHORT_TXT", "OPEN_DT": "NOTIF_DT", "SCHED_DT": "BASIC_START",
                    "COMP_DT": "ACT_FINISH", "LABOR_HRS": "ACT_WORK"},
}
ORDER = {  # new column order per (new) sheet
    "FUNC_LOC": ["PLANT", "FLOC", "FLOC_CAT", "SUP_FLOC", "FLOC_TXT"],
    "ASSET_REG": ["TAG", "OBJ_TYPE", "TAG_TXT", "FLOC", "SUP_TAG", "SYS_STAT", "ABC_IND",
                  "MAKE_MODEL", "SER_NR", "CNTR_TYP", "START_UP"],
    "MAINT_PLAN": ["PLAN_ID", "PLAN_TXT", "TAG", "WORK_CTR", "CYCLE_KIND", "INTERVAL",
                   "INT_UNIT", "LAST_CALL", "PLAN_HRS"],
    "JOB_HIST": ["JOB_NO", "JOB_TYPE", "USR_STAT", "PRIO", "TAG", "PLAN_ID", "SHORT_TXT",
                 "NOTIF_DT", "BASIC_START", "ACT_FINISH", "ACT_WORK"],
}
# old legacy code -> held-out legacy code, per (old sheet, old column)
RECODE = {
    ("LOCATIONS", "LOC_TYPE"): {"SITE": "PL", "AREA": "AR", "SYS": "SY"},
    ("EQUIPMENT", "EQ_CLASS"): {
        "CONV": "CVY", "CRSH": "CRU", "HTRK": "HAUL", "PUMP": "PMP", "BESS": "ESS",
        "SCRN": "SCR", "FEED": "FDR", "COMP": "CMP", "XFMR": "TRF", "FAN": "FN",
        "STKR": "S/R", "MOTR": "MTR", "GBOX": "GBX", "PULY": "PUL", "BELT": "BLT",
        "LUBE": "LUB", "HPU": "HYD", "ENGN": "ENG", "TRNS": "TRM", "FDRV": "FD",
        "PCS": "INV", "BATT": "BAT", "HVAC": "AC", "FIRE": "FSS"},
    ("EQUIPMENT", "CRIT"): {"A": "H", "B": "M", "C": "L"},
    ("EQUIPMENT", "METER_TYPE"): {"HRS": "OPH", "CYC": "CNT", "N/A": "-"},
    ("EQUIPMENT", "EQ_STATUS"): {"A": "ACTV", "I": "INAC"},
    ("PM_SCHED", "PM_TYPE"): {"T": "CAL", "M": "CNT"},
    ("PM_SCHED", "FREQ_UOM"): {"D": "DAY", "H": "HR", "W": "WK"},
    ("WORK_ORDERS", "WO_STATUS"): {"WAPPR": "CRTD", "INPRG": "REL", "COMP": "TECO",
                                   "CAN": "DLFL"},
}


def recode(sheet: str, col: str, v):
    m = RECODE.get((sheet, col))
    if not m or not isinstance(v, str):
        return v
    core = v.strip()
    return v.replace(core, m[core]) if core in m else v


def new_corruptions(k: c.Corruptor) -> None:
    """Corruption types the validator was not written for."""
    pm, wo = k.sheets["PM_SCHED"], k.sheets["WORK_ORDERS"]
    frac = lambda s, col: lambda i: "." in s.df.at[i, col]  # noqa: E731
    for r in k._pick(wo, "LABOR_HRS", 60, where=frac(wo, "LABOR_HRS")):
        k._set(wo, r, "LABOR_HRS", wo.df.at[r, "LABOR_HRS"].replace(".", ","),
               "comma_decimal", "decimal comma (2,5 = 2.5 h)")
    for r in k._pick(pm, "EST_HRS", 30, where=frac(pm, "EST_HRS")):
        k._set(pm, r, "EST_HRS", pm.df.at[r, "EST_HRS"].replace(".", ","),
               "comma_decimal", "decimal comma (2,5 = 2.5 h)")
    big = lambda i: pm.df.at[i, "PM_FREQ"].isdigit() and int(pm.df.at[i, "PM_FREQ"]) >= 1000  # noqa: E731
    for n, r in enumerate(k._pick(pm, "PM_FREQ", 50, where=big)):
        v = int(pm.df.at[r, "PM_FREQ"])
        if n % 4 == 3:
            k._set(pm, r, "PM_FREQ", f"{v:,}".replace(",", "."), "thousands_separator",
                   "European thousands separator (8.000 = 8000)")
        else:
            k._set(pm, r, "PM_FREQ", f"{v:,}", "thousands_separator",
                   "thousands separator (2,000 = 2000)")


def build() -> dict:
    truth = OUT / "ground_truth"
    from onboard.generate import generate
    write_tables(generate(SEED), truth)
    k = c.Corruptor(read_tables(truth), SEED)
    k.run()
    new_corruptions(k)

    sheets = {}
    for old, new in SHEETS.items():
        df = k.sheets[old].df.copy()
        for col in df.columns:
            df[col] = df[col].map(lambda v, col=col: recode(old, col, v))
        if old == "EQUIPMENT":
            df["MAKE_MODEL"] = [f"{m} / {n}" if m or n else ""
                                for m, n in zip(df.MFR, df.MODEL_NO)]
            df = df.drop(columns=["MFR", "MODEL_NO"])
        sheets[new] = df.rename(columns=COLUMNS[old])[ORDER[new]]

    OUT.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(OUT / "legacy_export.xlsx", engine="openpyxl") as xw:
        for name, df in sheets.items():
            df.to_excel(xw, sheet_name=name, index=False)

    log = k.corruptions()
    for col in ("original_value", "corrupted_value"):
        log[col] = [recode(s, f, v) for s, f, v in zip(log.sheet, log.field, log[col])]
    log["field"] = [COLUMNS[s][f] for s, f in zip(log.sheet, log.field)]
    log["sheet"] = log.sheet.map(SHEETS)
    log.sort_values(["sheet", "row_index", "field"], kind="stable").to_csv(
        truth / "corruptions.csv", index=False, lineterminator="\n")
    rm = k.row_map()
    rm["sheet"] = rm.sheet.map(SHEETS)
    rm.to_csv(truth / "row_map.csv", index=False, lineterminator="\n")

    ref = reference_mapping()
    out_ref = {"source_date_format": ref["source_date_format"], "sheets": {}}
    for old, new in SHEETS.items():
        spec = ref["sheets"][old]
        cols = {COLUMNS[old][lc]: tc for lc, tc in spec["columns"].items()
                if lc in COLUMNS[old]}
        vms = {}
        for field, vm in spec["value_maps"].items():
            lc = next(lc for lc, tc in spec["columns"].items() if tc == field)
            vms[field] = {recode(old, lc, str(code)): val for code, val in vm.items()}
        out_ref["sheets"][new] = {"table": spec["table"], "columns": cols, "value_maps": vms}
    (OUT / "reference_mapping.json").write_text(json.dumps(out_ref, indent=1) + "\n")
    pd.DataFrame([{"legacy_sheet": s, "legacy_column": lc, "target_table": v["table"],
                   "target_column": tc} for s, v in out_ref["sheets"].items()
                  for lc, tc in v["columns"].items()]
                 + [{"legacy_sheet": "ASSET_REG", "legacy_column": "MAKE_MODEL",
                     "target_table": "assets", "target_column": "manufacturer + model"}]
                 ).to_csv(truth / "column_mapping.csv", index=False, lineterminator="\n")
    return {"rows": {n: len(d) for n, d in sheets.items()},
            "corruptions": log.corruption_type.value_counts().to_dict()}


if __name__ == "__main__":
    print(json.dumps(build(), indent=1))
