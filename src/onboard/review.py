"""LLM review pass over what the rules can't settle on their own.

Three kinds of question, each generated *deterministically* with a closed set of
options, so the model chooses between readings the rules already consider
possible and can never invent a value:

* ``date``  - a DD/MM-vs-MM/DD ambiguous date whose context looks wrong as read
  (out of line with neighbouring work orders, before its own reported date,
  matching a related record only when swapped...). Options: keep / swap.
* ``labor`` - a labor_outlier: was it booked in minutes? Options: hours / minutes.
* ``dup``   - a near-duplicate asset ID: same asset? Options: same / different.

Accepted answers (confidence >= threshold) become :class:`Override` /
:class:`DropRow` objects and the validator is run again from scratch, so every
LLM answer is re-checked by the same rules as the original data. Low-confidence
or "unsure" answers change nothing; the item stays in review.
"""

from __future__ import annotations

import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date, datetime

import pandas as pd

from onboard.llm import LLM
from onboard.validate import DropRow, Override, Result, is_ambiguous_date

MIN_CONFIDENCE = 0.85
BATCH = 40
REASONING_EFFORT = "medium"
PARALLEL = 8
NEIGHBOURS = 3
NEIGHBOUR_WINDOW = 5
DATE_TOLERANCE = 15     # days from the neighbours' median
DELTA_RANGE = (0, 45)   # plausible days from reported to scheduled/completed

OPTIONS = {"date": ["keep", "swap"], "labor": ["hours", "minutes"], "dup": ["same", "different"]}

SYSTEM = """You review data-quality questions from a maintenance-system (CMMS) data migration.
Each item is one question with a fixed set of options and the context the rules
collected. Pick the option the evidence supports, or "unsure".
Domain facts you can rely on:
- Work order numbers are assigned when a work order is raised, so reported dates rise with
  the work order number; neighbouring work orders were raised within days of each other.
- A work order is scheduled and completed on or after the day it was reported, usually
  within a few weeks.
- A PM's last-done date is normally the completion date of its latest work order.
- Component assets are installed together with, or after, their parent asset.
- Every date shown as context is one that cannot itself be a day/month misreading.
- Labor is booked in hours; a value that equals plausible hours x 60 was probably typed
  in minutes.
- Two asset rows that differ only in how the tag is written (case, separators) and have the
  same serial number, model and location are the same physical asset.
Be conservative: a wrong answer silently corrupts data, an "unsure" just sends the item to
a person. Give a confidence between 0 and 1 and a one-line reason."""


@dataclass
class Candidate:
    id: str
    kind: str
    sheet: str
    row_index: int
    field: str
    options: dict          # option -> value it would write (or row index for dups)
    context: dict

    def prompt_item(self) -> dict:
        return {"id": self.id, "options": self.options, "context": self.context}


def _d(iso: str | None) -> date | None:
    return date.fromisoformat(iso) if iso else None


def _swap(raw: str, fmt: str) -> tuple[str, str] | None:
    if fmt not in ("%d/%m/%Y", "%m/%d/%Y") or not is_ambiguous_date(raw):
        return None
    a = datetime.strptime(raw, fmt).date()
    other = "%m/%d/%Y" if fmt == "%d/%m/%Y" else "%d/%m/%Y"
    b = datetime.strptime(raw, other).date()
    return a.isoformat(), b.isoformat()


class CandidateFinder:
    def __init__(self, legacy: dict[str, pd.DataFrame], mapping: dict, result: Result):
        self.legacy = legacy
        self.mapping = mapping
        self.r = result
        self.fmt = mapping.get("source_date_format", "%d/%m/%Y")
        self.sheet = {spec["table"]: s for s, spec in mapping["sheets"].items()}
        self.col = {spec["table"]: {t: lc for lc, t in spec["columns"].items()}
                    for spec in mapping["sheets"].values()}
        self.out: list[Candidate] = []
        ex = result.exceptions()
        self.ex = ex[ex.row_index.notna()]

    def _live(self, table: str) -> list[int]:
        return [i for i, s in enumerate(self.r.status.get(table, [])) if s != "dropped"]

    def _raw(self, table: str, i: int, field: str) -> str:
        lc = self.col[table].get(field)
        return str(self.legacy[self.sheet[table]].at[i, lc]).strip() if lc else ""

    def _add(self, kind, table, i, field, options, context):
        cid = f"{kind[0].upper()}{len(self.out) + 1}"
        self.out.append(Candidate(cid, kind, self.sheet[table], i, self.col[table][field],
                                  options, context))

    def _trusted(self, table: str, i: int, field: str) -> bool:
        """A date that can't be a day/month misreading (so it is safe as evidence)."""
        raw = self._raw(table, i, field)
        return bool(raw) and not is_ambiguous_date(raw)

    def _rows_with(self, table: str, check: str) -> set[int]:
        sheet = self.sheet.get(table)
        e = self.ex[(self.ex.sheet == sheet) & (self.ex.check == check)]
        return set(e.row_index.astype(int))

    # ---- dates ---------------------------------------------------------------
    def work_order_dates(self) -> None:
        t = "work_orders"
        if t not in self.r.staged:
            return
        wo = self.r.staged[t]
        live = self._live(t)
        order = sorted((i for i in live if wo.at[i, "wo_id"]), key=lambda i: wo.at[i, "wo_id"])
        pos = {i: k for k, i in enumerate(order)}
        bad_order = self._rows_with(t, "date_order")
        pm = self.r.staged.get("pm_schedules")
        cadence = {}
        if pm is not None:
            for j in range(len(pm)):
                if pm.at[j, "frequency_unit"] == "DAYS":
                    cadence[pm.at[j, "pm_id"]] = pm.at[j, "frequency_value"]
        for i in order:
            raws = {f: self._raw(t, i, f) for f in
                    ("reported_date", "scheduled_date", "completed_date")}
            rep = _d(wo.at[i, "reported_date"])
            k = pos[i]
            near = [order[x] for x in range(max(0, k - NEIGHBOUR_WINDOW),
                                            min(len(order), k + NEIGHBOUR_WINDOW + 1)) if x != k]
            near_dates = [_d(wo.at[j, "reported_date"]) for j in near
                          if wo.at[j, "reported_date"] and self._trusted(t, j, "reported_date")]
            rep_trusted = self._trusted(t, i, "reported_date")
            med = statistics.median_low(near_dates) if near_dates else None
            for f, raw in raws.items():
                sw = _swap(raw, self.fmt)
                if not sw:
                    continue
                keep, swap = _d(sw[0]), _d(sw[1])
                suspicious = i in bad_order
                if f == "reported_date" and med:
                    suspicious |= (abs((keep - med).days) > DATE_TOLERANCE
                                   and abs((swap - med).days) <= DATE_TOLERANCE)
                elif f != "reported_date" and rep and rep_trusted:
                    lo, hi = DELTA_RANGE
                    suspicious |= (not lo <= (keep - rep).days <= hi
                                   and lo <= (swap - rep).days <= hi)
                if not suspicious:
                    continue
                ctx = {
                    "record": f"work order {wo.at[i, 'wo_id']}",
                    "question": f"{self.col[t][f]} = '{raw}': keep as read ({sw[0]}) "
                                f"or swap day/month ({sw[1]})?",
                    "type": wo.at[i, "wo_type"], "status": wo.at[i, "status"],
                    "dates_as_read": {x: (wo.at[i, x] if x == f or self._trusted(t, i, x)
                                          else "ambiguous") for x in raws},
                    "neighbours_reported": [
                        [wo.at[j, "wo_id"], wo.at[j, "reported_date"]] for j in near
                        if wo.at[j, "reported_date"] and self._trusted(t, j, "reported_date")
                    ][:2 * NEIGHBOURS],
                }
                if wo.at[i, "pm_id"] in cadence:
                    ctx["pm_cadence_days"] = cadence[wo.at[i, "pm_id"]]
                self._add("date", t, i, f, {"keep": sw[0], "swap": sw[1]}, ctx)

    def pm_dates(self) -> None:
        t = "pm_schedules"
        if t not in self.r.staged or "work_orders" not in self.r.staged:
            return
        pm, wo = self.r.staged[t], self.r.staged["work_orders"]
        done: dict[str, list[str]] = {}
        for j in self._live("work_orders"):
            if (wo.at[j, "pm_id"] and wo.at[j, "completed_date"]
                    and self._trusted("work_orders", j, "completed_date")):
                done.setdefault(wo.at[j, "pm_id"], []).append(wo.at[j, "completed_date"])
        for i in self._live(t):
            sw = _swap(self._raw(t, i, "last_done_date"), self.fmt)
            hist = sorted(done.get(pm.at[i, "pm_id"], []))
            if not sw or not hist:
                continue
            if sw[1] in hist and sw[0] not in hist:
                self._add("date", t, i, "last_done_date", {"keep": sw[0], "swap": sw[1]}, {
                    "record": f"PM {pm.at[i, 'pm_id']}",
                    "question": f"last done date '{self._raw(t, i, 'last_done_date')}': keep "
                                f"as read ({sw[0]}) or swap day/month ({sw[1]})?",
                    "frequency": f"{pm.at[i, 'frequency_value']} {pm.at[i, 'frequency_unit']}",
                    "latest_work_order_completions": hist[-6:]})

    def asset_dates(self) -> None:
        t = "assets"
        if t not in self.r.staged:
            return
        a = self.r.staged[t]
        live = self._live(t)
        inst = {a.at[i, "asset_id"]: a.at[i, "install_date"] for i in live
                if self._trusted(t, i, "install_date")}
        kids: dict[str, list[int]] = {}
        for i in live:
            if a.at[i, "parent_asset_id"]:
                kids.setdefault(a.at[i, "parent_asset_id"], []).append(i)
        for i in live:
            sw = _swap(self._raw(t, i, "install_date"), self.fmt)
            if not sw:
                continue
            parent = a.at[i, "parent_asset_id"]
            related = []
            if parent and inst.get(parent):
                related.append(inst[parent])
            sibs = kids.get(parent, []) if parent else kids.get(a.at[i, "asset_id"], [])
            related += [a.at[j, "install_date"] for j in sibs
                        if j != i and a.at[j, "install_date"] and self._trusted(t, j, "install_date")]
            p_inst = _d(inst.get(parent)) if parent else None
            suspicious = (sw[1] in related and sw[0] not in related) or (
                p_inst is not None and _d(sw[0]) < p_inst <= _d(sw[1]))
            if not suspicious:
                continue
            self._add("date", t, i, "install_date", {"keep": sw[0], "swap": sw[1]}, {
                "record": f"asset {a.at[i, 'asset_id']} ({a.at[i, 'description']})",
                "question": f"install date '{self._raw(t, i, 'install_date')}': keep as read "
                            f"({sw[0]}) or swap day/month ({sw[1]})?",
                "parent": parent, "parent_install_date": inst.get(parent) if parent else None,
                "related_install_dates": sorted(set(related))[:8]})

    # ---- labor -----------------------------------------------------------------
    def labor(self) -> None:
        t = "work_orders"
        if t not in self.r.staged:
            return
        wo = self.r.staged[t]
        pm = self.r.staged.get("pm_schedules")
        est = {}
        if pm is not None:
            est = {pm.at[j, "pm_id"]: pm.at[j, "est_labor_hours"] for j in range(len(pm))}
        ok = [i for i, s in enumerate(self.r.status[t]) if s == "ok"]
        by_desc: dict[str, list[float]] = {}
        for i in ok:
            by_desc.setdefault(wo.at[i, "description"], []).append(float(wo.at[i, "labor_hours"]))
        for i in sorted(self._rows_with(t, "labor_outlier")):
            raw = self._raw(t, i, "labor_hours")
            try:
                h = float(raw)
            except ValueError:
                continue
            same = by_desc.get(wo.at[i, "description"], [])
            self._add("labor", t, i, "labor_hours",
                      {"hours": f"{h:g}", "minutes": f"{h / 60:g}"}, {
                          "record": f"work order {wo.at[i, 'wo_id']}",
                          "question": f"labor '{raw}': hours as booked, or minutes "
                                      f"(= {h / 60:g} h)?",
                          "type": wo.at[i, "wo_type"], "description": wo.at[i, "description"],
                          "status": wo.at[i, "status"],
                          "pm_estimated_hours": est.get(wo.at[i, "pm_id"]),
                          "median_hours_same_description": statistics.median(same)
                          if same else None,
                          "n_same_description": len(same)})

    # ---- near-duplicates ---------------------------------------------------------
    def duplicates(self) -> None:
        t = "assets"
        if t not in self.r.staged:
            return
        a = self.r.staged[t]
        sheet = self.sheet[t]
        e = self.ex[(self.ex.sheet == sheet) & (self.ex.check == "near_duplicate")]
        for row in e.itertuples():
            i = int(row.row_index)
            twins = [j for j in self._live(t) if a.at[j, "asset_id"] == row.value and j != i]
            if not twins:
                continue
            j = twins[0]
            raw = self.legacy[sheet]
            self.out.append(Candidate(
                f"U{len(self.out) + 1}", "dup", sheet, i, self.col[t]["asset_id"],
                {"same": j, "different": None}, {
                    "question": f"is excel_row {i + 2} the same asset as excel_row {j + 2}?",
                    "row": {c: str(raw.at[i, c]) for c in raw.columns},
                    "other_row": {c: str(raw.at[j, c]) for c in raw.columns}}))

    def find(self) -> list[Candidate]:
        self.work_order_dates()
        self.pm_dates()
        self.asset_dates()
        self.labor()
        self.duplicates()
        return self.out


def _schema(ids: list[str], options: list[str]) -> dict:
    return {"type": "object", "additionalProperties": False, "required": ["decisions"],
            "properties": {"decisions": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["id", "decision", "confidence", "reason"],
                "properties": {"id": {"type": "string", "enum": ids},
                               "decision": {"type": "string", "enum": options + ["unsure"]},
                               "confidence": {"type": "number"},
                               "reason": {"type": "string"}}}}}}


def ask(llm: LLM, candidates: list[Candidate]) -> list[dict]:
    """One structured call per batch of same-kind questions, batches in parallel.
    Returns one decision row per candidate, in candidate order."""
    batches = []
    for kind in ("date", "labor", "dup"):
        items = [c for c in candidates if c.kind == kind]
        batches += [(kind, items[b:b + BATCH]) for b in range(0, len(items), BATCH)]

    def one(kind_batch):
        kind, batch = kind_batch
        user = (f"QUESTIONS ({kind}, options: {', '.join(OPTIONS[kind])} or unsure)\n"
                + json.dumps([c.prompt_item() for c in batch], ensure_ascii=False,
                             separators=(",", ":"), default=str))
        return llm.json(f"review-{kind}", SYSTEM, user,
                        _schema([c.id for c in batch], OPTIONS[kind]),
                        schema_name="review_decisions", reasoning_effort=REASONING_EFFORT)

    with ThreadPoolExecutor(max_workers=PARALLEL) as pool:
        outputs = list(pool.map(one, batches))

    rows = []
    for (kind, batch), out in zip(batches, outputs):
        got = {d["id"]: d for d in out["decisions"]}
        for c in batch:
            d = got.get(c.id, {"decision": "unsure", "confidence": 0.0,
                               "reason": "no answer returned"})
            rows.append({"id": c.id, "kind": kind, "sheet": c.sheet,
                         "row_index": c.row_index, "excel_row": c.row_index + 2,
                         "field": c.field, "decision": d["decision"],
                         "confidence": float(d["confidence"]), "reason": d["reason"],
                         "options": json.dumps(c.options, default=str)})
    return rows


def to_resolutions(candidates: list[Candidate], decisions: list[dict],
                   min_confidence: float = MIN_CONFIDENCE
                   ) -> tuple[list[Override], list[DropRow], list[dict]]:
    by_id = {c.id: c for c in candidates}
    overrides, drops = [], []
    for d in decisions:
        c = by_id[d["id"]]
        accepted = d["decision"] != "unsure" and d["confidence"] >= min_confidence
        d["accepted"] = accepted
        d["applied"] = ""
        if not accepted:
            continue
        if c.kind == "date" and d["decision"] == "swap":
            overrides.append(Override(c.sheet, c.row_index, c.field, c.options["swap"],
                                      f"day/month swapped: {d['reason']}", d["confidence"]))
            d["applied"] = c.options["swap"]
        elif c.kind == "labor" and d["decision"] == "minutes":
            overrides.append(Override(c.sheet, c.row_index, c.field, c.options["minutes"],
                                      f"labor booked in minutes: {d['reason']}",
                                      d["confidence"]))
            d["applied"] = c.options["minutes"]
        elif c.kind == "dup" and d["decision"] == "same":
            drops.append(DropRow(c.sheet, c.row_index, c.options["same"], d["reason"],
                                 d["confidence"]))
            d["applied"] = f"drop, keep excel_row {c.options['same'] + 2}"
    return overrides, drops, decisions
