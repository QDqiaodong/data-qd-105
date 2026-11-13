"""Acquire, validate, overlay, store fragments and expose class areas."""
import hashlib
import json
import sqlite3
from pathlib import Path
from .source import load_input
from .overlay import overlay


def run(zones_path,parcels_path,output):
    zone_bytes,zones,_=load_input(zones_path,"zone",indexed=False)
    parcel_bytes,parcels,_=load_input(parcels_path,"parcel",indexed=False)
    fragments,summaries=overlay(zones,parcels)
    report={"schema_version":1,"coordinate_system":"LOCAL_METERS","zones":summaries,"fragment_count":len(fragments),
            "input_sha256":{"zones":hashlib.sha256(zone_bytes).hexdigest(),"parcels":hashlib.sha256(parcel_bytes).hexdigest()}}
    output=Path(output);output.mkdir(parents=True,exist_ok=True)
    with sqlite3.connect(output/"coverage.sqlite") as db:
        db.executescript("""CREATE TABLE IF NOT EXISTS fragments(fragment_id INTEGER PRIMARY KEY,zone_id TEXT,parcel_id TEXT,class TEXT,area_m2 REAL,coverage_kind TEXT,geometry_json TEXT);
        CREATE TABLE IF NOT EXISTS zone_summary(zone_id TEXT PRIMARY KEY,zone_area_m2 REAL,classified_area_m2 REAL,unclassified_area_m2 REAL,coverage_fraction REAL);
        CREATE TABLE IF NOT EXISTS input_hashes(name TEXT PRIMARY KEY,sha256 TEXT);""")
        for table in ["fragments","zone_summary","input_hashes"]:db.execute(f"DELETE FROM {table}")
        db.executemany("INSERT INTO fragments VALUES (?,?,?,?,?,?,?)",[(i,*[f["properties"][k] for k in ["zone_id","parcel_id","class","area_m2","coverage_kind"]],json.dumps(f["geometry"],sort_keys=True)) for i,f in enumerate(fragments,1)])
        db.executemany("INSERT INTO zone_summary VALUES (?,?,?,?,?)",[tuple(s[k] for k in ["zone_id","zone_area_m2","classified_area_m2","unclassified_area_m2","coverage_fraction"]) for s in summaries])
        db.executemany("INSERT INTO input_hashes VALUES (?,?)",report["input_sha256"].items())
    collection={"type":"FeatureCollection","coordinate_system":"LOCAL_METERS","features":fragments}
    (output/"fragments.geojson").write_text(json.dumps(collection,sort_keys=True,indent=2,allow_nan=False)+"\n")
    (output/"coverage.json").write_text(json.dumps(report,sort_keys=True,indent=2,allow_nan=False)+"\n")
    return report
