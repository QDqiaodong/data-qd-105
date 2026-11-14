"""Two-period transition: rectangle case, shapes, matrix algebra, failures."""
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape

from landcover_cube.transition import transition

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


def rectangle_inputs():
    """The contracted 100 m2 case: left 60 forest / right 40 empty before,
    left 40 forest / right 60 grass after."""
    return (
        [zone_feature("TZ", box(0, 0, 10, 10))],
        [parcel_feature("B1", "forest", box(0, 0, 6, 10))],
        [
            parcel_feature("A1", "forest", box(0, 0, 4, 10)),
            parcel_feature("A2", "grass", box(4, 0, 10, 10)),
        ],
    )


class Workspace:
    """Three input documents plus a fresh output directory path."""

    def __init__(self, temp, zones, before, after, name="case"):
        self.root = Path(temp)
        self.zones_path = self.root / f"{name}_zones.geojson"
        self.before_path = self.root / f"{name}_before.geojson"
        self.after_path = self.root / f"{name}_after.geojson"
        self.zones_path.write_text(json.dumps(collection(zones)), encoding="utf-8")
        self.before_path.write_text(json.dumps(collection(before)), encoding="utf-8")
        self.after_path.write_text(json.dumps(collection(after)), encoding="utf-8")
        self.output = self.root / f"{name}_out"

    def run(self):
        return transition(
            self.zones_path, self.before_path, self.after_path, self.output
        )

    def geojson(self):
        return json.loads((self.output / "transitions.geojson").read_text())

    def matrix(self):
        return json.loads((self.output / "matrix.json").read_text())

    def input_bytes(self):
        return (
            self.zones_path.read_bytes(),
            self.before_path.read_bytes(),
            self.after_path.read_bytes(),
        )


def keyed_facts(document):
    """Business-key map of GeoJSON transition features."""
    result = {}
    for feature in document["features"]:
        props = feature["properties"]
        key = (
            props["zone_id"],
            props["before_coverage_kind"],
            props["before_class"],
            props["after_coverage_kind"],
            props["after_class"],
        )
        result[key] = {
            "transition_id": props["transition_id"],
            "area": props["area_m2"],
            "geometry": shape(feature["geometry"]),
        }
    return result


def leftover_work_dirs(root):
    return [
        path.name
        for path in Path(root).iterdir()
        if path.name.startswith(".landcover-transition-")
    ]


class ConsistencyMixin:
    """Key-by-key agreement of the three outputs plus the three digests."""

    def assert_three_outputs_consistent(self, workspace):
        document = workspace.geojson()
        report = workspace.matrix()
        expected_hashes = {
            "zones": hashlib.sha256(workspace.zones_path.read_bytes()).hexdigest(),
            "parcels_before": hashlib.sha256(
                workspace.before_path.read_bytes()
            ).hexdigest(),
            "parcels_after": hashlib.sha256(
                workspace.after_path.read_bytes()
            ).hexdigest(),
        }
        self.assertEqual(document["input_sha256"], expected_hashes)
        self.assertEqual(report["input_sha256"], expected_hashes)
        with sqlite3.connect(workspace.output / "transitions.sqlite") as db:
            self.assertEqual(
                dict(db.execute("SELECT name, sha256 FROM input_hashes")),
                expected_hashes,
            )
            transition_rows = db.execute(
                "SELECT transition_id, zone_id, before_coverage_kind,"
                " before_class, after_coverage_kind, after_class, area_m2,"
                " geometry_json FROM transitions ORDER BY transition_id"
            ).fetchall()
            cell_rows = db.execute(
                "SELECT zone_id, before_coverage_kind, before_class,"
                " after_coverage_kind, after_class, area_m2 FROM matrix_cells"
            ).fetchall()
        # GeoJSON features and SQLite transitions are the same facts.
        self.assertEqual(len(transition_rows), len(document["features"]))
        for feature, row in zip(document["features"], transition_rows):
            props = feature["properties"]
            self.assertEqual(props["transition_id"], row[0])
            self.assertEqual(props["zone_id"], row[1])
            self.assertEqual(props["before_coverage_kind"], row[2])
            self.assertEqual(props["before_class"], row[3])
            self.assertEqual(props["after_coverage_kind"], row[4])
            self.assertEqual(props["after_class"], row[5])
            self.assertEqual(props["area_m2"], row[6])
            self.assertEqual(
                json.dumps(feature["geometry"], sort_keys=True), row[7]
            )
        # Dense SQLite matrix cells equal the JSON matrix key by key.
        sqlite_cells = {tuple(row[:5]): row[5] for row in cell_rows}
        json_cells = {}
        positive_cells = {}
        for matrix in report["zones"]:
            for i, row in enumerate(matrix["rows"]):
                for j, column in enumerate(matrix["columns"]):
                    key = (
                        matrix["zone_id"],
                        row["coverage_kind"],
                        row["class"],
                        column["coverage_kind"],
                        column["class"],
                    )
                    area = matrix["areas_m2"][i][j]
                    json_cells[key] = area
                    if area > 0:
                        positive_cells[key] = area
        self.assertEqual(sqlite_cells, json_cells)
        # Positive cells are exactly the GeoJSON/SQLite transition facts.
        fact_areas = {
            key: fact["area"] for key, fact in keyed_facts(document).items()
        }
        self.assertEqual(fact_areas, positive_cells)
        self.assertEqual(report["transition_count"], len(positive_cells))
        return document, report


class RectangleCaseTests(ConsistencyMixin, unittest.TestCase):
    def test_contracted_100m2_rectangle_areas_and_positions(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, *rectangle_inputs())
            workspace.run()
            facts = keyed_facts(workspace.geojson())
            self.assertEqual(len(facts), 3)
            forest_to_forest = facts[
                ("TZ", "classified", "forest", "classified", "forest")
            ]
            forest_to_grass = facts[
                ("TZ", "classified", "forest", "classified", "grass")
            ]
            empty_to_grass = facts[
                ("TZ", "unclassified", None, "classified", "grass")
            ]
            self.assertEqual(forest_to_forest["area"], 40.0)
            self.assertEqual(forest_to_grass["area"], 20.0)
            self.assertEqual(empty_to_grass["area"], 40.0)
            # Positions: left 40 forest stays, middle 20 turns to grass,
            # right 40 was uncovered and becomes grass.
            self.assertEqual(forest_to_forest["geometry"].bounds, (0, 0, 4, 10))
            self.assertEqual(forest_to_grass["geometry"].bounds, (4, 0, 6, 10))
            self.assertEqual(empty_to_grass["geometry"].bounds, (6, 0, 10, 10))
            self.assertEqual(
                [fact["transition_id"] for fact in facts.values()], [1, 2, 3]
            )

    def test_rectangle_matrix_rows_columns_and_conservation(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, *rectangle_inputs())
            report = workspace.run()
            self.assertEqual(report["mode"], "transition")
            self.assertEqual(report["zone_count"], 1)
            self.assertEqual(report["transition_count"], 3)
            matrix = report["zones"][0]
            self.assertEqual(matrix["zone_id"], "TZ")
            self.assertEqual(matrix["zone_area_m2"], 100.0)
            self.assertEqual(
                matrix["rows"],
                [
                    {"coverage_kind": "classified", "class": "forest"},
                    {"coverage_kind": "unclassified", "class": None},
                ],
            )
            self.assertEqual(
                matrix["columns"],
                [
                    {"coverage_kind": "classified", "class": "forest"},
                    {"coverage_kind": "classified", "class": "grass"},
                ],
            )
            self.assertEqual(matrix["areas_m2"], [[40.0, 20.0], [0.0, 40.0]])
            # Row sums are the before-period classified/uncovered areas,
            # column sums the after-period ones; the total conserves 100 m2.
            self.assertEqual(matrix["row_totals_m2"], [60.0, 40.0])
            self.assertEqual(matrix["column_totals_m2"], [40.0, 60.0])
            self.assertEqual(matrix["total_m2"], 100.0)

    def test_rectangle_three_outputs_are_key_by_key_consistent(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, *rectangle_inputs())
            workspace.run()
            document, _ = self.assert_three_outputs_consistent(workspace)
            self.assertEqual(document["coordinate_system"], "LOCAL_METERS")
            with sqlite3.connect(workspace.output / "transitions.sqlite") as db:
                summary = db.execute(
                    "SELECT * FROM zone_summary WHERE zone_id='TZ'"
                ).fetchone()
            self.assertEqual(
                summary, ("TZ", 100.0, 60.0, 40.0, 100.0, 0.0, 100.0)
            )

    def test_bundled_fixture_reproduces_the_rectangle_case(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "bundled"
            report = transition(
                DATA / "transition_zones.geojson",
                DATA / "transition_parcels_before.geojson",
                DATA / "transition_parcels_after.geojson",
                output,
            )
            matrix = report["zones"][0]
            self.assertEqual(matrix["areas_m2"], [[40.0, 20.0], [0.0, 40.0]])
            facts = keyed_facts(
                json.loads((output / "transitions.geojson").read_text())
            )
            self.assertEqual(
                facts[("TZ", "classified", "forest", "classified", "grass")][
                    "geometry"
                ].bounds,
                (4, 0, 6, 10),
            )


class SpatialNotIdentityTests(ConsistencyMixin, unittest.TestCase):
    def test_split_parcel_with_new_ids_is_one_unchanged_combination(self):
        # One before parcel splits into two after parcels with new IDs but
        # the same class: spatially nothing changed, one forest->forest cell.
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 10, 10))],
                [parcel_feature("OLD", "forest", box(0, 0, 10, 10))],
                [
                    parcel_feature("NEW1", "forest", box(0, 0, 5, 10)),
                    parcel_feature("NEW2", "forest", box(5, 0, 10, 10)),
                ],
            )
            report = workspace.run()
            facts = keyed_facts(workspace.geojson())
            self.assertEqual(len(facts), 1)
            fact = facts[("Z", "classified", "forest", "classified", "forest")]
            self.assertEqual(fact["area"], 100.0)
            self.assertEqual(fact["geometry"].bounds, (0, 0, 10, 10))
            self.assertEqual(report["zones"][0]["areas_m2"], [[100.0]])

    def test_merged_parcels_with_new_id_stay_one_combination(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 10, 10))],
                [
                    parcel_feature("OLD1", "forest", box(0, 0, 5, 10)),
                    parcel_feature("OLD2", "forest", box(5, 0, 10, 10)),
                ],
                [parcel_feature("NEW", "forest", box(0, 0, 10, 10))],
            )
            workspace.run()
            facts = keyed_facts(workspace.geojson())
            self.assertEqual(len(facts), 1)
            self.assertEqual(
                facts[("Z", "classified", "forest", "classified", "forest")][
                    "area"
                ],
                100.0,
            )

    def test_same_parcel_id_does_not_pair_periods(self):
        # Identical IDs across periods must not suppress a real class change.
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 10, 10))],
                [parcel_feature("P", "forest", box(0, 0, 5, 10))],
                [parcel_feature("P", "grass", box(0, 0, 5, 10))],
            )
            workspace.run()
            facts = keyed_facts(workspace.geojson())
            self.assertEqual(
                facts[("Z", "classified", "forest", "classified", "grass")][
                    "area"
                ],
                50.0,
            )
            self.assertEqual(
                facts[("Z", "unclassified", None, "unclassified", None)]["area"],
                50.0,
            )
            self.assertNotIn(
                ("Z", "classified", "forest", "classified", "forest"), facts
            )


class GeometryShapeTests(unittest.TestCase):
    def test_hole_is_real_uncovered_and_hole_geometry_is_retained(self):
        donut = Polygon(
            [(0, 0), (10, 0), (10, 10), (0, 10)],
            holes=[[(4, 4), (6, 4), (6, 6), (4, 6)]],
        )
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 10, 10))],
                [parcel_feature("D", "forest", donut)],
                [
                    parcel_feature("D2", "forest", donut),
                    parcel_feature("F", "grass", box(4, 4, 6, 6)),
                ],
            )
            workspace.run()
            facts = keyed_facts(workspace.geojson())
            stayed = facts[("Z", "classified", "forest", "classified", "forest")]
            self.assertEqual(stayed["area"], 96.0)
            # The merged transition geometry keeps the hole.
            self.assertEqual(stayed["geometry"].geom_type, "Polygon")
            self.assertEqual(len(stayed["geometry"].interiors), 1)
            hole = Polygon(stayed["geometry"].interiors[0])
            self.assertEqual(hole.bounds, (4.0, 4.0, 6.0, 6.0))
            filled = facts[("Z", "unclassified", None, "classified", "grass")]
            self.assertEqual(filled["area"], 4.0)
            self.assertEqual(filled["geometry"].bounds, (4.0, 4.0, 6.0, 6.0))
            matrix = workspace.matrix()["zones"][0]
            self.assertEqual(matrix["areas_m2"], [[96.0, 0.0], [0.0, 4.0]])

    def test_disconnected_components_are_merged_into_one_fact(self):
        multi_before = MultiPolygon([box(0, 0, 2, 10), box(8, 0, 10, 10)])
        multi_after = MultiPolygon([box(0, 0, 2, 10), box(8, 0, 10, 10)])
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 10, 10))],
                [parcel_feature("M", "forest", multi_before)],
                [parcel_feature("N", "forest", multi_after)],
            )
            workspace.run()
            facts = keyed_facts(workspace.geojson())
            self.assertEqual(len(facts), 2)
            stayed = facts[("Z", "classified", "forest", "classified", "forest")]
            self.assertEqual(stayed["area"], 40.0)
            self.assertEqual(stayed["geometry"].geom_type, "MultiPolygon")
            self.assertEqual(len(stayed["geometry"].geoms), 2)
            self.assertEqual(
                sorted(part.bounds for part in stayed["geometry"].geoms),
                [(0.0, 0.0, 2.0, 10.0), (8.0, 0.0, 10.0, 10.0)],
            )
            untouched = facts[
                ("Z", "unclassified", None, "unclassified", None)
            ]
            self.assertEqual(untouched["area"], 60.0)
            self.assertEqual(untouched["geometry"].bounds, (2, 0, 8, 10))

    def test_boundary_contact_creates_no_transition_area(self):
        # Parcels touching along an edge share zero area: no forest->grass.
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 10, 10))],
                [parcel_feature("L", "forest", box(0, 0, 5, 10))],
                [parcel_feature("R", "grass", box(5, 0, 10, 10))],
            )
            workspace.run()
            facts = keyed_facts(workspace.geojson())
            self.assertEqual(len(facts), 2)
            self.assertEqual(
                facts[("Z", "classified", "forest", "unclassified", None)][
                    "area"
                ],
                50.0,
            )
            self.assertEqual(
                facts[("Z", "unclassified", None, "classified", "grass")][
                    "area"
                ],
                50.0,
            )
            matrix = workspace.matrix()["zones"][0]
            self.assertEqual(
                matrix["rows"],
                [
                    {"coverage_kind": "classified", "class": "forest"},
                    {"coverage_kind": "unclassified", "class": None},
                ],
            )
            self.assertEqual(
                matrix["columns"],
                [
                    {"coverage_kind": "classified", "class": "grass"},
                    {"coverage_kind": "unclassified", "class": None},
                ],
            )
            self.assertEqual(matrix["areas_m2"], [[0.0, 50.0], [50.0, 0.0]])

    def test_zones_sharing_an_edge_are_independent_matrices(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [
                    zone_feature("Z1", box(0, 0, 5, 10)),
                    zone_feature("Z2", box(5, 0, 10, 10)),
                ],
                [parcel_feature("W", "forest", box(0, 0, 10, 10))],
                [parcel_feature("W2", "grass", box(0, 0, 10, 10))],
            )
            report = workspace.run()
            self.assertEqual(report["zone_count"], 2)
            for matrix in report["zones"]:
                self.assertEqual(matrix["zone_area_m2"], 50.0)
                self.assertEqual(matrix["areas_m2"], [[50.0]])
                self.assertEqual(
                    matrix["rows"],
                    [{"coverage_kind": "classified", "class": "forest"}],
                )
                self.assertEqual(
                    matrix["columns"],
                    [{"coverage_kind": "classified", "class": "grass"}],
                )
            facts = keyed_facts(workspace.geojson())
            self.assertEqual(len(facts), 2)
            self.assertEqual(
                facts[("Z1", "classified", "forest", "classified", "grass")][
                    "geometry"
                ].bounds,
                (0, 0, 5, 10),
            )
            self.assertEqual(
                facts[("Z2", "classified", "forest", "classified", "grass")][
                    "geometry"
                ].bounds,
                (5, 0, 10, 10),
            )


class EmptyPeriodTests(unittest.TestCase):
    def test_empty_before_period_is_all_uncovered_rows(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 10, 10))],
                [],
                [parcel_feature("A", "grass", box(0, 0, 4, 10))],
            )
            report = workspace.run()
            facts = keyed_facts(workspace.geojson())
            self.assertEqual(
                facts[("Z", "unclassified", None, "classified", "grass")]["area"],
                40.0,
            )
            self.assertEqual(
                facts[("Z", "unclassified", None, "unclassified", None)]["area"],
                60.0,
            )
            matrix = report["zones"][0]
            self.assertEqual(
                matrix["rows"],
                [{"coverage_kind": "unclassified", "class": None}],
            )
            self.assertEqual(matrix["areas_m2"], [[40.0, 60.0]])
            self.assertEqual(matrix["row_totals_m2"], [100.0])
            self.assertEqual(matrix["column_totals_m2"], [40.0, 60.0])

    def test_empty_after_period_is_all_uncovered_columns(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 10, 10))],
                [parcel_feature("F", "forest", box(0, 0, 6, 10))],
                [],
            )
            report = workspace.run()
            facts = keyed_facts(workspace.geojson())
            self.assertEqual(
                facts[("Z", "classified", "forest", "unclassified", None)][
                    "area"
                ],
                60.0,
            )
            self.assertEqual(
                facts[("Z", "unclassified", None, "unclassified", None)]["area"],
                40.0,
            )
            matrix = report["zones"][0]
            self.assertEqual(
                matrix["columns"],
                [{"coverage_kind": "unclassified", "class": None}],
            )
            self.assertEqual(matrix["areas_m2"], [[60.0], [40.0]])

    def test_both_periods_empty_give_one_unchanged_uncovered_cell(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp, [zone_feature("Z", box(0, 0, 10, 10))], [], []
            )
            report = workspace.run()
            facts = keyed_facts(workspace.geojson())
            self.assertEqual(len(facts), 1)
            fact = facts[("Z", "unclassified", None, "unclassified", None)]
            self.assertEqual(fact["area"], 100.0)
            self.assertEqual(fact["geometry"].bounds, (0, 0, 10, 10))
            matrix = report["zones"][0]
            self.assertEqual(matrix["areas_m2"], [[100.0]])
            self.assertEqual(matrix["total_m2"], 100.0)


class SameNameClassTests(unittest.TestCase):
    def test_real_unclassified_class_never_mixes_with_uncovered(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 10, 10))],
                [parcel_feature("U", "unclassified", box(0, 0, 5, 10))],
                [
                    parcel_feature("V", "unclassified", box(0, 0, 5, 10)),
                    parcel_feature("G", "grass", box(5, 0, 10, 10)),
                ],
            )
            report = workspace.run()
            facts = keyed_facts(workspace.geojson())
            self.assertEqual(len(facts), 2)
            real = facts[
                (
                    "Z",
                    "classified",
                    "unclassified",
                    "classified",
                    "unclassified",
                )
            ]
            self.assertEqual(real["area"], 50.0)
            uncovered = facts[
                ("Z", "unclassified", None, "classified", "grass")
            ]
            self.assertEqual(uncovered["area"], 50.0)
            matrix = report["zones"][0]
            # The real class and genuine uncovered area stay separate rows.
            self.assertEqual(
                matrix["rows"],
                [
                    {"coverage_kind": "classified", "class": "unclassified"},
                    {"coverage_kind": "unclassified", "class": None},
                ],
            )
            self.assertEqual(matrix["row_totals_m2"], [50.0, 50.0])
            with sqlite3.connect(workspace.output / "transitions.sqlite") as db:
                kinds = db.execute(
                    "SELECT DISTINCT before_coverage_kind, before_class"
                    " FROM matrix_cells ORDER BY 1, 2"
                ).fetchall()
            self.assertEqual(
                kinds, [("classified", "unclassified"), ("unclassified", None)]
            )


class SwapTransposeTests(unittest.TestCase):
    def test_swapped_periods_transpose_matrix_and_keep_spatial_meaning(self):
        with tempfile.TemporaryDirectory() as temp:
            zones, before, after = rectangle_inputs()
            forward_ws = Workspace(temp, zones, before, after, name="forward")
            backward_ws = Workspace(
                temp, zones, after, before, name="backward"
            )
            forward = forward_ws.run()
            backward = backward_ws.run()
            forward_matrix = forward["zones"][0]
            backward_matrix = backward["zones"][0]
            # Rows and columns exchange roles; areas transpose exactly.
            self.assertEqual(forward_matrix["rows"], backward_matrix["columns"])
            self.assertEqual(forward_matrix["columns"], backward_matrix["rows"])
            transposed = [
                [forward_matrix["areas_m2"][i][j] for i in range(2)]
                for j in range(2)
            ]
            self.assertEqual(backward_matrix["areas_m2"], transposed)
            self.assertEqual(
                forward_matrix["row_totals_m2"],
                backward_matrix["column_totals_m2"],
            )
            self.assertEqual(
                forward_matrix["column_totals_m2"],
                backward_matrix["row_totals_m2"],
            )
            self.assertEqual(forward_matrix["total_m2"], backward_matrix["total_m2"])
            # Every swapped fact keeps the same area and the same geometry.
            forward_facts = keyed_facts(forward_ws.geojson())
            backward_facts = keyed_facts(backward_ws.geojson())
            self.assertEqual(len(forward_facts), len(backward_facts))
            for key, fact in forward_facts.items():
                zone_id, before_kind, before_class, after_kind, after_class = key
                swapped_key = (
                    zone_id,
                    after_kind,
                    after_class,
                    before_kind,
                    before_class,
                )
                swapped = backward_facts[swapped_key]
                self.assertEqual(fact["area"], swapped["area"])
                self.assertEqual(
                    fact["geometry"].bounds, swapped["geometry"].bounds
                )
                self.assertTrue(
                    fact["geometry"].equals(swapped["geometry"]),
                    f"geometry changed under swap for {key}",
                )
                self.assertEqual(
                    fact["geometry"].symmetric_difference(
                        swapped["geometry"]
                    ).area,
                    0,
                )


class DeterminismTests(unittest.TestCase):
    def test_replay_is_byte_identical_and_sqlite_logically_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            zones, before, after = rectangle_inputs()
            first = Workspace(temp, zones, before, after, name="first")
            second = Workspace(temp, zones, before, after, name="second")
            first.run()
            second.run()
            for name in ("transitions.geojson", "matrix.json"):
                self.assertEqual(
                    (first.output / name).read_bytes(),
                    (second.output / name).read_bytes(),
                )
            for output in (first.output, second.output):
                with sqlite3.connect(output / "transitions.sqlite") as db:
                    content = [
                        db.execute(
                            f"SELECT * FROM {table} ORDER BY {ordering}"
                        ).fetchall()
                        for table, ordering in (
                            ("transitions", "1"),
                            ("matrix_cells", "1, 2, 3, 4, 5"),
                            ("zone_summary", "1"),
                            ("input_hashes", "1"),
                        )
                    ]
                if output is first.output:
                    first_content = content
                else:
                    self.assertEqual(first_content, content)

    def test_reordered_features_change_digest_but_not_semantics(self):
        with tempfile.TemporaryDirectory() as temp:
            zones, before, after = rectangle_inputs()
            plain = Workspace(temp, zones, before, after, name="plain")
            plain.run()
            reordered_after = list(reversed(after))
            shuffled = Workspace(temp, zones, before, reordered_after, name="shuffled")
            shuffled.run()
            plain_report = plain.matrix()
            shuffled_report = shuffled.matrix()
            self.assertNotEqual(
                plain_report["input_sha256"]["parcels_after"],
                shuffled_report["input_sha256"]["parcels_after"],
            )
            self.assertEqual(plain_report["zones"], shuffled_report["zones"])
            self.assertEqual(
                keyed_facts(plain.geojson()).keys(),
                keyed_facts(shuffled.geojson()).keys(),
            )
            for key, fact in keyed_facts(plain.geojson()).items():
                other = keyed_facts(shuffled.geojson())[key]
                self.assertEqual(fact["area"], other["area"])
                self.assertTrue(fact["geometry"].equals(other["geometry"]))


class ValidationTests(unittest.TestCase):
    def assert_rejected(self, workspace, pattern, expect=ValueError):
        before_inputs = workspace.input_bytes()
        with self.assertRaisesRegex(expect, pattern):
            workspace.run()
        self.assertFalse(workspace.output.exists())
        self.assertEqual(workspace.input_bytes(), before_inputs)
        self.assertEqual(leftover_work_dirs(workspace.root), [])

    def test_invalid_after_period_is_rejected_before_any_output(self):
        with tempfile.TemporaryDirectory() as temp:
            zones, before, _ = rectangle_inputs()
            bad_after = [
                parcel_feature("A1", "forest", box(0, 0, 4, 10)),
                parcel_feature("A2", "grass", box(2, 0, 10, 10)),
            ]
            workspace = Workspace(temp, zones, before, bad_after)
            self.assert_rejected(workspace, "overlapping parcel")

    def test_invalid_before_period_is_rejected_before_any_output(self):
        with tempfile.TemporaryDirectory() as temp:
            zones, _, after = rectangle_inputs()
            bad_before = [
                parcel_feature("B1", "forest", box(0, 0, 6, 10)),
                parcel_feature("B2", "grass", box(5, 0, 10, 10)),
            ]
            workspace = Workspace(temp, zones, bad_before, after)
            self.assert_rejected(workspace, "overlapping parcel")

    def test_overlapping_zones_are_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            _, before, after = rectangle_inputs()
            bad_zones = [
                zone_feature("Z1", box(0, 0, 6, 10)),
                zone_feature("Z2", box(5, 0, 10, 10)),
            ]
            workspace = Workspace(temp, bad_zones, before, after)
            self.assert_rejected(workspace, "overlapping zone")

    def test_contract_violations_in_any_input(self):
        zones, before, after = rectangle_inputs()
        cases = []
        duplicate = [
            parcel_feature("B1", "forest", box(0, 0, 6, 10)),
            parcel_feature("B1", "grass", box(6, 0, 10, 10)),
        ]
        cases.append(("before", duplicate, "duplicate"))
        bowtie = zone_feature(
            "TZ", Polygon([(0, 0), (10, 10), (0, 10), (10, 0)])
        )
        cases.append(("zones", [bowtie], "invalid"))
        wrong_class = [parcel_feature("A1", "", box(0, 0, 4, 10))]
        cases.append(("after", wrong_class, "properties"))
        with tempfile.TemporaryDirectory() as temp:
            for index, (role, features, pattern) in enumerate(cases):
                z, b, a = rectangle_inputs()
                if role == "zones":
                    z = features
                elif role == "before":
                    b = features
                else:
                    a = features
                workspace = Workspace(temp, z, b, a, name=f"bad{index}")
                self.assert_rejected(workspace, pattern)

    def test_wrong_crs_and_empty_zones_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            zones, before, after = rectangle_inputs()
            workspace = Workspace(temp, zones, before, after, name="crs")
            document = json.loads(workspace.zones_path.read_text())
            document["coordinate_system"] = "EPSG:4326"
            workspace.zones_path.write_text(json.dumps(document))
            self.assert_rejected(workspace, "LOCAL_METERS")
        with tempfile.TemporaryDirectory() as temp:
            zones, before, after = rectangle_inputs()
            workspace = Workspace(temp, zones, before, after, name="empty")
            workspace.zones_path.write_text(json.dumps(collection([])))
            self.assert_rejected(workspace, "empty")

    def test_whole_inputs_validated_even_outside_zones(self):
        # Overlapping parcels far outside every zone are still rejected.
        with tempfile.TemporaryDirectory() as temp:
            zones, before, after = rectangle_inputs()
            bad_after = after + [
                parcel_feature("FAR1", "water", box(100, 100, 110, 110)),
                parcel_feature("FAR2", "water", box(105, 105, 115, 115)),
            ]
            workspace = Workspace(temp, zones, before, bad_after)
            self.assert_rejected(workspace, "overlapping parcel")

    def test_missing_input_file_raises_oserror(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            with self.assertRaises(OSError):
                transition(
                    temp / "missing_zones.geojson",
                    temp / "missing_before.geojson",
                    temp / "missing_after.geojson",
                    temp / "out",
                )
            self.assertFalse((temp / "out").exists())


class FailureAtomicityTests(unittest.TestCase):
    def test_sqlite_write_failure_leaves_no_partial_target(self):
        from landcover_cube import transition as module

        original = module._write_sqlite

        def broken(*args, **kwargs):
            raise RuntimeError("injected sqlite failure")

        module._write_sqlite = broken
        try:
            with tempfile.TemporaryDirectory() as temp:
                workspace = Workspace(temp, *rectangle_inputs())
                before_inputs = workspace.input_bytes()
                with self.assertRaisesRegex(RuntimeError, "injected sqlite"):
                    workspace.run()
                self.assertFalse(workspace.output.exists())
                self.assertEqual(workspace.input_bytes(), before_inputs)
                self.assertEqual(leftover_work_dirs(workspace.root), [])
        finally:
            module._write_sqlite = original

    def test_json_write_failure_leaves_no_partial_target(self):
        from landcover_cube import transition as module

        original = module._write_json
        calls = {"count": 0}

        def broken(path, payload):
            calls["count"] += 1
            if calls["count"] == 2:
                raise RuntimeError("injected json failure")
            return original(path, payload)

        module._write_json = broken
        try:
            with tempfile.TemporaryDirectory() as temp:
                workspace = Workspace(temp, *rectangle_inputs())
                with self.assertRaisesRegex(RuntimeError, "injected json"):
                    workspace.run()
                self.assertFalse(workspace.output.exists())
                self.assertEqual(leftover_work_dirs(workspace.root), [])
        finally:
            module._write_json = original
        # A clean rerun after the injected failure succeeds.
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, *rectangle_inputs())
            workspace.run()
            self.assertTrue((workspace.output / "matrix.json").exists())

    def test_existing_target_is_never_overwritten(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, *rectangle_inputs())
            workspace.output.mkdir()
            (workspace.output / "sentinel.txt").write_text("keep")
            with self.assertRaises(FileExistsError):
                workspace.run()
            self.assertTrue((workspace.output / "sentinel.txt").exists())
            self.assertFalse((workspace.output / "matrix.json").exists())
            self.assertEqual(leftover_work_dirs(workspace.root), [])


class CliTests(ConsistencyMixin, unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run(
            [PYTHON, "-m", "landcover_cube.transition", *args],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_builds_outputs_and_prints_summary(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "transition"
            completed = self.run_cli(
                "--zones", str(DATA / "transition_zones.geojson"),
                "--before", str(DATA / "transition_parcels_before.geojson"),
                "--after", str(DATA / "transition_parcels_after.geojson"),
                "--output", str(output),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(completed.stdout),
                {"TZ": {"forest->forest": 40.0, "forest->grass": 20.0,
                        "uncovered->grass": 40.0}},
            )
            self.assertEqual(
                sorted(path.name for path in output.iterdir()),
                ["matrix.json", "transitions.geojson", "transitions.sqlite"],
            )
            workspace = Workspace(temp, *rectangle_inputs(), name="cli")
            workspace.output = output
            workspace.zones_path = DATA / "transition_zones.geojson"
            workspace.before_path = DATA / "transition_parcels_before.geojson"
            workspace.after_path = DATA / "transition_parcels_after.geojson"
            self.assert_three_outputs_consistent(workspace)

    def test_cli_aliases_and_failure_exit_codes(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "aliases"
            completed = self.run_cli(
                "--zones", str(DATA / "transition_zones.geojson"),
                "--parcels-before", str(DATA / "transition_parcels_before.geojson"),
                "--parcels-after", str(DATA / "transition_parcels_after.geojson"),
                "--output", str(output),
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            # Invalid after-period input exits nonzero and writes nothing.
            bad = Path(temp) / "bad_after.geojson"
            bad.write_text(json.dumps(collection([
                parcel_feature("X", "forest", box(0, 0, 5, 10)),
                parcel_feature("Y", "grass", box(4, 0, 10, 10)),
            ])))
            failed = self.run_cli(
                "--zones", str(DATA / "transition_zones.geojson"),
                "--before", str(DATA / "transition_parcels_before.geojson"),
                "--after", str(bad),
                "--output", str(Path(temp) / "bad_out"),
            )
            self.assertEqual(failed.returncode, 2)
            self.assertIn("overlapping", failed.stderr)
            self.assertFalse((Path(temp) / "bad_out").exists())
            # Existing target is refused with a nonzero exit.
            refused = self.run_cli(
                "--zones", str(DATA / "transition_zones.geojson"),
                "--before", str(DATA / "transition_parcels_before.geojson"),
                "--after", str(DATA / "transition_parcels_after.geojson"),
                "--output", str(output),
            )
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn("already exists", refused.stderr)


if __name__ == "__main__":
    unittest.main()
