import json
import math
import sqlite3
import tempfile
import unittest
from pathlib import Path
from shapely.geometry import box,Polygon
from landcover_cube.source import load
from landcover_cube.overlay import overlay
from landcover_cube.pipeline import run

DATA=Path(__file__).resolve().parents[1]/"data"


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.zones=load(DATA/"zones.geojson","zone");self.parcels=load(DATA/"parcels.geojson","parcel")

    def test_overlay_area_and_uncovered_evidence(self):
        fragments,summary=overlay(self.zones,self.parcels)
        by_id={r["zone_id"]:r for r in summary}
        self.assertEqual(by_id["Z1"]["class_area_m2"],{"grass":50.0,"woodland":50.0})
        self.assertEqual(by_id["Z2"]["class_area_m2"],{"water":80.0,"woodland":10.0})
        self.assertEqual(by_id["Z2"]["unclassified_area_m2"],10)
        self.assertEqual(by_id["Z2"]["coverage_fraction"],.9)
        self.assertEqual(sum(f["properties"]["area_m2"] for f in fragments),200)
        self.assertEqual(len(fragments),5)

    def test_touching_boundary_does_not_create_area(self):
        fragments,summary=overlay([{"zone_id":"Z","geometry":box(0,0,1,1)}],[{"parcel_id":"P","class":"x","geometry":box(1,0,2,1)}])
        self.assertEqual(summary[0]["classified_area_m2"],0)
        self.assertEqual(len(fragments),1)
        self.assertEqual(fragments[0]["properties"]["coverage_kind"],"unclassified")

    def test_hole_and_empty_parcels_preserve_unknown_area(self):
        zone=[{"zone_id":"Z","geometry":box(0,0,10,10)}]
        polygon=Polygon([(0,0),(10,0),(10,10),(0,10)],holes=[[(2,2),(4,2),(4,4),(2,4)]])
        _,summary=overlay(zone,[{"parcel_id":"P","class":"forest","geometry":polygon}])
        self.assertEqual(summary[0]["unclassified_area_m2"],4)
        _,summary=overlay(zone,[])
        self.assertEqual(summary[0]["unclassified_area_m2"],100)

    def test_overlapping_inputs_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            document=json.loads((DATA/"parcels.geojson").read_text())
            duplicate=json.loads(json.dumps(document["features"][0]));duplicate["properties"]["parcel_id"]="overlap"
            document["features"].append(duplicate);path=Path(temp)/"bad.json";path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError,"overlapping"):load(path,"parcel")

    def test_bad_topology_crs_and_nonfinite_coordinates(self):
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)/"bad.json";base=json.loads((DATA/"zones.geojson").read_text())
            variants=[]
            wrong=json.loads(json.dumps(base));wrong["coordinate_system"]="EPSG:4326";variants.append(wrong)
            crossed=json.loads(json.dumps(base));crossed["features"][0]["geometry"]["coordinates"]=[[[0,0],[10,10],[0,10],[10,0],[0,0]]];variants.append(crossed)
            nan=json.loads(json.dumps(base));nan["features"][0]["geometry"]["coordinates"][0][0][0]=float("nan");variants.append(nan)
            for document in variants:
                path.write_text(json.dumps(document))
                with self.assertRaises(ValueError):load(path,"zone")

    def test_unclosed_or_short_rings_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            document=json.loads((DATA/"zones.geojson").read_text())
            ring=document["features"][0]["geometry"]["coordinates"][0]
            for bad in [ring[:-1],ring[:2]+[ring[0]]]:
                document["features"][0]["geometry"]["coordinates"]=[bad]
                path=Path(temp)/"open.json";path.write_text(json.dumps(document))
                with self.assertRaises(ValueError):load(path,"zone")

    def test_multi_polygon_and_real_unclassified_class(self):
        from shapely.geometry import MultiPolygon
        parcels=[{"parcel_id":"M","class":"unclassified","geometry":MultiPolygon([box(0,0,2,2),box(3,0,5,2)])}]
        fragments,summary=overlay([{"zone_id":"Z","geometry":box(0,0,5,5)}],parcels)
        self.assertEqual(summary[0]["class_area_m2"]["unclassified"],8)
        self.assertEqual(sum(f["properties"]["coverage_kind"]=="classified" for f in fragments),1)

    def test_reports_replay_and_queryable_storage(self):
        with tempfile.TemporaryDirectory() as temp:
            run(DATA/"zones.geojson",DATA/"parcels.geojson",temp)
            before=(Path(temp)/"coverage.json").read_bytes();geometry=(Path(temp)/"fragments.geojson").read_bytes()
            run(DATA/"zones.geojson",DATA/"parcels.geojson",temp)
            self.assertEqual(before,(Path(temp)/"coverage.json").read_bytes())
            self.assertEqual(geometry,(Path(temp)/"fragments.geojson").read_bytes())
            with sqlite3.connect(Path(temp)/"coverage.sqlite") as db:
                self.assertEqual(db.execute("SELECT sum(area_m2) FROM fragments").fetchone()[0],200)
                self.assertEqual(db.execute("SELECT sum(area_m2) FROM fragments WHERE coverage_kind='unclassified'").fetchone()[0],10)


if __name__=="__main__":unittest.main()
