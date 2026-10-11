"""LLM mapper: propose a source -> target mapping for a legacy export it has never seen.

1. Profile every sheet and column deterministically (names, fill rate, distinct
   values, top values, value shapes, samples).
2. Ask the LLM, with a strict JSON schema, which table each sheet feeds, which
   target field each column feeds, how the legacy codes translate, and which date
   format the export uses. Every answer carries a confidence and a one-line reason.
3. Apply deterministic guards before anything is used: targets must exist, one
   source per target, codes must be observed in the data and land on an allowed
   value, and only confident exact synonyms are applied. Everything else is left
   unmapped, so the validator routes it to review.

The output is a mapping in the same format as ``mappings/legacy_cmms.json`` plus
a report with every proposal, its confidence and reason, and what the guards did.
"""

from __future__ import annotations

import json
import re
from collections import Counter

import pandas as pd

from onboard.llm import LLM
from onboard.schema import CODED_FIELDS, FIELD_DOCS, TABLES

COLUMN_MIN_CONFIDENCE = 0.6
CODE_MIN_CONFIDENCE = 0.8
DATE_FORMATS = ["%d/%m/%Y", "%m/%d/%Y", "%Y-%m-%d"]
N_SAMPLES = 6
N_TOP = 15
MAX_DISTINCT_FOR_CODES = 40


# --------------------------------------------------------------------------
# Profiling
# --------------------------------------------------------------------------

def _shape(v: str) -> str:
    s = re.sub(r"[0-9]", "9", v)
    s = re.sub(r"[A-Za-z]", "A", s)
    return re.sub(r"(A)A+", r"\1+", s)


def profile_sheet(name: str, df: pd.DataFrame) -> dict:
    cols = []
    n = len(df)
    for c in df.columns:
        vals = [str(v).strip() for v in df[c]]
        filled = [v for v in vals if v]
        counts = Counter(filled)
        step = max(1, len(filled) // N_SAMPLES)
        samples = [filled[i] for i in range(0, len(filled), step)][:N_SAMPLES]
        col = {"name": c, "filled": len(filled), "distinct": len(counts),
               "shapes": dict(Counter(_shape(v) for v in filled).most_common(4)),
               "samples": samples}
        if 0 < len(counts) <= MAX_DISTINCT_FOR_CODES:
            col["values"] = dict(counts.most_common(N_TOP))
        cols.append(col)
    return {"sheet": name, "rows": n, "columns": cols}


def profile_export(legacy: dict[str, pd.DataFrame]) -> list[dict]:
    return [profile_sheet(name, df) for name, df in legacy.items()]


# --------------------------------------------------------------------------
# Prompt and schema
# --------------------------------------------------------------------------

SYSTEM = """You map legacy enterprise-system exports onto a target schema.
You get a profile of every sheet and column of an export you have never seen, and the
target schema. Decide:
- which target table each sheet feeds ("none" if it feeds no table),
- which target field each source column feeds ("none" if it has no target),
- for coded target fields, what each observed legacy code means in the target's
  allowed values,
- which date format the export uses for dates (the majority format).
Rules:
- Base every decision on the column names, values and shapes in the profile.
- A code translation must be an exact synonym. If a code could mean something else, or
  would need a unit conversion (miles vs km, weeks vs days, kWh vs MWh), set
  exact_synonym to false. Never guess: a wrong translation silently corrupts data.
- Give each decision a confidence between 0 and 1 and a one-line reason."""


def target_schema_text() -> str:
    lines = []
    for table, fields in FIELD_DOCS.items():
        lines.append(f"table {table}:")
        for f, doc in fields.items():
            allowed = CODED_FIELDS.get((table, f))
            extra = f" [allowed: {', '.join(map(str, allowed))}]" if allowed else ""
            lines.append(f"  - {f}: {doc}{extra}")
    return "\n".join(lines)


def build_prompt(profile: list[dict]) -> str:
    return ("TARGET SCHEMA\n" + target_schema_text() + "\n\nEXPORT PROFILE (JSON)\n"
            + json.dumps(profile, ensure_ascii=False, separators=(",", ":")))


def response_schema() -> dict:
    tables = list(TABLES) + ["none"]
    fields = sorted({f for cols in TABLES.values() for f in cols}) + ["none"]
    num = {"type": "number"}
    txt = {"type": "string"}
    column = {"type": "object", "additionalProperties": False,
              "required": ["source", "target", "confidence", "reason"],
              "properties": {"source": txt, "target": {"type": "string", "enum": fields},
                             "confidence": num, "reason": txt}}
    code = {"type": "object", "additionalProperties": False,
            "required": ["source_column", "code", "value", "exact_synonym", "confidence",
                         "reason"],
            "properties": {"source_column": txt, "code": txt, "value": txt,
                           "exact_synonym": {"type": "boolean"}, "confidence": num,
                           "reason": txt}}
    sheet = {"type": "object", "additionalProperties": False,
             "required": ["sheet", "table", "confidence", "reason", "columns", "codes"],
             "properties": {"sheet": txt, "table": {"type": "string", "enum": tables},
                            "confidence": num, "reason": txt,
                            "columns": {"type": "array", "items": column},
                            "codes": {"type": "array", "items": code}}}
    return {"type": "object", "additionalProperties": False,
            "required": ["date_format", "date_format_confidence", "date_format_reason",
                         "sheets"],
            "properties": {"date_format": {"type": "string", "enum": DATE_FORMATS},
                           "date_format_confidence": num, "date_format_reason": txt,
                           "sheets": {"type": "array", "items": sheet}}}


# --------------------------------------------------------------------------
# Guards: proposal -> mapping
# --------------------------------------------------------------------------

def _coerce(table: str, field: str, value: str):
    allowed = CODED_FIELDS[(table, field)]
    if isinstance(allowed[0], int):
        try:
            v = int(str(value).strip())
        except ValueError:
            return None
        return v if v in allowed else None
    v = str(value).strip().upper()
    return v if v in allowed else None


def apply_guards(proposal: dict, legacy: dict[str, pd.DataFrame]) -> tuple[dict, list[dict]]:
    """Turn the raw LLM proposal into a mapping. Returns (mapping, decisions log)."""
    log: list[dict] = []

    def note(kind, sheet, item, decision, detail, conf=None, reason=""):
        log.append({"kind": kind, "sheet": sheet, "item": item, "decision": decision,
                    "detail": detail, "confidence": conf, "reason": reason})

    sheets: dict[str, dict] = {}
    used_tables: dict[str, tuple[str, float]] = {}
    for sp in sorted(proposal["sheets"], key=lambda s: -s["confidence"]):
        sheet, table = sp["sheet"], sp["table"]
        if sheet not in legacy:
            note("sheet", sheet, sheet, "rejected", "sheet not in export")
            continue
        if table == "none":
            note("sheet", sheet, sheet, "skipped", "proposed no target table",
                 sp["confidence"], sp["reason"])
            continue
        if table in used_tables:
            note("sheet", sheet, table, "rejected",
                 f"table already fed by {used_tables[table][0]}", sp["confidence"],
                 sp["reason"])
            continue
        used_tables[table] = (sheet, sp["confidence"])
        note("sheet", sheet, table, "accepted", "", sp["confidence"], sp["reason"])

        src_cols = list(legacy[sheet].columns)
        columns: dict[str, str] = {}
        best: dict[str, float] = {}
        for c in sorted(sp["columns"], key=lambda c: -c["confidence"]):
            src, tgt = c["source"], c["target"]
            item = f"{src} -> {tgt}"
            if src not in src_cols:
                note("column", sheet, item, "rejected", "no such source column",
                     c["confidence"], c["reason"])
            elif tgt == "none":
                note("column", sheet, item, "skipped", "no target", c["confidence"],
                     c["reason"])
            elif tgt not in TABLES[table]:
                note("column", sheet, item, "rejected", f"{tgt} is not a field of {table}",
                     c["confidence"], c["reason"])
            elif src in columns:
                note("column", sheet, item, "rejected", "source already mapped",
                     c["confidence"], c["reason"])
            elif tgt in best:
                note("column", sheet, item, "rejected", "target already mapped",
                     c["confidence"], c["reason"])
            elif c["confidence"] < COLUMN_MIN_CONFIDENCE:
                note("column", sheet, item, "review", "low confidence, left unmapped",
                     c["confidence"], c["reason"])
            else:
                columns[src] = tgt
                best[tgt] = c["confidence"]
                note("column", sheet, item, "accepted", "", c["confidence"], c["reason"])
        for src in src_cols:
            if src not in columns and not any(e["item"].startswith(f"{src} ->")
                                              for e in log if e["sheet"] == sheet):
                note("column", sheet, f"{src} -> ?", "review", "no proposal")

        # value maps
        value_maps: dict[str, dict] = {}
        by_target = {tgt: src for src, tgt in columns.items()}
        for k in sorted(sp["codes"], key=lambda k: -k["confidence"]):
            src = k["source_column"]
            tgt = columns.get(src)
            item = f"{src}: {k['code']} -> {k['value']}"
            if tgt is None or (table, tgt) not in CODED_FIELDS:
                note("code", sheet, item, "rejected", "column is not mapped to a coded field",
                     k["confidence"], k["reason"])
                continue
            observed = {str(v).strip() for v in legacy[sheet][src]}
            value = _coerce(table, tgt, k["value"])
            vm = value_maps.setdefault(tgt, {})
            if k["code"] not in observed:
                note("code", sheet, item, "rejected", "code not present in the data",
                     k["confidence"], k["reason"])
            elif value is None:
                note("code", sheet, item, "rejected", "value not allowed for the field",
                     k["confidence"], k["reason"])
            elif not k["exact_synonym"]:
                note("code", sheet, item, "review", "not an exact synonym, left for review",
                     k["confidence"], k["reason"])
            elif k["confidence"] < CODE_MIN_CONFIDENCE:
                note("code", sheet, item, "review", "low confidence, left for review",
                     k["confidence"], k["reason"])
            elif k["code"] in vm:
                note("code", sheet, item, "rejected", "code already translated",
                     k["confidence"], k["reason"])
            else:
                vm[k["code"]] = value
                note("code", sheet, item, "accepted", "", k["confidence"], k["reason"])
        # Values that already are allowed target values pass through unchanged.
        for tgt, src in by_target.items():
            allowed = CODED_FIELDS.get((table, tgt))
            if not allowed or tgt not in value_maps:
                continue
            observed = {str(v).strip() for v in legacy[sheet][src]}
            for a in allowed:
                if str(a) in observed and str(a) not in value_maps[tgt]:
                    value_maps[tgt][str(a)] = a
        sheets[sheet] = {"table": table, "columns": columns, "value_maps": value_maps}

    fmt = proposal["date_format"]
    note("date_format", "", fmt, "accepted", "", proposal["date_format_confidence"],
         proposal["date_format_reason"])
    return {"source_date_format": fmt, "sheets": sheets}, log


def propose_mapping(llm: LLM, legacy: dict[str, pd.DataFrame]) -> tuple[dict, dict]:
    profile = profile_export(legacy)
    proposal = llm.json("map", SYSTEM, build_prompt(profile), response_schema(),
                        schema_name="mapping_proposal")
    mapping, log = apply_guards(proposal, legacy)
    return mapping, {"proposal": proposal, "decisions": log}


# --------------------------------------------------------------------------
# Accuracy against a reference mapping
# --------------------------------------------------------------------------

def _non_identity(vm: dict) -> dict:
    return {str(k): v for k, v in vm.items() if str(k) != str(v)}


def mapping_accuracy(proposed: dict, reference: dict) -> dict:
    sheets_ok = cols_ok = cols_n = codes_ok = codes_n = codes_wrong = 0
    errors = []
    for sheet, ref in reference["sheets"].items():
        got = proposed["sheets"].get(sheet)
        if got and got["table"] == ref["table"]:
            sheets_ok += 1
        else:
            errors.append(f"{sheet}: table {got and got['table']} != {ref['table']}")
        got_cols = got["columns"] if got else {}
        for src, tgt in ref["columns"].items():
            cols_n += 1
            if got_cols.get(src) == tgt:
                cols_ok += 1
            else:
                errors.append(f"{sheet}.{src}: {got_cols.get(src)} != {tgt}")
        got_vm = got["value_maps"] if got else {}
        for field, vm in ref["value_maps"].items():
            ref_codes = _non_identity(vm)
            mine = {str(k): v for k, v in got_vm.get(field, {}).items()}
            for code, val in ref_codes.items():
                codes_n += 1
                if mine.get(code) == val:
                    codes_ok += 1
                else:
                    errors.append(f"{sheet}.{field} code {code}: {mine.get(code)} != {val}")
        for field, vm in got_vm.items():
            ref_vm = ref["value_maps"].get(field, {})
            for code, val in vm.items():
                if code in ref_vm and ref_vm[code] != val:
                    continue  # already counted above
                if code not in ref_vm and str(code) != str(val):
                    codes_wrong += 1
                    errors.append(f"{sheet}.{field} extra code {code} -> {val} (not in reference)")
    n_sheets = len(reference["sheets"])
    return {
        "sheets_correct": sheets_ok, "sheets_total": n_sheets,
        "columns_correct": cols_ok, "columns_total": cols_n,
        "codes_correct": codes_ok, "codes_total": codes_n,
        "codes_extra_wrong": codes_wrong,
        "date_format_correct": proposed["source_date_format"] == reference["source_date_format"],
        "column_accuracy": round(cols_ok / cols_n, 4) if cols_n else 1.0,
        "code_accuracy": round(codes_ok / codes_n, 4) if codes_n else 1.0,
        "errors": errors,
    }
