"""Seeded generator for a clean, target-schema CMMS dataset.

The site is a fictional open-pit mine ("Granite Ridge") plus the rail and
port terminal it ships through ("Port Halden"). Everything is synthetic.

Output: sites, locations, assets, pm_schedules, work_orders as CSV, all in
the target schema defined in :mod:`onboard.schema`, with zero integrity
violations. Same seed -> byte-identical files.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from faker import Faker

from onboard.schema import TABLES

DEFAULT_SEED = 42
AS_OF = date(2026, 9, 30)          # "extract date" of the dataset
WO_START = date(2024, 1, 1)        # work-order history window starts here
N_WORK_ORDERS = 5000

# --------------------------------------------------------------------------
# Sites and locations
# --------------------------------------------------------------------------

SITES = [
    ("GR", "Granite Ridge Mine", "America/Toronto"),
    ("PH", "Port Halden Rail & Port Terminal", "America/Halifax"),
]

# site -> [(area code, area name, [system names])]
AREAS: dict[str, list[tuple[str, str, list[str]]]] = {
    "GR": [
        ("PIT", "Open Pit", ["North Bench", "South Bench", "Haul Road"]),
        ("CRSH", "Primary Crushing", ["Crusher 1 Building", "Crusher 2 Building"]),
        ("CONV", "Overland Conveying", ["Line 1", "Line 2", "Line 3", "Transfer Towers"]),
        ("PLNT", "Concentrator Plant", ["Grinding", "Flotation", "Thickening", "Filtration"]),
        ("DWTR", "Dewatering", ["Pit Sumps", "Booster Station", "Tailings Return"]),
        ("PWR", "Power & BESS Yard", ["Main Substation", "BESS Yard A", "BESS Yard B"]),
        ("TSHOP", "Truck Shop", ["Service Bays", "Wash Bay", "Fuel Bay"]),
    ],
    "PH": [
        ("RAIL", "Rail Unloading", ["Car Dumper", "Dumper Conveyors"]),
        ("STKY", "Stockyard", ["Stacker Line", "Reclaim Line"]),
        ("SHIP", "Shiploading", ["Shiploader", "Berth Conveyors"]),
        ("PWR", "Substation & BESS", ["Port Substation", "BESS Yard"]),
    ],
}

# --------------------------------------------------------------------------
# Asset classes
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Child:
    suffix: str
    asset_class: str
    description: str
    meter_type: str
    count: tuple[int, int] = (1, 1)   # min/max instances


@dataclass(frozen=True)
class ParentClass:
    prefix: str
    asset_class: str
    description: str
    meter_type: str
    count: int
    areas: tuple[str, ...]           # "SITE-AREA" codes this class lives in
    criticality: tuple[int, ...]     # choices (1 = most critical)
    children: tuple[Child, ...] = field(default_factory=tuple)


MOTOR = lambda n=(1, 1): Child("MTR", "MOTOR", "Drive motor", "HOURS", n)  # noqa: E731

PARENT_CLASSES: list[ParentClass] = [
    ParentClass("CV", "CONVEYOR", "Belt conveyor", "HOURS", 62,
                ("GR-CONV", "GR-PLNT", "PH-RAIL", "PH-STKY", "PH-SHIP"), (1, 2, 2),
                (MOTOR((1, 2)), Child("GBX", "GEARBOX", "Drive gearbox", "NONE", (1, 2)),
                 Child("HPL", "PULLEY", "Head pulley", "NONE"),
                 Child("TPL", "PULLEY", "Tail pulley", "NONE"),
                 Child("BLT", "BELT", "Conveyor belt", "NONE"))),
    ParentClass("CR", "CRUSHER", "Crusher", "HOURS", 8, ("GR-CRSH", "GR-PLNT"), (1,),
                (MOTOR(), Child("LUB", "LUBE_SYSTEM", "Lube oil system", "HOURS"),
                 Child("HPU", "HPU", "Hydraulic power unit", "HOURS"))),
    ParentClass("HT", "HAUL_TRUCK", "Haul truck 220t", "KM", 44, ("GR-PIT", "GR-TSHOP"), (2,),
                (Child("ENG", "ENGINE", "Diesel engine", "HOURS"),
                 Child("TRN", "TRANSMISSION", "Transmission", "HOURS"),
                 Child("FDR", "FINAL_DRIVE", "Final drive", "NONE"))),
    ParentClass("PP", "PUMP", "Centrifugal pump", "HOURS", 118,
                ("GR-DWTR", "GR-PLNT", "PH-STKY"), (2, 3, 3), (MOTOR(),)),
    ParentClass("BS", "BESS", "Battery energy storage system 5 MWh", "MWH", 6,
                ("GR-PWR", "PH-PWR"), (1,),
                (Child("PCS", "PCS", "Power conversion system (inverter)", "NONE", (2, 2)),
                 Child("BAT", "BATTERY_CONTAINER", "Battery container", "CYCLES", (4, 4)),
                 Child("HVC", "HVAC", "Container HVAC unit", "HOURS", (4, 4)),
                 Child("FSS", "FIRE_SUPPRESSION", "Fire suppression system", "NONE"),
                 Child("TX", "TRANSFORMER", "Step-up transformer", "NONE"))),
    ParentClass("SC", "SCREEN", "Vibrating screen", "HOURS", 20, ("GR-PLNT", "GR-CRSH"), (2,),
                (MOTOR((1, 2)),)),
    ParentClass("FD", "FEEDER", "Apron feeder", "HOURS", 24,
                ("GR-CRSH", "GR-PLNT", "PH-RAIL"), (2, 3), (MOTOR(),)),
    ParentClass("AC", "COMPRESSOR", "Air compressor", "HOURS", 15,
                ("GR-PLNT", "GR-TSHOP", "PH-SHIP"), (3,), (MOTOR(),)),
    ParentClass("TX", "TRANSFORMER", "Distribution transformer", "NONE", 30,
                ("GR-PWR", "GR-PLNT", "GR-CRSH", "PH-PWR"), (2, 3)),
    ParentClass("FN", "FAN", "Ventilation / dust extraction fan", "HOURS", 30,
                ("GR-PLNT", "GR-CRSH", "PH-RAIL"), (3,), (MOTOR(),)),
    ParentClass("SR", "STACKER_RECLAIMER", "Stacker-reclaimer", "HOURS", 4, ("PH-STKY",), (1,),
                (MOTOR((2, 3)), Child("BLT", "BELT", "Boom belt", "NONE"))),
    ParentClass("MT", "MOTOR", "Standalone motor", "HOURS", 60,
                ("GR-PLNT", "GR-DWTR", "GR-CRSH", "PH-STKY", "PH-SHIP"), (3,)),
]

MANUFACTURERS = {
    "CONVEYOR": ["Northline Conveyors", "Bravura Bulk Systems"],
    "CRUSHER": ["Ironjaw Crushing", "Kessel Mineral Equipment"],
    "HAUL_TRUCK": ["Tundra Haul", "Basalt Heavy Machines"],
    "PUMP": ["Aquaforce Pumps", "Riverbend Pump Works", "Slurrytec"],
    "BESS": ["Voltreserve Energy", "Gridstack Storage"],
    "SCREEN": ["Shakewell Screens"],
    "FEEDER": ["Kessel Mineral Equipment", "Northline Conveyors"],
    "COMPRESSOR": ["Polar Air Systems"],
    "TRANSFORMER": ["Meridian Electric", "Halden Transformers"],
    "FAN": ["Cyclone Air Movement"],
    "STACKER_RECLAIMER": ["Bravura Bulk Systems"],
    "MOTOR": ["Meridian Electric", "Torquemax Motors", "Lakeshore Drives"],
    "GEARBOX": ["Gearline Industrial", "Torquemax Motors"],
    "PULLEY": ["Northline Conveyors"],
    "BELT": ["Flexrun Belting"],
    "LUBE_SYSTEM": ["Lubripro Systems"],
    "HPU": ["Hydrapower Units"],
    "ENGINE": ["Basalt Heavy Machines", "Diesel Dynamics"],
    "TRANSMISSION": ["Basalt Heavy Machines"],
    "FINAL_DRIVE": ["Basalt Heavy Machines"],
    "PCS": ["Voltreserve Energy", "Invertix Power"],
    "BATTERY_CONTAINER": ["Voltreserve Energy", "Gridstack Storage"],
    "HVAC": ["Coolbox Climate"],
    "FIRE_SUPPRESSION": ["Safeguard Fire Systems"],
}

# --------------------------------------------------------------------------
# PM rules: (description, trigger, value, unit, craft, est_hours, probability)
# --------------------------------------------------------------------------

PM_RULES: dict[str, list[tuple[str, str, int, str, str, float, float]]] = {
    "CONVEYOR": [("Belt and idler inspection", "TIME", 7, "DAYS", "MECH", 2.0, 1.0),
                 ("Pull-wire and guarding test", "TIME", 30, "DAYS", "ELEC", 1.0, 0.7)],
    "BELT": [("Belt thickness survey", "TIME", 90, "DAYS", "MECH", 3.0, 0.8)],
    "MOTOR": [("Motor vibration and thermography", "TIME", 90, "DAYS", "ELEC", 1.5, 0.8),
              ("Motor bearing regrease", "METER", 2000, "HOURS", "MECH", 1.0, 0.5)],
    "GEARBOX": [("Gearbox oil sample", "TIME", 90, "DAYS", "MECH", 0.5, 0.9),
                ("Gearbox oil change", "TIME", 365, "DAYS", "MECH", 4.0, 0.6)],
    "PULLEY": [("Pulley lagging inspection", "TIME", 180, "DAYS", "MECH", 0.5, 0.5)],
    "CRUSHER": [("Liner wear measurement", "TIME", 14, "DAYS", "MECH", 3.0, 1.0),
                ("Crusher major service", "METER", 2000, "HOURS", "MECH", 24.0, 1.0)],
    "LUBE_SYSTEM": [("Lube filter change", "TIME", 30, "DAYS", "MECH", 1.0, 1.0)],
    "HPU": [("Hydraulic oil sample", "TIME", 90, "DAYS", "MECH", 0.5, 1.0)],
    "HAUL_TRUCK": [("Weekly chassis inspection", "TIME", 7, "DAYS", "MECH", 2.0, 1.0),
                   ("Tyre inspection and rotation", "METER", 5000, "KM", "MECH", 4.0, 0.8)],
    "ENGINE": [("Engine service 250h", "METER", 250, "HOURS", "MECH", 4.0, 1.0),
               ("Engine service 500h", "METER", 500, "HOURS", "MECH", 8.0, 1.0),
               ("Engine service 1000h", "METER", 1000, "HOURS", "MECH", 12.0, 0.7)],
    "TRANSMISSION": [("Transmission oil sample", "METER", 500, "HOURS", "MECH", 0.5, 0.9)],
    "FINAL_DRIVE": [("Final drive oil change", "TIME", 180, "DAYS", "MECH", 3.0, 0.9)],
    "PUMP": [("Pump inspection", "TIME", 30, "DAYS", "MECH", 1.0, 0.9),
             ("Pump overhaul", "METER", 8000, "HOURS", "MECH", 16.0, 0.4)],
    "BESS": [("BESS monthly site inspection", "TIME", 30, "DAYS", "ELEC", 4.0, 1.0),
             ("BESS annual capacity test", "TIME", 365, "DAYS", "ELEC", 16.0, 1.0),
             ("BESS throughput health check", "METER", 500, "MWH", "ELEC", 6.0, 1.0)],
    "PCS": [("Inverter filter clean and IR scan", "TIME", 90, "DAYS", "ELEC", 3.0, 1.0)],
    "BATTERY_CONTAINER": [("Battery module SOH review", "METER", 300, "CYCLES", "ELEC", 2.0, 1.0),
                          ("Container thermal scan", "TIME", 30, "DAYS", "ELEC", 1.0, 0.8)],
    "HVAC": [("HVAC filter change", "TIME", 90, "DAYS", "MECH", 1.0, 1.0),
             ("HVAC service", "METER", 2000, "HOURS", "MECH", 3.0, 0.6)],
    "FIRE_SUPPRESSION": [("Fire suppression inspection", "TIME", 180, "DAYS", "INST", 2.0, 1.0)],
    "TRANSFORMER": [("Transformer visual inspection", "TIME", 30, "DAYS", "HV", 0.5, 0.8),
                    ("Transformer oil DGA sample", "TIME", 365, "DAYS", "HV", 1.0, 0.9)],
    "SCREEN": [("Screen deck inspection", "TIME", 7, "DAYS", "MECH", 1.5, 1.0)],
    "FEEDER": [("Feeder pan and chain inspection", "TIME", 14, "DAYS", "MECH", 2.0, 0.9)],
    "COMPRESSOR": [("Compressor service", "METER", 2000, "HOURS", "MECH", 4.0, 1.0),
                   ("Air receiver inspection", "TIME", 365, "DAYS", "INST", 2.0, 0.8)],
    "FAN": [("Fan vibration check", "TIME", 30, "DAYS", "MECH", 0.5, 0.8)],
    "STACKER_RECLAIMER": [("Slew bearing inspection", "TIME", 90, "DAYS", "MECH", 6.0, 1.0)],
}

# Rough daily meter accrual, used to weight how often a meter PM comes due.
METER_PER_DAY = {"DAYS": 1.0, "HOURS": 18.0, "KM": 350.0, "CYCLES": 1.2, "MWH": 6.0}

FAILURES: dict[str, list[str]] = {
    "CONVEYOR": ["Belt misalignment at tail", "Spillage at transfer chute", "Seized return idler",
                 "Belt rip detected", "Pull-wire trip, no fault found"],
    "BELT": ["Belt splice failure", "Edge damage on belt", "Belt rip repair"],
    "MOTOR": ["Motor tripping on overload", "High vibration DE bearing", "Motor winding fault",
              "Motor running hot", "Terminal box water ingress"],
    "GEARBOX": ["Gearbox oil leak at output seal", "Gearbox high temperature",
                "Metal in gearbox oil sample"],
    "PULLEY": ["Pulley lagging worn", "Pulley bearing noise"],
    "CRUSHER": ["Crusher blocked, uncrushable in chamber", "Liner bolt failure",
                "Tramp metal event", "Crusher high vibration"],
    "LUBE_SYSTEM": ["Lube pump low pressure alarm", "Lube oil cooler leak"],
    "HPU": ["HPU low pressure", "Hydraulic hose burst"],
    "HAUL_TRUCK": ["Flat tyre", "Payload system fault", "Dump body crack", "Brake fault",
                   "Air conditioning not working"],
    "ENGINE": ["Engine derate, low power", "Coolant leak", "Turbo failure", "High exhaust temp"],
    "TRANSMISSION": ["Transmission slipping", "Shift fault code"],
    "FINAL_DRIVE": ["Final drive oil leak", "Final drive noise"],
    "PUMP": ["Pump cavitation", "Mechanical seal leak", "Pump impeller wear, low flow",
             "Pump bearing failure"],
    "BESS": ["BESS availability below target", "BESS tripped offline", "SOC calibration drift",
             "Communication loss with BMS"],
    "PCS": ["Inverter IGBT fault", "PCS ground fault alarm", "Inverter fan failure"],
    "BATTERY_CONTAINER": ["Cell over-temperature alarm", "Module voltage imbalance",
                          "Rack contactor failure"],
    "HVAC": ["HVAC compressor failure", "HVAC high pressure trip", "Condensate leak"],
    "FIRE_SUPPRESSION": ["Fire panel fault", "Smoke detector fault"],
    "TRANSFORMER": ["Transformer oil leak", "Buchholz alarm", "High winding temperature"],
    "SCREEN": ["Broken screen panel", "Screen exciter bearing failure", "Cracked side plate"],
    "FEEDER": ["Feeder pan damage", "Chain stretch beyond limit"],
    "COMPRESSOR": ["Compressor high discharge temp", "Air leak on receiver drain"],
    "FAN": ["Fan imbalance", "Damper actuator failure"],
    "STACKER_RECLAIMER": ["Slew drive fault", "Boom collision sensor fault",
                          "Cable reel failure"],
}


def _rand_date(rng: np.random.Generator, start: date, end: date) -> date:
    return start + timedelta(days=int(rng.integers(0, (end - start).days + 1)))


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------

def build_sites() -> pd.DataFrame:
    return pd.DataFrame(SITES, columns=TABLES["sites"])


def build_locations() -> pd.DataFrame:
    rows = []
    for site_id, site_name, _ in SITES:
        rows.append((site_id, site_id, None, site_name, "SITE"))
        for area_code, area_name, systems in AREAS[site_id]:
            area_id = f"{site_id}-{area_code}"
            rows.append((area_id, site_id, site_id, area_name, "AREA"))
            for i, sys_name in enumerate(systems, start=1):
                rows.append((f"{area_id}-{i:02d}", site_id, area_id,
                             f"{area_name} - {sys_name}", "SYSTEM"))
    return pd.DataFrame(rows, columns=TABLES["locations"])


def build_assets(rng: np.random.Generator, fake: Faker, locations: pd.DataFrame) -> pd.DataFrame:
    systems = locations[locations.location_type == "SYSTEM"]
    systems_by_area = systems.groupby("parent_location_id")["location_id"].apply(list).to_dict()

    rows: list[dict] = []

    def make(asset_id, desc, cls, parent, loc, crit, install, meter):
        mfr_choices = MANUFACTURERS[cls]
        rows.append({
            "asset_id": asset_id,
            "description": desc,
            "asset_class": cls,
            "parent_asset_id": parent,
            "location_id": loc,
            "criticality": int(crit),
            "manufacturer": mfr_choices[int(rng.integers(len(mfr_choices)))],
            "model": fake.bothify("??-####").upper(),
            "serial_number": fake.bothify("SN########"),
            "install_date": install.isoformat(),
            "meter_type": meter,
            "status": "INACTIVE" if rng.random() < 0.03 else "ACTIVE",
        })

    for pc in PARENT_CLASSES:
        base = 1000 if pc.prefix != "MT" else 5000
        for n in range(1, pc.count + 1):
            parent_id = f"{pc.prefix}-{base + n}"
            area = pc.areas[int(rng.integers(len(pc.areas)))]
            loc_list = systems_by_area[area]
            loc = loc_list[int(rng.integers(len(loc_list)))]
            crit = pc.criticality[int(rng.integers(len(pc.criticality)))]
            install = _rand_date(rng, date(2008, 1, 1), date(2021, 12, 31))
            desc = f"{pc.description} {pc.prefix}-{base + n}"
            make(parent_id, desc, pc.asset_class, None, loc, crit, install, pc.meter_type)
            for ch in pc.children:
                k_max = int(rng.integers(ch.count[0], ch.count[1] + 1))
                for k in range(1, k_max + 1):
                    child_id = f"{parent_id}-{ch.suffix}{k}"
                    child_desc = f"{ch.description} {k}" if k_max > 1 else ch.description
                    child_desc = f"{child_desc} - {pc.prefix}-{base + n}"
                    # Components are original or replaced later (never after 2023).
                    child_install = install if rng.random() < 0.6 else _rand_date(
                        rng, install, date(2023, 12, 31))
                    child_crit = min(3, crit + int(rng.random() < 0.4))
                    make(child_id, child_desc, ch.asset_class, parent_id, loc, child_crit,
                         child_install, ch.meter_type)

    return pd.DataFrame(rows, columns=TABLES["assets"])


def build_pm_schedules(rng: np.random.Generator, assets: pd.DataFrame) -> pd.DataFrame:
    rows = []
    n = 0
    for a in assets.itertuples(index=False):
        if a.status != "ACTIVE":
            continue
        for desc, trigger, value, unit, craft, hrs, prob in PM_RULES.get(a.asset_class, []):
            if trigger == "METER" and unit != a.meter_type:
                continue
            if rng.random() > prob:
                continue
            n += 1
            rows.append({
                "pm_id": f"PM-{n:05d}",
                "asset_id": a.asset_id,
                "description": desc,
                "trigger_type": trigger,
                "frequency_value": value,
                "frequency_unit": unit,
                "craft": craft,
                "est_labor_hours": hrs,
                "last_done_date": None,
            })
    return pd.DataFrame(rows, columns=TABLES["pm_schedules"])


def build_work_orders(rng: np.random.Generator, assets: pd.DataFrame,
                      pms: pd.DataFrame, n_total: int = N_WORK_ORDERS) -> pd.DataFrame:
    window = (AS_OF - WO_START).days
    n_pm = int(n_total * 0.55)
    n_em = int(n_total * 0.07)
    n_cm = n_total - n_pm - n_em

    # PM work orders: PMs that come due more often get more work orders.
    period_days = pms.frequency_value / pms.frequency_unit.map(METER_PER_DAY)
    w = (1.0 / period_days).to_numpy()
    pm_idx = rng.choice(len(pms), size=n_pm, p=w / w.sum())

    active = assets[assets.status == "ACTIVE"].reset_index(drop=True)
    crit_w = active.criticality.map({1: 3.0, 2: 2.0, 3: 1.0}).to_numpy()
    crit_w = crit_w / crit_w.sum()

    raw = []
    for i in pm_idx:
        pm = pms.iloc[int(i)]
        raw.append(("PM", pm.asset_id, pm.pm_id, pm.description, float(pm.est_labor_hours)))
    for wo_type, count in (("CM", n_cm), ("EM", n_em)):
        for j in rng.choice(len(active), size=count, p=crit_w):
            a = active.iloc[int(j)]
            choices = FAILURES[a.asset_class]
            raw.append((wo_type, a.asset_id, None, choices[int(rng.integers(len(choices)))], None))

    reported = [WO_START + timedelta(days=int(d)) for d in rng.integers(0, window + 1, len(raw))]
    order = sorted(range(len(raw)), key=lambda k: (reported[k], k))

    rows = []
    for seq, k in enumerate(order, start=1):
        wo_type, asset_id, pm_id, desc, est = raw[k]
        rep = reported[k]
        if wo_type == "PM":
            sched = rep
            priority = 3
            hours = est * float(rng.lognormal(0, 0.25))
            lead = int(rng.integers(0, 10))
        elif wo_type == "CM":
            sched = rep + timedelta(days=int(rng.integers(1, 15)))
            priority = int(rng.choice([2, 3, 4], p=[0.3, 0.5, 0.2]))
            hours = float(rng.lognormal(1.2, 0.7))
            lead = (sched - rep).days + int(rng.integers(0, 10))
        else:  # EM
            sched = rep
            priority = 1
            hours = float(rng.lognormal(1.8, 0.6))
            lead = int(rng.integers(0, 3))
        comp = rep + timedelta(days=lead)

        if rng.random() < 0.03:
            status, comp_s, hours = "CANCELLED", None, 0.0
        elif comp > AS_OF:
            status = "IN_PROGRESS" if rng.random() < 0.4 else "OPEN"
            comp_s = None
            hours = hours * 0.3 if status == "IN_PROGRESS" else 0.0
        else:
            status, comp_s = "COMPLETED", comp.isoformat()

        rows.append({
            "wo_id": f"WO-{100000 + seq}",
            "asset_id": asset_id,
            "pm_id": pm_id,
            "wo_type": wo_type,
            "status": status,
            "priority": priority,
            "description": desc,
            "reported_date": rep.isoformat(),
            "scheduled_date": sched.isoformat(),
            "completed_date": comp_s,
            "labor_hours": round(round(hours * 2) / 2, 1),
        })
    return pd.DataFrame(rows, columns=TABLES["work_orders"])


def generate(seed: int = DEFAULT_SEED) -> dict[str, pd.DataFrame]:
    """Build the full clean dataset in memory."""
    rng = np.random.default_rng(seed)
    fake = Faker()
    fake.seed_instance(seed)

    sites = build_sites()
    locations = build_locations()
    assets = build_assets(rng, fake, locations)
    pms = build_pm_schedules(rng, assets)
    wos = build_work_orders(rng, assets, pms)

    # last_done_date = last completed PM work order for that PM (blank if none).
    done = wos[(wos.wo_type == "PM") & (wos.status == "COMPLETED")]
    last = done.groupby("pm_id")["completed_date"].max()
    pms["last_done_date"] = pms.pm_id.map(last)

    return {"sites": sites, "locations": locations, "assets": assets,
            "pm_schedules": pms, "work_orders": wos}


def write_tables(tables: dict[str, pd.DataFrame], out_dir: Path) -> list[Path]:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, df in tables.items():
        p = out_dir / f"{name}.csv"
        df.to_csv(p, index=False, lineterminator="\n")
        paths.append(p)
    return paths


def read_tables(in_dir: Path) -> dict[str, pd.DataFrame]:
    """Read clean CSVs back with stable dtypes (IDs and dates as strings)."""
    in_dir = Path(in_dir)
    out = {}
    for name in TABLES:
        df = pd.read_csv(in_dir / f"{name}.csv", dtype=str, keep_default_na=False)
        df = df.replace({"": None})
        for col in ("criticality", "frequency_value", "priority"):
            if col in df:
                df[col] = df[col].astype(int)
        for col in ("est_labor_hours", "labor_hours"):
            if col in df:
                df[col] = df[col].astype(float)
        out[name] = df
    return out
