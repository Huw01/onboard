"""The clean dataset is deterministic and has zero integrity violations."""

from datetime import date, timedelta

import pytest

from onboard.generate import AS_OF, generate, write_tables
from onboard.schema import (ASSET_STATUSES, DATE_COLUMNS, FREQUENCY_UNITS, METER_TYPES,
                            PRIMARY_KEYS, TABLES, TRIGGER_TYPES, WO_STATUSES, WO_TYPES)

from conftest import DATA


def test_same_seed_is_byte_identical(tmp_path, clean_dir):
    write_tables(generate(42), tmp_path)
    for name in TABLES:
        assert (tmp_path / f"{name}.csv").read_bytes() == (clean_dir / f"{name}.csv").read_bytes()


def test_committed_clean_data_matches_generator(clean_dir):
    for name in TABLES:
        assert (DATA / "clean" / f"{name}.csv").read_bytes() == \
            (clean_dir / f"{name}.csv").read_bytes(), f"data/clean/{name}.csv is stale"


def test_different_seed_differs():
    a, b = generate(42), generate(7)
    assert not a["work_orders"].equals(b["work_orders"])


def test_sizes(clean):
    assert 1100 <= len(clean["assets"]) <= 1400
    assert len(clean["work_orders"]) == 5000
    assert len(clean["pm_schedules"]) > 1000


def test_columns_match_schema(clean):
    for name, cols in TABLES.items():
        assert list(clean[name].columns) == cols


@pytest.mark.parametrize("table", list(PRIMARY_KEYS))
def test_primary_keys_unique_and_present(clean, table):
    pk = clean[table][PRIMARY_KEYS[table]]
    assert pk.notna().all()
    assert pk.is_unique


def test_location_hierarchy(clean):
    loc = clean["locations"]
    ids = set(loc.location_id)
    assert set(loc.site_id) <= set(clean["sites"].site_id)
    parents = loc.parent_location_id.dropna()
    assert set(parents) <= ids
    assert loc[loc.location_type == "SITE"].parent_location_id.isna().all()
    assert loc[loc.location_type != "SITE"].parent_location_id.notna().all()


def test_asset_hierarchy_and_references(clean):
    a = clean["assets"]
    ids = set(a.asset_id)
    parents = a.parent_asset_id.dropna()
    assert set(parents) <= ids, "orphan parent"
    assert (a.parent_asset_id != a.asset_id).all()
    # parents are top level: no cycles possible
    assert a[a.asset_id.isin(parents)].parent_asset_id.isna().all()
    systems = set(clean["locations"].query("location_type == 'SYSTEM'").location_id)
    assert set(a.location_id) <= systems
    # child shares parent's location
    loc = a.set_index("asset_id").location_id
    kids = a.dropna(subset=["parent_asset_id"])
    assert (kids.location_id.values == loc.loc[kids.parent_asset_id].values).all()


def test_asset_domains(clean):
    a = clean["assets"]
    assert set(a.criticality) <= {1, 2, 3}
    assert set(a.meter_type) <= set(METER_TYPES)
    assert set(a.status) <= set(ASSET_STATUSES)
    for col in ("description", "asset_class", "manufacturer", "serial_number"):
        assert a[col].notna().all() and (a[col].str.strip() == a[col]).all()
    assert {"CONVEYOR", "CRUSHER", "HAUL_TRUCK", "PUMP", "MOTOR", "BESS"} <= set(a.asset_class)


def test_pm_schedules(clean):
    pm, a = clean["pm_schedules"], clean["assets"].set_index("asset_id")
    assert set(pm.asset_id) <= set(a.index)
    assert set(pm.trigger_type) <= set(TRIGGER_TYPES)
    assert set(pm.frequency_unit) <= set(FREQUENCY_UNITS)
    assert (pm.frequency_value > 0).all()
    assert ((pm.trigger_type == "TIME") == (pm.frequency_unit == "DAYS")).all()
    meter = pm[pm.trigger_type == "METER"]
    assert (meter.frequency_unit.values == a.loc[meter.asset_id, "meter_type"].values).all()


def test_work_orders(clean):
    wo, pm = clean["work_orders"], clean["pm_schedules"].set_index("pm_id")
    a = clean["assets"].set_index("asset_id")
    assert set(wo.asset_id) <= set(a.index)
    assert set(wo.wo_type) <= set(WO_TYPES)
    assert set(wo.status) <= set(WO_STATUSES)
    pmw = wo[wo.wo_type == "PM"]
    assert pmw.pm_id.notna().all()
    assert (pmw.asset_id.values == pm.loc[pmw.pm_id, "asset_id"].values).all()
    assert wo[wo.wo_type != "PM"].pm_id.isna().all()
    assert (wo.labor_hours >= 0).all()
    done = wo.status == "COMPLETED"
    assert wo[done].completed_date.notna().all()
    assert wo[~done].completed_date.isna().all()


def test_dates_valid_and_ordered(clean):
    for table, cols in DATE_COLUMNS.items():
        for col in cols:
            for v in clean[table][col].dropna():
                d = date.fromisoformat(v)
                # only open work orders may be scheduled after the extract date
                latest = AS_OF + timedelta(days=30) if col == "scheduled_date" else AS_OF
                assert date(2000, 1, 1) <= d <= latest, (table, col, v)
    wo = clean["work_orders"]
    late = wo[wo.scheduled_date > AS_OF.isoformat()]
    assert set(late.status) <= {"OPEN", "IN_PROGRESS", "CANCELLED"}
    wo = clean["work_orders"].dropna(subset=["completed_date"])
    assert (wo.completed_date >= wo.reported_date).all()
    wo = clean["work_orders"]
    assert (wo.scheduled_date >= wo.reported_date).all()
    inst = clean["assets"].set_index("asset_id").install_date
    assert (wo.reported_date.values >= inst.loc[wo.asset_id].values).all()
    kids = clean["assets"].dropna(subset=["parent_asset_id"])
    assert (kids.install_date.values >= inst.loc[kids.parent_asset_id].values).all()
