"""Target (clean) schema for the CMMS worked example.

This is the shape a modern CMMS/EAM expects to load. The generator produces
data in exactly this shape; the corruptor turns it into a legacy export; the
pipeline (later milestones) has to get back here.
"""

from __future__ import annotations

TABLES: dict[str, list[str]] = {
    "sites": ["site_id", "site_name", "timezone"],
    "locations": ["location_id", "site_id", "parent_location_id", "name", "location_type"],
    "assets": [
        "asset_id",
        "description",
        "asset_class",
        "parent_asset_id",
        "location_id",
        "criticality",
        "manufacturer",
        "model",
        "serial_number",
        "install_date",
        "meter_type",
        "status",
    ],
    "pm_schedules": [
        "pm_id",
        "asset_id",
        "description",
        "trigger_type",
        "frequency_value",
        "frequency_unit",
        "craft",
        "est_labor_hours",
        "last_done_date",
    ],
    "work_orders": [
        "wo_id",
        "asset_id",
        "pm_id",
        "wo_type",
        "status",
        "priority",
        "description",
        "reported_date",
        "scheduled_date",
        "completed_date",
        "labor_hours",
    ],
}

PRIMARY_KEYS = {
    "sites": "site_id",
    "locations": "location_id",
    "assets": "asset_id",
    "pm_schedules": "pm_id",
    "work_orders": "wo_id",
}

DATE_COLUMNS = {
    "assets": ["install_date"],
    "pm_schedules": ["last_done_date"],
    "work_orders": ["reported_date", "scheduled_date", "completed_date"],
}

METER_TYPES = ["HOURS", "KM", "CYCLES", "MWH", "NONE"]
FREQUENCY_UNITS = ["DAYS", "HOURS", "KM", "CYCLES", "MWH"]
WO_TYPES = ["PM", "CM", "EM"]
WO_STATUSES = ["OPEN", "IN_PROGRESS", "COMPLETED", "CANCELLED"]
ASSET_STATUSES = ["ACTIVE", "INACTIVE"]
TRIGGER_TYPES = ["TIME", "METER"]
CRAFTS = ["MECH", "ELEC", "INST", "HV"]

# Fields that must be non-blank for a row to be loadable.
REQUIRED: dict[str, list[str]] = {
    "locations": ["location_id", "site_id", "name", "location_type"],
    "assets": ["asset_id", "description", "asset_class", "location_id", "criticality",
               "install_date", "meter_type", "status"],
    "pm_schedules": ["pm_id", "asset_id", "description", "trigger_type", "frequency_value",
                     "frequency_unit"],
    "work_orders": ["wo_id", "asset_id", "wo_type", "status", "priority", "description",
                    "reported_date", "scheduled_date"],
}

INT_COLUMNS = {"criticality", "frequency_value", "priority"}
FLOAT_COLUMNS = {"est_labor_hours", "labor_hours"}

# Load order: a table may only reference tables before it.
LOAD_ORDER = ["locations", "assets", "pm_schedules", "work_orders"]

ASSET_CLASSES = [
    "CONVEYOR", "CRUSHER", "HAUL_TRUCK", "PUMP", "BESS", "SCREEN", "FEEDER", "COMPRESSOR",
    "TRANSFORMER", "FAN", "STACKER_RECLAIMER", "MOTOR", "GEARBOX", "PULLEY", "BELT",
    "LUBE_SYSTEM", "HPU", "ENGINE", "TRANSMISSION", "FINAL_DRIVE", "PCS",
    "BATTERY_CONTAINER", "HVAC", "FIRE_SUPPRESSION",
]
LOCATION_TYPES = ["SITE", "AREA", "SYSTEM"]

# Fields whose values come from a fixed list: legacy codes must be translated.
CODED_FIELDS: dict[tuple[str, str], list] = {
    ("locations", "location_type"): LOCATION_TYPES,
    ("assets", "asset_class"): ASSET_CLASSES,
    ("assets", "criticality"): [1, 2, 3],
    ("assets", "meter_type"): METER_TYPES,
    ("assets", "status"): ASSET_STATUSES,
    ("pm_schedules", "trigger_type"): TRIGGER_TYPES,
    ("pm_schedules", "frequency_unit"): FREQUENCY_UNITS,
    ("work_orders", "wo_type"): WO_TYPES,
    ("work_orders", "status"): WO_STATUSES,
}

# One-line meaning of every target field (what the mapper is told).
FIELD_DOCS: dict[str, dict[str, str]] = {
    "locations": {
        "location_id": "unique location code, e.g. GR-CONV-04",
        "site_id": "code of the site the location belongs to",
        "parent_location_id": "location code of the parent location (blank for a site)",
        "name": "human-readable location name",
        "location_type": "level in the hierarchy: SITE, AREA or SYSTEM",
    },
    "assets": {
        "asset_id": "unique asset tag",
        "description": "asset description",
        "asset_class": "equipment class",
        "parent_asset_id": "asset tag of the parent asset (blank for top-level assets)",
        "location_id": "location code where the asset is installed",
        "criticality": "1 = most critical, 3 = least critical",
        "manufacturer": "manufacturer name",
        "model": "model number",
        "serial_number": "serial number",
        "install_date": "date the asset was installed",
        "meter_type": "what the asset's meter counts: HOURS, KM, CYCLES, MWH, or NONE",
        "status": "ACTIVE or INACTIVE",
    },
    "pm_schedules": {
        "pm_id": "unique preventive-maintenance schedule ID",
        "asset_id": "asset tag the PM applies to",
        "description": "PM task description",
        "trigger_type": "TIME (calendar) or METER (usage) based",
        "frequency_value": "interval between PMs, a number",
        "frequency_unit": "unit of the interval: DAYS, HOURS, KM, CYCLES or MWH",
        "craft": "trade that does the work (MECH, ELEC, INST, HV)",
        "est_labor_hours": "estimated labor hours per occurrence",
        "last_done_date": "date the PM was last completed",
    },
    "work_orders": {
        "wo_id": "unique work order number",
        "asset_id": "asset tag the work was done on",
        "pm_id": "PM schedule that generated the work order (blank if not a PM)",
        "wo_type": "PM (preventive), CM (corrective) or EM (emergency)",
        "status": "OPEN, IN_PROGRESS, COMPLETED or CANCELLED",
        "priority": "1 (highest) to 4",
        "description": "work description",
        "reported_date": "date the work order was raised",
        "scheduled_date": "date the work was scheduled for",
        "completed_date": "date the work was completed (blank if not completed)",
        "labor_hours": "actual labor hours booked",
    },
}
