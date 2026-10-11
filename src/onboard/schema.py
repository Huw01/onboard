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
