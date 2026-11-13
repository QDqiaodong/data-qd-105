"""Tiled overlay: synthetic cases, instrumented scope, staging and failures."""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from shapely import union_all
from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape

from landcover_cube.overlay import polygonal
from landcover_cube.pipeline import run
from landcover_cube.tiled import parse_tile_size, tiled

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PYTHON = sys.executable


def collection(features):
    return {
        "type": "FeatureCollection",
        "coordinate_system": "LOCAL_METERS",
        "features": features,
    }


def zone_feature(zone_id, geometry):
    return {
        "type": "Feature",
        "properties": {"zone_id": zone_id},
        "geometry": mapping(geometry),
    }


def parcel_feature(parcel_id, klass, geometry):
    return {
        "type": "Feature",
        "properties": {"parcel_id": parcel_id, "class": klass},
        "geometry": mapping(geometry),
    }


class Workspace:
    """Write synthetic input documents and hold a tiled output directory."""

    def __init__(self, temp, zones, parcels, name="case"):
        self.root = Path(temp)
        self.zones_path = self.root / f"{name}_zones.geojson"
        self.parcels_path = self.root / f"{name}_parcels.geojson"
        self.zones_path.write_text(json.dumps(collection(zones)), encoding="utf-8")
        self.parcels_path.write_text(json.dumps(collection(parcels)), encoding="utf-8")

    def build(self, tile_size, output_name="out", observer=None):
        self.output = self.root / output_name
        report = tiled(
            self.zones_path,
            self.parcels_path,
            tile_size,
            self.output,
            observer=observer,
        )
        return report

    def facts(self):
        document = json.loads((self.output / "fragments.geojson").read_text())
        return document["features"]

    def diagnostics(self):
        return json.loads((self.output / "build_diagnostics.json").read_text())

    def report(self):
        return json.loads((self.output / "coverage.json").read_text())


def keyed_facts(features):
    """Canonical business-key map with shapely geometry and area."""
    result = {}
    for feature in features:
        props = feature["properties"]
        if props["coverage_kind"] == "unclassified":
            key = (props["zone_id"], "unclassified", None)
        else:
            key = (props["zone_id"], "classified", props["parcel_id"])
        result[key] = {
            "class": props["class"],
            "area": props["area_m2"],
            "geometry": shape(feature["geometry"]),
        }
    return result


def tolerance(zone_area):
    return max(1e-8, 1e-9 * zone_area)


class BundledFixtureTests(unittest.TestCase):
    def test_synthetic_tiled_demo_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            report = tiled(
                DATA / "tiled_zones.geojson",
                DATA / "tiled_parcels.geojson",
                4,
                temp / "demo",
            )
            features = json.loads((temp / "demo/fragments.geojson").read_text())["features"]
            facts = keyed_facts(features)
            self.assertEqual(len(facts), 3)
            self.assertEqual(facts[("TZ", "classified", "TP")]["area"], 100.0)
            self.assertEqual(facts[("TZ", "classified", "TQ")]["area"], 50.0)
            self.assertEqual(facts[("TZ", "unclassified", None)]["area"], 50.0)
            self.assertEqual(report["zones"][0]["zone_area_m2"], 200.0)
            self.assertEqual(
                facts[("TZ", "unclassified", None)]["geometry"].bounds,
                (10.0, -5.0, 15.0, 5.0),
            )
            self.assertEqual(
                facts[("TZ", "classified", "TP")]["geometry"].bounds,
                (-5.0, -5.0, 5.0, 5.0),
            )

    def test_tile_size_three_keeps_five_final_fragments_and_200m2(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                json.loads((DATA / "zones.geojson").read_text())["features"],
                json.loads((DATA / "parcels.geojson").read_text())["features"],
                "bundled",
            )
            report = workspace.build(3)
            features = workspace.facts()
            self.assertEqual(len(features), 5)
            self.assertEqual(sum(f["properties"]["area_m2"] for f in features), 200)
            by_id = {s["zone_id"]: s for s in report["zones"]}
            self.assertEqual(by_id["Z1"]["class_area_m2"], {"grass": 50.0, "woodland": 50.0})
            self.assertEqual(
                by_id["Z2"]["class_area_m2"], {"water": 80.0, "woodland": 10.0}
            )
            self.assertEqual(by_id["Z2"]["unclassified_area_m2"], 10)
            # Staged tile pieces are far more numerous than final facts.
            diagnostics = workspace.diagnostics()
            self.assertGreater(diagnostics["totals"]["staged_pieces"], 5)
            self.assertEqual(diagnostics["totals"]["final_facts"], 5)

    def test_tiled_matches_full_overlay_on_bundled_fixture(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            tiled(DATA / "zones.geojson", DATA / "parcels.geojson", 3, temp / "tiled")
            run(DATA / "zones.geojson", DATA / "parcels.geojson", temp / "full")
            tiled_facts = keyed_facts(
                json.loads((temp / "tiled/fragments.geojson").read_text())["features"]
            )
            full_facts = keyed_facts(
                json.loads((temp / "full/fragments.geojson").read_text())["features"]
            )
            self.assertEqual(set(tiled_facts), set(full_facts))
            for key in full_facts:
                self.assertAlmostEqual(
                    tiled_facts[key]["area"], full_facts[key]["area"], places=8
                )
                self.assertLessEqual(
                    polygonal(
                        tiled_facts[key]["geometry"].symmetric_difference(
                            full_facts[key]["geometry"]
                        )
                    ).area,
                    tolerance(full_facts[key]["area"]),
                )


class NegativeCoordinateTests(unittest.TestCase):
    def test_negative_unaligned_blocks_give_exact_rectangles(self):
        zones = [zone_feature("Z", box(-5, -5, 15, 5))]
        parcels = [
            parcel_feature("P", "woodland", box(-5, -5, 5, 5)),
            parcel_feature("Q", "grass", box(5, -5, 10, 5)),
        ]
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, zones, parcels)
            report = workspace.build(4)
            facts = workspace.facts()
            self.assertEqual(len(facts), 3)
            expected = {
                "P": (100.0, box(-5, -5, 5, 5)),
                "Q": (50.0, box(5, -5, 10, 5)),
                None: (50.0, box(10, -5, 15, 5)),
            }
            seen = {}
            for feature in facts:
                props = feature["properties"]
                geom = shape(feature["geometry"])
                seen[props["parcel_id"]] = (props["area_m2"], geom)
                self.assertEqual(props["zone_id"], "Z")
                self.assertAlmostEqual(props["area_m2"], expected[props["parcel_id"]][0])
                self.assertEqual(geom.bounds, expected[props["parcel_id"]][1].bounds)
                self.assertLessEqual(
                    polygonal(geom.symmetric_difference(expected[props["parcel_id"]][1])).area,
                    1e-9,
                )
            self.assertEqual(set(seen), {"P", "Q", None})
            self.assertEqual(report["zones"][0]["zone_area_m2"], 200.0)
            self.assertEqual(report["zones"][0]["classified_area_m2"], 150.0)
            diagnostics = workspace.diagnostics()
            block_items = diagnostics["blocks"]["items"]
            self.assertTrue(any(i < 0 and j < 0 for i, j in ((b["i"], b["j"]) for b in block_items)))


class GeometryMergeTests(unittest.TestCase):
    def test_hole_crossing_blocks_and_disconnected_parcel_preserved(self):
        zone = box(0, 0, 10, 10)
        holed = Polygon(
            [(0, 0), (10, 0), (10, 10), (0, 10)],
            holes=[[(3, 3), (7, 3), (7, 7), (3, 7)]],
        )
        islands = MultiPolygon([box(3, 3, 4, 4), box(6, 6, 7, 7)])
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", zone)],
                [
                    parcel_feature("P", "woodland", holed),
                    parcel_feature("Q", "grass", islands),
                ],
            )
            report = workspace.build(4)
            facts = keyed_facts(workspace.facts())
            self.assertEqual(len(facts), 3)
            self.assertEqual(facts[("Z", "classified", "P")]["class"], "woodland")
            self.assertEqual(facts[("Z", "classified", "P")]["area"], 84.0)
            holed_geometry = facts[("Z", "classified", "P")]["geometry"]
            self.assertEqual(len(holed_geometry.interiors), 1)
            self.assertAlmostEqual(Polygon(holed_geometry.interiors[0]).area, 16.0)
            self.assertEqual(facts[("Z", "classified", "Q")]["area"], 2.0)
            islands_geometry = facts[("Z", "classified", "Q")]["geometry"]
            self.assertEqual(islands_geometry.geom_type, "MultiPolygon")
            self.assertEqual(len(islands_geometry.geoms), 2)
            self.assertEqual(facts[("Z", "unclassified", None)]["area"], 14.0)
            self.assertEqual(report["zones"][0]["unclassified_area_m2"], 14.0)

    def test_zone_with_hole_and_multiple_components_across_blocks(self):
        zone_geometry = MultiPolygon(
            [
                Polygon(
                    [(0, 0), (6, 0), (6, 6), (0, 6)],
                    holes=[[(2, 2), (4, 2), (4, 4), (2, 4)]],
                ),
                box(10, 0, 12, 4),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", zone_geometry)],
                [parcel_feature("P", "woodland", box(-1, -1, 7, 7))],
            )
            report = workspace.build(2)
            facts = keyed_facts(workspace.facts())
            # Zone hole contributes uncovered area; the second component too.
            expected_zone_area = 36 - 4 + 8
            self.assertEqual(report["zones"][0]["zone_area_m2"], expected_zone_area)
            classified = facts[("Z", "classified", "P")]
            self.assertEqual(classified["area"], 36 - 4)  # full first component filled
            self.assertEqual(facts[("Z", "unclassified", None)]["area"], 8)
            missing_geometry = facts[("Z", "unclassified", None)]["geometry"]
            self.assertEqual(missing_geometry.bounds, (10.0, 0.0, 12.0, 4.0))
            # The block containing only the second component is still processed.
            block_items = workspace.diagnostics()["blocks"]["items"]
            self.assertTrue(any(b["i"] == 5 for b in block_items))

    def test_streaming_observer_counts_match_written_diagnostics(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 4, 4))],
                [parcel_feature("P", "woodland", box(0, 0, 4, 4))],
            )
            events = []
            workspace.build(2, observer=events.append)
            diagnostics = workspace.diagnostics()["totals"]
            observed = {
                "classified_clips": sum(e["event"] == "classified_clip" for e in events),
                "unclassified_clips": sum(e["event"] == "unclassified_clip" for e in events),
                "staged_pieces": sum(e["event"] == "staged_piece" for e in events),
            }
            for key, value in observed.items():
                self.assertEqual(diagnostics[key], value)

    def test_parcel_spanning_two_zones_keeps_zone_parcel_granularity(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [
                    zone_feature("A", box(0, 0, 2, 2)),
                    zone_feature("B", box(2, 0, 4, 2)),
                ],
                [parcel_feature("X", "unclassified", box(0, 0, 4, 2))],
            )
            report = workspace.build("0.5")
            facts = keyed_facts(workspace.facts())
            self.assertEqual(
                set(facts),
                {("A", "classified", "X"), ("B", "classified", "X")},
            )
            self.assertEqual(facts[("A", "classified", "X")]["area"], 4.0)
            self.assertEqual(facts[("B", "classified", "X")]["area"], 4.0)
            self.assertEqual(facts[("A", "classified", "X")]["class"], "unclassified")
            self.assertEqual(report["zones"][0]["class_area_m2"], {"unclassified": 4.0})


class BoundaryAndSliverTests(unittest.TestCase):
    def test_shapes_exactly_on_grid_lines_and_touching_points(self):
        zones = [zone_feature("A", box(0, 0, 4, 4))]
        parcels = [
            parcel_feature("P", "x", box(0, 0, 2, 4)),
            parcel_feature("Q", "y", box(2, 0, 4, 4)),
            parcel_feature("T", "z", Polygon([(4, 4), (4.5, 4), (4.5, 4.5), (4, 4.5)])),
        ]
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, zones, parcels)
            report = workspace.build(2)
            facts = keyed_facts(workspace.facts())
            self.assertEqual(
                set(facts), {("A", "classified", "P"), ("A", "classified", "Q")}
            )
            self.assertEqual(facts[("A", "classified", "P")]["area"], 8.0)
            self.assertEqual(facts[("A", "classified", "Q")]["area"], 8.0)
            self.assertNotIn(("A", "classified", "T"), facts)
            self.assertEqual(report["zones"][0]["unclassified_area_m2"], 0)
            self.assertEqual(report["zones"][0]["classified_area_m2"], 16.0)

    def test_empty_parcel_collection_leaves_whole_zone_unclassified(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, [zone_feature("Z", box(0, 0, 2, 2))], [])
            report = workspace.build(1)
            facts = workspace.facts()
            self.assertEqual(len(facts), 1)
            props = facts[0]["properties"]
            self.assertEqual(props["coverage_kind"], "unclassified")
            self.assertIsNone(props["parcel_id"])
            self.assertIsNone(props["class"])
            self.assertEqual(props["area_m2"], 4.0)
            self.assertEqual(report["zones"][0]["unclassified_area_m2"], 4.0)

    def test_real_class_named_unclassified_stays_classified(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 3, 1))],
                [parcel_feature("P", "unclassified", box(0, 0, 2, 1))],
            )
            report = workspace.build(1)
            facts = workspace.facts()
            classified = [f for f in facts if f["properties"]["coverage_kind"] == "classified"]
            unclassified = [
                f for f in facts if f["properties"]["coverage_kind"] == "unclassified"
            ]
            self.assertEqual(len(classified), 1)
            self.assertEqual(classified[0]["properties"]["class"], "unclassified")
            self.assertEqual(classified[0]["properties"]["area_m2"], 2.0)
            self.assertEqual(len(unclassified), 1)
            self.assertIsNone(unclassified[0]["properties"]["class"])
            self.assertEqual(report["zones"][0]["class_area_m2"], {"unclassified": 2.0})
            self.assertEqual(report["zones"][0]["unclassified_area_m2"], 1.0)

    def test_sub_tolerance_sliver_straddling_blocks_is_not_filtered(self):
        epsilon = 0.5e-9
        zone = box(0, 0, 1, 1)
        split = MultiPolygon(
            [
                Polygon([(0, 0), (0.5 - epsilon, 0), (0.5 - epsilon, 1), (0, 1)]),
                Polygon([(0.5 + epsilon, 0), (1, 0), (1, 1), (0.5 + epsilon, 1)]),
            ]
        )
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", zone)],
                [parcel_feature("P", "woodland", split)],
            )
            report = workspace.build(0.5)
            facts = workspace.facts()
            self.assertEqual(len(facts), 2)
            unclassified = [f for f in facts if f["properties"]["coverage_kind"] == "unclassified"]
            self.assertEqual(len(unclassified), 1)
            gap_area = unclassified[0]["properties"]["area_m2"]
            self.assertGreater(gap_area, 0)
            self.assertLess(gap_area, 1e-8)
            self.assertEqual(report["zones"][0]["unclassified_area_m2"], gap_area)

    def test_tiny_but_positive_parcel_crossing_blocks_is_kept(self):
        # 1m x 1e-9m strip, area 1e-9 m2, deliberately below abs_tol 1e-8.
        strip = box(0, 0, 1, 1e-9)
        self.assertGreater(strip.area, 0)
        self.assertLess(strip.area, 1e-8)
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 1, 1))],
                [parcel_feature("P", "woodland", strip)],
            )
            report = workspace.build(0.25)
            facts = workspace.facts()
            classified = [f for f in facts if f["properties"]["coverage_kind"] == "classified"]
            self.assertEqual(len(classified), 1)
            geometry = shape(classified[0]["geometry"])
            self.assertGreater(geometry.area, 0)
            self.assertLess(geometry.area, 1e-8)
            self.assertAlmostEqual(geometry.area, strip.area, places=15)
            self.assertEqual(classified[0]["properties"]["area_m2"], geometry.area)
            diagnostics = workspace.diagnostics()
            # The strip crosses x=0.25,0.5,0.75, so several pieces feed one fact.
            self.assertGreater(diagnostics["totals"]["staged_pieces"], 1)
            self.assertEqual(report["zones"][0]["classified_area_m2"], geometry.area)

    def test_same_class_different_parcel_ids_are_not_merged(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 2, 2))],
                [
                    parcel_feature("A", "woodland", box(0, 0, 1, 1)),
                    parcel_feature("B", "woodland", box(1, 1, 2, 2)),
                ],
            )
            report = workspace.build(1)
            facts = keyed_facts(workspace.facts())
            self.assertEqual(
                set(facts),
                {
                    ("Z", "classified", "A"),
                    ("Z", "classified", "B"),
                    ("Z", "unclassified", None),
                },
            )
            self.assertEqual(report["zones"][0]["class_area_m2"], {"woodland": 2.0})


class ValidationTests(unittest.TestCase):
    def assert_rejected(self, zones, parcels, tile_size=2, message=None):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, zones, parcels, "bad")
            with self.assertRaises(ValueError) as caught:
                workspace.build(tile_size)
            self.assertFalse((workspace.root / "out").exists())
            if message:
                self.assertIn(message, str(caught.exception))
            return str(caught.exception)

    def test_invalid_ring_and_duplicate_identity(self):
        self.assert_rejected(
            [zone_feature("Z", box(0, 0, 2, 2)), zone_feature("Z", box(3, 0, 4, 2))],
            [],
            message="duplicate",
        )
        self.assert_rejected(
            [
                zone_feature(
                    "Z",
                    Polygon([(0, 0), (2, 2), (2, 0), (0, 2), (0, 0)]),
                )
            ],
            [],
            message="invalid",
        )

    def test_wrong_crs_and_nonfinite_coordinates(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "zones.geojson"
            document = collection([zone_feature("Z", box(0, 0, 2, 2))])
            document["coordinate_system"] = "EPSG:4326"
            path.write_text(json.dumps(document))
            parcels = Path(temp) / "parcels.geojson"
            parcels.write_text(json.dumps(collection([])))
            with self.assertRaisesRegex(ValueError, "LOCAL_METERS"):
                tiled(path, parcels, 1, Path(temp) / "out")
            self.assertFalse((Path(temp) / "out").exists())

            document["coordinate_system"] = "LOCAL_METERS"
            coordinates = json.loads(
                json.dumps(document["features"][0]["geometry"]["coordinates"])
            )
            coordinates[0][0][0] = float("nan")
            document["features"][0]["geometry"]["coordinates"] = coordinates
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, "finite"):
                tiled(path, parcels, 1, Path(temp) / "out2")
            self.assertFalse((Path(temp) / "out2").exists())

    def test_overlapping_parcels_outside_zones_are_rejected(self):
        # Both parcels lie completely outside the zone but overlap each other.
        self.assert_rejected(
            [zone_feature("Z", box(0, 0, 2, 2))],
            [
                parcel_feature("P", "a", box(10, 10, 12, 12)),
                parcel_feature("Q", "b", box(10.5, 10.5, 12.5, 12.5)),
            ],
            message="overlapping parcel",
        )

    def test_cross_block_candidate_pair_is_checked_once(self):
        with tempfile.TemporaryDirectory() as temp:
            zones = [zone_feature("Z", box(-10, -10, 10, 10))]
            parcels = [
                parcel_feature("A", "x", box(-9, -9, -1, 1)),
                parcel_feature("B", "y", box(-1, -2, 9, 8)),
            ]
            events = []
            workspace = Workspace(temp, zones, parcels)
            workspace.build(1, observer=events.append)
            checks = [
                event
                for event in events
                if event["event"] == "exact_topology_check" and event["kind"] == "parcel"
            ]
            self.assertEqual(len(checks), 1)
            # The shared edge has zero area, so this is accepted, and the
            # candidate pair is checked exactly once despite spanning many blocks.
            self.assertEqual(workspace.report()["zones"][0]["zone_id"], "Z")


class TileSizeTests(unittest.TestCase):
    def test_valid_and_invalid_tile_sizes(self):
        for value in ["2", 2, Decimal("2"), "2.5", "1e3", ".5"]:
            self.assertGreater(parse_tile_size(value), 0)
        for value in [
            True,
            False,
            "0",
            -1,
            "-2.5",
            "1000001",
            "nan",
            "inf",
            "1x",
            " 2",
            "1_0",
            "",
            None,
            float("inf"),
            float("nan"),
        ]:
            with self.assertRaises(ValueError):
                parse_tile_size(value)
        self.assertEqual(parse_tile_size(Decimal("1000000")), Decimal("1000000"))

    def test_absurd_tile_size_rejected_before_allocation(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, [zone_feature("Z", box(-1, -1, 1, 1))], [])
            with self.assertRaisesRegex(ValueError, "too many blocks"):
                workspace.build("1e-100")
            self.assertFalse((workspace.root / "out").exists())

    def test_three_tile_sizes_share_business_semantics(self):
        zone = box(-5, -5, 15, 5)
        parcels = [
            parcel_feature("P", "woodland", box(-5, -5, 5, 5)),
            parcel_feature("Q", "grass", box(5, -5, 10, 5)),
        ]
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, [zone_feature("Z", zone)], parcels)
            reference = None
            for index, size in enumerate(["3", "4", str(2.5)]):
                report = workspace.build(size, f"out{index}")
                facts = keyed_facts(workspace.facts())
                keys = set(facts)
                classes = {k: facts[k]["class"] for k in facts}
                areas = {k: round(facts[k]["area"], 9) for k in facts}
                summaries = {
                    s["zone_id"]: (
                        round(s["zone_area_m2"], 9),
                        round(s["classified_area_m2"], 9),
                        round(s["unclassified_area_m2"], 9),
                    )
                    for s in report["zones"]
                }
                semantics = (keys, classes, areas, summaries)
                if reference is None:
                    reference = semantics
                else:
                    self.assertEqual(semantics, reference)


class CrossEntryConsistencyTests(unittest.TestCase):
    def test_tiled_matches_full_entry_on_nontrivial_dataset(self):
        zones = [
            zone_feature("ZA", box(0, 0, 6, 6)),
            zone_feature("ZB", box(6, 0, 12, 6)),
            zone_feature("ZC", Polygon([(12, 0), (15, 0), (15, 6), (12, 6)], holes=[[(13, 2), (14, 2), (14, 4), (13, 4)]])),
        ]
        parcels = [
            parcel_feature("P1", "woodland", box(-1, -1, 3, 3)),
            parcel_feature(
                "P2", "grass", MultiPolygon([box(3, 3, 7, 7), box(9, 4, 11, 6)])
            ),
            parcel_feature("P3", "water", box(5, -1, 13, 2)),
            parcel_feature("P4", "unclassified", box(11, 4, 14, 6)),
        ]
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, zones, parcels, "mixed")
            run(workspace.zones_path, workspace.parcels_path, workspace.root / "full")
            full_features = json.loads(
                (workspace.root / "full/fragments.geojson").read_text()
            )["features"]
            full_facts = keyed_facts(full_features)
            full_summaries = {
                s["zone_id"]: s
                for s in json.loads(
                    (workspace.root / "full/coverage.json").read_text()
                )["zones"]
            }
            for index, size in enumerate(["1", "2.5", "4"]):
                workspace.build(size, f"tiled{index}")
                facts = keyed_facts(workspace.facts())
                self.assertEqual(set(facts), set(full_facts))
                zone_keys = {}
                for key in full_facts:
                    zone_keys.setdefault(key[0], []).append(key)
                for zone_id in {zone["properties"]["zone_id"] for zone in zones}:
                    keys = zone_keys.get(zone_id, [])
                    union = union_all(
                        [full_facts[key]["geometry"] for key in keys]
                    ) if keys else None
                    tiled_union = union_all(
                        [facts[key]["geometry"] for key in keys]
                    ) if keys else None
                    zone_shape = shape(next(z for z in zones if z["properties"]["zone_id"] == zone_id)["geometry"])
                    if union is not None:
                        self.assertLessEqual(
                            polygonal(union.symmetric_difference(tiled_union)).area,
                            tolerance(zone_shape.area),
                        )
                        self.assertLessEqual(
                            polygonal(tiled_union.symmetric_difference(zone_shape)).area,
                            tolerance(zone_shape.area),
                        )
                    else:
                        self.assertAlmostEqual(zone_shape.area, 0, places=6)
                classified = sum(
                    full_facts[key]["area"] for key in full_facts
                    if key[1] == "classified"
                )
                total = sum(fact["area"] for fact in full_facts.values())
                self.assertAlmostEqual(
                    sum(fact["area"] for fact in facts.values()), total, places=8
                )
                self.assertAlmostEqual(
                    sum(facts[key]["area"] for key in facts if key[1] == "classified"),
                    classified,
                    places=9,
                )
                for zone_id, summary in full_summaries.items():
                    tiled_summary = next(
                        s for s in workspace.report()["zones"] if s["zone_id"] == zone_id
                    )
                    self.assertAlmostEqual(
                        tiled_summary["classified_area_m2"],
                        summary["classified_area_m2"],
                        places=9,
                    )
                    self.assertAlmostEqual(
                        tiled_summary["unclassified_area_m2"],
                        summary["unclassified_area_m2"],
                        places=9,
                    )


class ScalingAndInstrumentationTests(unittest.TestCase):
    def test_two_hundred_isolated_zones_avoid_cartesian_exact_work(self):
        zones = []
        parcels = []
        for k in range(200):
            x = (k % 20) * 40
            y = (k // 20) * 40
            geometry = box(x, y, x + 10, y + 10)
            zones.append(zone_feature(f"Z{k:03d}", geometry))
            parcels.append(parcel_feature(f"P{k:03d}", "woodland", geometry))
        events = []
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, zones, parcels, "many")
            report = workspace.build(10, observer=events.append)
            self.assertEqual(report["fragment_count"], 200)
            diagnostics = workspace.diagnostics()
            for layer in ("zones", "parcels"):
                stats = diagnostics["inputs"][layer]
                self.assertEqual(stats["candidate_pairs"], 0)
                self.assertEqual(stats["exact_topology_checks"], 0)
            self.assertEqual(diagnostics["totals"]["classified_clips"], 200)
            self.assertEqual(diagnostics["totals"]["zone_candidates"], 200)
            self.assertEqual(diagnostics["totals"]["parcel_candidates"], 200)
            # Instrumented actual work counts, not a hand-written report.
            actual_classified = sum(
                event["event"] == "classified_clip" for event in events
            )
            actual_blocks = sum(event["event"] == "block" for event in events)
            self.assertEqual(actual_classified, 200)
            self.assertEqual(actual_blocks, 200)
            self.assertLess(diagnostics["totals"]["classified_clips"], 200 * 200)

    def test_spaced_dataset_clip_scope_far_below_cartesian_product(self):
        zones = []
        parcels = []
        for k in range(60):
            x = (k % 12) * 25
            y = (k // 12) * 25
            zones.append(zone_feature(f"Z{k:03d}", box(x, y, x + 9, y + 9)))
            parcels.append(parcel_feature(f"P{k:03d}", "woodland", box(x, y, x + 4, y + 9)))
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, zones, parcels, "spaced")
            workspace.build(5)
            diagnostics = workspace.diagnostics()
            self.assertLess(diagnostics["totals"]["classified_clips"], 60 * 6)
            self.assertEqual(diagnostics["totals"]["final_facts"], 120)
            self.assertGreater(diagnostics["totals"]["staged_pieces"], 120)

    def test_pieces_are_actually_staged_and_merged_through_sqlite(self):
        # Straddling blocks must produce several staged pieces per business key.
        zone = box(0, 0, 6, 6)
        parcel = box(0, 0, 6, 6)
        events = []
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp, [zone_feature("Z", zone)], [parcel_feature("P", "woodland", parcel)]
            )
            workspace.build(2, observer=events.append)
            merge_events = [e for e in events if e["event"] == "merge_key"]
            classified = [e for e in merge_events if e["coverage_kind"] == "classified"]
            self.assertEqual(len(classified), 1)
            self.assertEqual(classified[0]["pieces"], 9)
            self.assertEqual(len(workspace.facts()), 1)
            self.assertEqual(
                workspace.diagnostics()["totals"]["staged_pieces"], 9
            )


class FailureInjectionTests(unittest.TestCase):
    def setUp(self):
        self.zone = box(0, 0, 4, 4)
        self.parcel = box(0, 0, 4, 4)

    def _workspace(self, temp):
        return Workspace(
            temp,
            [zone_feature("Z", self.zone)],
            [parcel_feature("P", "woodland", self.parcel)],
        )

    def test_staging_insert_failure_leaves_no_target_and_cleans_work(self):
        from landcover_cube import tiled as module

        def broken_insert(db, zone_id, coverage_kind, parcel_id, klass, geometry, i, j):
            raise RuntimeError("injected staging failure")

        original = module._insert_piece
        module._insert_piece = broken_insert
        try:
            with tempfile.TemporaryDirectory() as temp:
                workspace = self._workspace(temp)
                before = workspace.parcels_path.read_bytes()
                with self.assertRaises(ValueError) as caught:
                    workspace.build(2)
                self.assertIn("injected staging failure", str(caught.exception))
                self.assertFalse((workspace.root / "out").exists())
                self.assertEqual(workspace.parcels_path.read_bytes(), before)
                self.assertEqual(
                    [p.name for p in workspace.root.iterdir() if p.name.startswith(".landcover")],
                    [],
                )
        finally:
            module._insert_piece = original

    def test_merge_failure_leaves_no_target_and_cleans_work(self):
        from landcover_cube import tiled as module

        original = module._merge_pieces

        def broken_merge(rows):
            raise RuntimeError("injected merge failure")

        module._merge_pieces = broken_merge
        try:
            with tempfile.TemporaryDirectory() as temp:
                workspace = self._workspace(temp)
                with self.assertRaises(ValueError) as caught:
                    workspace.build(2)
                self.assertIn("injected merge failure", str(caught.exception))
                self.assertFalse((workspace.root / "out").exists())
                self.assertEqual(
                    [p.name for p in workspace.root.iterdir() if p.name.startswith(".landcover")],
                    [],
                )
        finally:
            module._merge_pieces = original

    def test_report_write_failure_leaves_no_target_and_rerun_succeeds(self):
        from landcover_cube import tiled as module

        original = module._write_json
        calls = {"count": 0}

        def broken_write(path, payload):
            calls["count"] += 1
            if calls["count"] == 3:
                raise RuntimeError("injected report write failure")
            return original(path, payload)

        module._write_json = broken_write
        try:
            with tempfile.TemporaryDirectory() as temp:
                workspace = self._workspace(temp)
                with self.assertRaises(RuntimeError):
                    workspace.build(2)
                self.assertFalse((workspace.root / "out").exists())
                self.assertEqual(
                    [p.name for p in workspace.root.iterdir() if p.name.startswith(".landcover")],
                    [],
                )
        finally:
            module._write_json = original
        with tempfile.TemporaryDirectory() as temp:
            workspace = self._workspace(temp)
            workspace.build(2)
            self.assertTrue((workspace.output / "build_diagnostics.json").exists())

    def test_existing_target_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = self._workspace(temp)
            workspace.output = workspace.root / "out"
            workspace.output.mkdir()
            (workspace.output / "sentinel.txt").write_text("keep")
            with self.assertRaises(FileExistsError):
                tiled(workspace.zones_path, workspace.parcels_path, 2, workspace.output)
            self.assertTrue((workspace.output / "sentinel.txt").exists())
            self.assertFalse((workspace.output / "fragments.geojson").exists())
            self.assertEqual(
                [p.name for p in workspace.root.iterdir() if p.name.startswith(".landcover")],
                [],
            )

    def test_validation_failure_does_not_create_target_or_touch_inputs(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 2, 2))],
                [parcel_feature("P", "a", box(5, 5, 7, 7)), parcel_feature("Q", "b", box(5.5, 5.5, 7.5, 7.5))],
            )
            zone_bytes = workspace.zones_path.read_bytes()
            parcel_bytes = workspace.parcels_path.read_bytes()
            with self.assertRaisesRegex(ValueError, "overlapping"):
                tiled(workspace.zones_path, workspace.parcels_path, 1, workspace.root / "out")
            self.assertFalse((workspace.root / "out").exists())
            self.assertEqual(workspace.zones_path.read_bytes(), zone_bytes)
            self.assertEqual(workspace.parcels_path.read_bytes(), parcel_bytes)


class DeterminismAndCliTests(unittest.TestCase):
    def test_rerun_with_fixed_bytes_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            tiled(DATA / "zones.geojson", DATA / "parcels.geojson", 3, temp / "first")
            tiled(DATA / "zones.geojson", DATA / "parcels.geojson", 3, temp / "second")
            for name in ("fragments.geojson", "coverage.json", "build_diagnostics.json"):
                self.assertEqual(
                    (temp / "first" / name).read_bytes(),
                    (temp / "second" / name).read_bytes(),
                )
            with sqlite3.connect(temp / "first/coverage.sqlite") as first:
                with sqlite3.connect(temp / "second/coverage.sqlite") as second:
                    query = (
                        "SELECT fragment_id,zone_id,parcel_id,class,area_m2,coverage_kind,"
                        "geometry_json FROM fragments ORDER BY zone_id,coverage_kind,"
                        "parcel_id IS NOT NULL,parcel_id"
                    )
                    self.assertEqual(
                        first.execute(query).fetchall(),
                        second.execute(query).fetchall(),
                    )

    def test_reordered_features_change_hash_but_not_final_semantics(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            zones = json.loads((DATA / "zones.geojson").read_text())
            parcels = json.loads((DATA / "parcels.geojson").read_text())
            original_zones = json.dumps(zones).encode()
            reordered = json.loads(json.dumps(parcels))
            reordered["features"].reverse()
            (temp / "p2.json").write_bytes(json.dumps(reordered).encode())
            tiled(DATA / "zones.geojson", DATA / "parcels.geojson", 3, temp / "a")
            tiled(DATA / "zones.geojson", temp / "p2.json", 3, temp / "b")
            first = json.loads((temp / "a/coverage.json").read_text())
            second = json.loads((temp / "b/coverage.json").read_text())
            self.assertNotEqual(
                first["input_sha256"]["parcels"], second["input_sha256"]["parcels"]
            )
            self.assertEqual(
                keyed_keys_for(temp / "a/fragments.geojson"),
                keyed_keys_for(temp / "b/fragments.geojson"),
            )
            self.assertEqual(first["zones"], second["zones"])

    def test_cli_subprocess_reads_back_geojson_sqlite_and_hashes(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            command = [
                PYTHON,
                "-m",
                "landcover_cube.tiled",
                "--zones",
                str(DATA / "zones.geojson"),
                "--parcels",
                str(DATA / "parcels.geojson"),
                "--tile-size",
                "3",
                "--output",
                str(temp / "cli"),
            ]
            completed = subprocess.run(
                command,
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            output = temp / "cli"
            self.assertEqual(
                sorted(p.name for p in output.iterdir()),
                [
                    "build_diagnostics.json",
                    "coverage.json",
                    "coverage.sqlite",
                    "fragments.geojson",
                ],
            )
            geojson = json.loads((output / "fragments.geojson").read_text())
            report = json.loads((output / "coverage.json").read_text())
            with sqlite3.connect(output / "coverage.sqlite") as db:
                rows = db.execute(
                    "SELECT fragment_id,zone_id,parcel_id,class,area_m2,coverage_kind,"
                    "geometry_json FROM fragments ORDER BY fragment_id"
                ).fetchall()
                hashes = dict(db.execute("SELECT name,sha256 FROM input_hashes"))
            self.assertEqual(hashes, report["input_sha256"])
            self.assertEqual(len(rows), len(geojson["features"]))
            for row, feature in zip(rows, geojson["features"]):
                props = feature["properties"]
                self.assertEqual(row[0], props["fragment_id"])
                self.assertEqual(row[1], props["zone_id"])
                self.assertEqual(row[2], props["parcel_id"])
                self.assertEqual(row[3], props["class"])
                self.assertAlmostEqual(row[4], props["area_m2"])
                self.assertEqual(row[5], props["coverage_kind"])
                self.assertEqual(json.loads(row[6]), feature["geometry"])
            # Rerun into a second directory gives the same logical content.
            second = temp / "cli2"
            subprocess.run(
                [*command[:6], str(DATA / "parcels.geojson"), "--tile-size", "3", "--output", str(second)],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(
                (output / "fragments.geojson").read_bytes(),
                (second / "fragments.geojson").read_bytes(),
            )

    def test_cli_refuses_existing_output_with_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "existing"
            output.mkdir()
            completed = subprocess.run(
                [
                    PYTHON,
                    "-m",
                    "landcover_cube.tiled",
                    "--tile-size",
                    "3",
                    "--output",
                    str(output),
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse((output / "coverage.json").exists())


def keyed_keys_for(path):
    document = json.loads(Path(path).read_text())
    return {
        (
            feature["properties"]["zone_id"],
            feature["properties"]["coverage_kind"],
            feature["properties"]["parcel_id"],
            feature["properties"]["class"],
        )
        for feature in document["features"]
    }


if __name__ == "__main__":
    unittest.main()
