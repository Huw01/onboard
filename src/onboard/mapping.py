"""Source -> target mapping: which legacy column feeds which target field, and how
legacy codes decode.

In milestone 4 the LLM mapper *proposes* this structure from a raw export. For
now the reference mapping for the bundled legacy export is written by hand and
stored in ``mappings/legacy_cmms.json``; the validator consumes either one.
"""

from __future__ import annotations

import json
from pathlib import Path

from onboard import corrupt as c


def reference_mapping() -> dict:
    """The correct mapping for the bundled legacy export."""
    inv = lambda d: {str(v): k for k, v in d.items()}  # noqa: E731
    value_maps = {
        "LOCATIONS": {"location_type": inv(c.LOC_TYPE_CODES)},
        "EQUIPMENT": {
            "asset_class": inv(c.CLASS_CODES),
            "criticality": {v: k for k, v in c.CRIT_CODES.items()},
            "meter_type": inv(c.METER_CODES),
            "status": inv(c.STATUS_CODES),
        },
        "PM_SCHED": {"trigger_type": inv(c.TRIGGER_CODES),
                     "frequency_unit": inv(c.UOM_CODES)},
        "WORK_ORDERS": {"status": inv(c.WO_STATUS_CODES)},
    }
    sheets = {}
    for sheet, (table, cols) in c.COLUMN_MAP.items():
        sheets[sheet] = {
            "table": table,
            "columns": {legacy: target for legacy, target, _ in cols},
            "value_maps": value_maps.get(sheet, {}),
        }
    return {"source_date_format": "%d/%m/%Y", "sheets": sheets}


def load_mapping(path: Path | None) -> dict:
    if path is None:
        return reference_mapping()
    return json.loads(Path(path).read_text())


def write_mapping(mapping: dict, path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(mapping, indent=2, ensure_ascii=False) + "\n")
