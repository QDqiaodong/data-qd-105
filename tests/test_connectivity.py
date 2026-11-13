"""Connectivity vulnerability: synthetic graphs, thresholds, holes and failures."""
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from shapely.geometry import MultiPolygon, Polygon, box, mapping, shape

from landcover_cube.connectivity import (
    CAVEAT,
    connectivity,
    parse_shared_edge_threshold,
)

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
    """Input documents plus a report path outside a temporary directory."""

    def __init__(self, temp, zones, parcels, name="case"):
        self.root = Path(temp)
        self.zones_path = self.root / f"{name}_zones.geojson"
        self.parcels_path = self.root / f"{name}_parcels.geojson"
        self.zones_path.write_text(json.dumps(collection(zones)), encoding="utf-8")
        self.parcels_path.write_text(json.dumps(collection(parcels)), encoding="utf-8")
        self.output = self.root / f"{name}_report.json"

    def run(self, target_class, threshold):
        return connectivity(
            self.zones_path, self.parcels_path, target_class, threshold, self.output
        )

    def report_bytes(self):
        return self.output.read_bytes()

    def report(self):
        return json.loads(self.report_bytes())


def zone_of(report, zone_id):
    return next(zone for zone in report["zones"] if zone["zone_id"] == zone_id)


def vulnerability(zone, parcel_id):
    return next(
        entry for entry in zone["vulnerability"] if entry["parcel_id"] == parcel_id
    )


class ChainTests(unittest.TestCase):
    """A-B-C unit squares: removing B leaves two isolated squares."""

    def test_chain_removal_loss_and_ranking(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 3, 1))],
                [
                    parcel_feature("A", "forest", box(0, 0, 1, 1)),
                    parcel_feature("B", "forest", box(1, 0, 2, 1)),
                    parcel_feature("C", "forest", box(2, 0, 3, 1)),
                ],
            )
            report = workspace.run("forest", "1")
            zone = zone_of(report, "Z")
            self.assertEqual(zone["node_count"], 3)
            self.assertEqual(zone["edge_count"], 2)
            self.assertEqual(zone["baseline"]["component_count"], 1)
            self.assertEqual(zone["baseline"]["maximum_component_area_m2"], 3.0)
            self.assertEqual(
                [node["area_m2"] for node in zone["nodes"]], [1.0, 1.0, 1.0]
            )
            for edge in zone["edges"]:
                self.assertEqual(edge["shared_boundary_length_m"], 1.0)

            b = vulnerability(zone, "B")
            self.assertEqual(b["rank"], 1)
            self.assertEqual(b["maximum_remaining_component_area_m2"], 1.0)
            self.assertEqual(b["absolute_loss_m2"], 2.0)
            self.assertAlmostEqual(b["relative_loss"], 2 / 3)
            self.assertEqual(b["remaining_component_count"], 2)
            self.assertEqual(b["remaining_node_count"], 2)
            for component in b["remaining_components"]:
                self.assertEqual(component["node_count"], 1)
                self.assertEqual(component["area_m2"], 1.0)

            a = vulnerability(zone, "A")
            c = vulnerability(zone, "C")
            self.assertEqual([entry["parcel_id"] for entry in zone["vulnerability"]],
                             ["B", "A", "C"])
            for entry in (a, c):
                self.assertEqual(entry["maximum_remaining_component_area_m2"], 2.0)
                self.assertEqual(entry["absolute_loss_m2"], 1.0)
                self.assertAlmostEqual(entry["relative_loss"], 1 / 3)

    def test_chain_report_contains_geometry_hashes_and_caveat(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 3, 1))],
                [
                    parcel_feature("A", "forest", box(0, 0, 1, 1)),
                    parcel_feature("B", "forest", box(1, 0, 2, 1)),
                    parcel_feature("C", "forest", box(2, 0, 3, 1)),
                ],
            )
            report = workspace.run("forest", "1")
            self.assertEqual(report["mode"], "connectivity")
            self.assertEqual(report["coordinate_system"], "LOCAL_METERS")
            self.assertEqual(report["target_class"], "forest")
            self.assertEqual(report["shared_edge_threshold_m"], "1")
            import hashlib
            self.assertEqual(
                report["input_sha256"]["zones"],
                hashlib.sha256(workspace.zones_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                report["input_sha256"]["parcels"],
                hashlib.sha256(workspace.parcels_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(report["caveat"], CAVEAT)
            self.assertIn("species passage", CAVEAT)
            for node in zone_of(report, "Z")["nodes"]:
                restored = shape(node["geometry"])
                self.assertEqual(restored.area, node["area_m2"])
                self.assertTrue(restored.is_valid)
            for edge in zone_of(report, "Z")["edges"]:
                restored = shape(edge["shared_boundary"])
                self.assertEqual(restored.geom_type, "LineString")
                self.assertEqual(restored.length, edge["shared_boundary_length_m"])

    def test_independent_recomputation_from_report_matches(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 3, 1))],
                [
                    parcel_feature("A", "forest", box(0, 0, 1, 1)),
                    parcel_feature("B", "forest", box(1, 0, 2, 1)),
                    parcel_feature("C", "forest", box(2, 0, 3, 1)),
                ],
            )
            report = workspace.run("forest", "1")
            zone = zone_of(report, "Z")
            # Rebuild node identity from encoded geometry and the edge evidence.
            nodes = {node["node_id"]: shape(node["geometry"]) for node in zone["nodes"]}
            edge_pairs = {(edge["node_a"], edge["node_b"]) for edge in zone["edges"]}
            parent = {node_id: node_id for node_id in nodes}

            def find(node_id):
                while parent[node_id] != node_id:
                    parent[node_id] = parent[parent[node_id]]
                    node_id = parent[node_id]
                return node_id

            for left, right in edge_pairs:
                parent[find(left)] = find(right)
            components = {}
            for node_id, geometry in nodes.items():
                components.setdefault(find(node_id), []).append((node_id, geometry))
            areas = {
                root: math.fsum(geometry.area for _, geometry in members)
                for root, members in components.items()
            }
            self.assertEqual(max(areas.values()), zone["baseline"]["maximum_component_area_m2"])
            self.assertEqual(len(components), zone["baseline"]["component_count"])
            # Shared-edge evidence equals the recomputed boundary length.
            for edge in zone["edges"]:
                recomputed = nodes[edge["node_a"]].boundary.intersection(
                    nodes[edge["node_b"]].boundary
                )
                self.assertEqual(recomputed.length, edge["shared_boundary_length_m"])
            for entry in zone["vulnerability"]:
                remaining = set(nodes) - set(entry["removed_node_ids"])
                self.assertEqual(len(remaining), entry["remaining_node_count"])
                # Fully re-derive vulnerability from the encoded nodes/edges.
                parent = {node_id: node_id for node_id in remaining}

                def find_remaining(node_id):
                    while parent[node_id] != node_id:
                        parent[node_id] = parent[parent[node_id]]
                        node_id = parent[node_id]
                    return node_id

                for left, right in edge_pairs:
                    if left in remaining and right in remaining:
                        parent[find_remaining(left)] = find_remaining(right)
                groups = {}
                for node_id in remaining:
                    groups.setdefault(find_remaining(node_id), []).append(node_id)
                group_areas = [
                    math.fsum(nodes[node_id].area for node_id in group)
                    for group in groups.values()
                ]
                maximum = max(group_areas, default=0.0)
                self.assertEqual(len(groups), entry["remaining_component_count"])
                self.assertEqual(
                    maximum, entry["maximum_remaining_component_area_m2"]
                )
                self.assertEqual(
                    max(0.0, zone["baseline"]["maximum_component_area_m2"] - maximum),
                    entry["absolute_loss_m2"],
                )
                recorded = sorted(
                    component["node_ids"]
                    for component in entry["remaining_components"]
                )
                self.assertEqual(recorded, sorted(sorted(group) for group in groups.values()))
                # Removing every node of the vulnerable parcel is equivalent to
                # dropping exactly those node identities from the graph.
                removed_parcels = {
                    node["parcel_id"]
                    for node in zone["nodes"]
                    if node["node_id"] in entry["removed_node_ids"]
                }
                self.assertEqual(removed_parcels, {entry["parcel_id"]})


class ThresholdTests(unittest.TestCase):
    def test_threshold_equal_to_shared_length_connects(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 2, 1))],
                [
                    parcel_feature("A", "forest", box(0, 0, 1, 1)),
                    parcel_feature("B", "forest", box(1, 0, 2, 1)),
                ],
            )
            report = workspace.run("forest", "1.0")
            zone = zone_of(report, "Z")
            self.assertEqual(zone["edge_count"], 1)
            self.assertEqual(report["shared_edge_threshold_m"], "1")
            self.assertEqual(zone["baseline"]["component_count"], 1)
            entry = vulnerability(zone, "A")
            self.assertEqual(entry["remaining_component_count"], 1)
            self.assertEqual(entry["maximum_remaining_component_area_m2"], 1.0)
            self.assertEqual(entry["absolute_loss_m2"], 1.0)
            self.assertEqual(entry["relative_loss"], 0.5)

    def test_threshold_above_shared_length_disconnects(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 2, 1))],
                [
                    parcel_feature("A", "forest", box(0, 0, 1, 1)),
                    parcel_feature("B", "forest", box(1, 0, 2, 1)),
                ],
            )
            report = workspace.run("forest", "1.0001")
            zone = zone_of(report, "Z")
            self.assertEqual(zone["edge_count"], 0)
            self.assertEqual(zone["baseline"]["component_count"], 2)
            self.assertEqual(zone["baseline"]["maximum_component_area_m2"], 1.0)
            self.assertEqual(vulnerability(zone, "A")["absolute_loss_m2"], 0.0)

    def test_u_shape_total_shared_length_above_each_segment(self):
        # The filler B meets A along three segments totalling 5 m.
        u_shape = Polygon(
            [(0, 0), (3, 0), (3, 3), (2, 3), (2, 1), (1, 1), (1, 3), (0, 3)]
        )
        filler = box(1, 1, 2, 3)
        self.assertGreater(u_shape.area, 0)
        with tempfile.TemporaryDirectory() as temp:
            for threshold, expected_edges in (("5", 1), ("5.0001", 0)):
                workspace = Workspace(
                    temp,
                    [zone_feature("Z", box(0, 0, 3, 3))],
                    [
                        parcel_feature("U", "forest", u_shape),
                        parcel_feature("F", "forest", filler),
                    ],
                    f"case{threshold}",
                )
                report = workspace.run("forest", threshold)
                zone = zone_of(report, "Z")
                self.assertEqual(zone["edge_count"], expected_edges)
                if expected_edges:
                    edge = zone["edges"][0]
                    self.assertEqual(edge["shared_boundary_length_m"], 5.0)
                    self.assertEqual(
                        shape(edge["shared_boundary"]).geom_type,
                        "MultiLineString",
                    )

    def test_point_contact_is_not_an_edge(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 2, 2))],
                [
                    parcel_feature("A", "forest", box(0, 0, 1, 1)),
                    parcel_feature("B", "forest", box(1, 1, 2, 2)),
                ],
            )
            report = workspace.run("forest", "0.0001")
            zone = zone_of(report, "Z")
            self.assertEqual(zone["edge_count"], 0)
            self.assertEqual(zone["baseline"]["component_count"], 2)
            self.assertEqual(zone["baseline"]["maximum_component_area_m2"], 1.0)

    def test_threshold_parsing_contract(self):
        self.assertEqual(parse_shared_edge_threshold("0.50"), Decimal("0.50"))
        self.assertEqual(parse_shared_edge_threshold(2), Decimal(2))
        self.assertEqual(parse_shared_edge_threshold(1.5), Decimal("1.5"))
        for bad in (0, -1, "0", "-1", "", "nan", "inf", float("nan"), float("inf"),
                    True, False, None, " 1", "1_000", "1,0", "0x10"):
            with self.assertRaises(ValueError):
                parse_shared_edge_threshold(bad)


class GraphShapeTests(unittest.TestCase):
    def test_ring_cycle_removal_keeps_path_intact(self):
        # Four unit squares around the centre: A-B-C-D-A cycle.
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 2, 2))],
                [
                    parcel_feature("A", "forest", box(0, 0, 1, 1)),
                    parcel_feature("B", "forest", box(1, 0, 2, 1)),
                    parcel_feature("C", "forest", box(1, 1, 2, 2)),
                    parcel_feature("D", "forest", box(0, 1, 1, 2)),
                ],
            )
            report = workspace.run("forest", "1")
            zone = zone_of(report, "Z")
            self.assertEqual(zone["node_count"], 4)
            self.assertEqual(zone["edge_count"], 4)
            self.assertEqual(zone["baseline"]["maximum_component_area_m2"], 4.0)
            for entry in zone["vulnerability"]:
                self.assertEqual(entry["remaining_component_count"], 1)
                self.assertEqual(entry["maximum_remaining_component_area_m2"], 3.0)
                self.assertEqual(entry["absolute_loss_m2"], 1.0)
                self.assertAlmostEqual(entry["relative_loss"], 0.25)
                self.assertEqual(entry["remaining_component_count"], 1)
                self.assertEqual(entry["remaining_components"][0]["node_count"], 3)

    def test_multicomponent_parcel_removes_all_nodes_at_once(self):
        multi = MultiPolygon([box(0, 0, 1, 1), box(2, 0, 3, 1)])
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(-1, 0, 4, 1))],
                [
                    parcel_feature("A", "forest", box(-1, 0, 0, 1)),
                    parcel_feature("M", "forest", multi),
                    parcel_feature("C", "forest", box(3, 0, 4, 1)),
                ],
            )
            report = workspace.run("forest", "1")
            zone = zone_of(report, "Z")
            self.assertEqual(zone["node_count"], 4)
            self.assertEqual(
                sorted(node["node_id"].split("|")[-1] for node in zone["nodes"]),
                ["0", "0", "0", "1"],
            )
            self.assertEqual(zone["baseline"]["component_count"], 2)
            self.assertEqual(zone["baseline"]["maximum_component_area_m2"], 2.0)
            m = vulnerability(zone, "M")
            self.assertEqual(m["rank"], 1)
            self.assertEqual(len(m["removed_node_ids"]), 2)
            self.assertEqual(m["removed_area_m2"], 2.0)
            self.assertEqual(m["remaining_node_count"], 2)
            self.assertEqual(m["remaining_component_count"], 2)
            self.assertEqual(m["maximum_remaining_component_area_m2"], 1.0)
            self.assertEqual(m["absolute_loss_m2"], 1.0)
            self.assertAlmostEqual(m["relative_loss"], 0.5)

    def test_same_parcel_components_are_never_implicitly_joined(self):
        # One parcel split into two diagonally touching squares by a zone hole.
        split = MultiPolygon([box(0, 0, 1, 1), box(1, 1, 2, 2)])
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 2, 2))],
                [parcel_feature("M", "forest", split)],
            )
            report = workspace.run("forest", "0.001")
            zone = zone_of(report, "Z")
            self.assertEqual(zone["node_count"], 2)
            self.assertEqual(zone["edge_count"], 0)
            self.assertEqual(zone["baseline"]["component_count"], 2)
            self.assertEqual(zone["baseline"]["maximum_component_area_m2"], 1.0)

    def test_hole_is_retained_and_hole_boundary_counts_as_shared_edge(self):
        donut = Polygon(
            [(0, 0), (3, 0), (3, 3), (0, 3)],
            holes=[[(1, 1), (2, 1), (2, 2), (1, 2)]],
        )
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(-1, -1, 4, 4))],
                [
                    parcel_feature("DONUT", "forest", donut),
                    parcel_feature("FILL", "forest", box(1, 1, 2, 2)),
                ],
            )
            report = workspace.run("forest", "1")
            zone = zone_of(report, "Z")
            self.assertEqual(zone["node_count"], 2)
            self.assertEqual(zone["edge_count"], 1)
            donut_node = next(
                node for node in zone["nodes"] if node["parcel_id"] == "DONUT"
            )
            self.assertEqual(donut_node["area_m2"], 8.0)
            self.assertEqual(len(donut_node["geometry"]["coordinates"]), 2)
            self.assertEqual(donut_node["geometry"]["coordinates"][1][0], (1.0, 1.0))
            self.assertEqual(zone["edges"][0]["shared_boundary_length_m"], 4.0)
            self.assertEqual(zone["baseline"]["component_count"], 1)
            self.assertEqual(zone["baseline"]["maximum_component_area_m2"], 9.0)
            donut_entry = vulnerability(zone, "DONUT")
            self.assertEqual(donut_entry["maximum_remaining_component_area_m2"], 1.0)
            self.assertEqual(donut_entry["absolute_loss_m2"], 8.0)
            fill_entry = vulnerability(zone, "FILL")
            self.assertEqual(fill_entry["maximum_remaining_component_area_m2"], 8.0)
            self.assertEqual(fill_entry["absolute_loss_m2"], 1.0)
            self.assertLess(fill_entry["relative_loss"], donut_entry["relative_loss"])

    def test_empty_graph_and_non_target_classes(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 2, 2))],
                [parcel_feature("G", "grass", box(0, 0, 2, 1))],
            )
            report = workspace.run("forest", "1")
            zone = zone_of(report, "Z")
            self.assertEqual(zone["node_count"], 0)
            self.assertEqual(zone["edge_count"], 0)
            self.assertEqual(zone["target_area_m2"], 0.0)
            self.assertEqual(zone["baseline"]["component_count"], 0)
            self.assertEqual(zone["baseline"]["maximum_component_area_m2"], 0.0)
            self.assertEqual(zone["baseline"]["components"], [])
            self.assertEqual(zone["vulnerability"], [])

    def test_target_class_named_unclassified_is_matched_by_string(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 2, 1))],
                [
                    parcel_feature("A", "unclassified", box(0, 0, 1, 1)),
                    parcel_feature("B", "unclassified", box(1, 0, 2, 1)),
                ],
            )
            report = workspace.run("unclassified", "1")
            zone = zone_of(report, "Z")
            self.assertEqual(zone["node_count"], 2)
            self.assertEqual(zone["edge_count"], 1)
            self.assertEqual(zone["target_class"], "unclassified")

    def test_zones_are_separate_graphs_and_never_cross_connected(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [
                    zone_feature("Z1", box(0, 0, 1, 1)),
                    zone_feature("Z2", box(1, 0, 2, 1)),
                ],
                [
                    parcel_feature("A", "forest", box(0, 0, 1, 1)),
                    parcel_feature("B", "forest", box(1, 0, 2, 1)),
                ],
            )
            report = workspace.run("forest", "1")
            self.assertEqual(report["zone_count"], 2)
            for zone in report["zones"]:
                self.assertEqual(zone["node_count"], 1)
                self.assertEqual(zone["edge_count"], 0)
                self.assertEqual(zone["baseline"]["maximum_component_area_m2"], 1.0)
                self.assertEqual(zone["vulnerability"][0]["absolute_loss_m2"], 1.0)
                self.assertEqual(zone["vulnerability"][0]["relative_loss"], 1.0)
                for node in zone["nodes"]:
                    self.assertEqual(node["node_id"].split("|")[0], f"2:{zone['zone_id']}")

    def test_parcel_spanning_two_zones_yields_independent_nodes(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [
                    zone_feature("Z1", box(0, 0, 1, 1)),
                    zone_feature("Z2", box(1, 0, 2, 1)),
                ],
                [parcel_feature("P", "forest", box(0, 0, 2, 1))],
            )
            report = workspace.run("forest", "1")
            first, second = report["zones"]
            self.assertEqual(first["node_count"], 1)
            self.assertEqual(second["node_count"], 1)
            self.assertNotEqual(
                first["nodes"][0]["node_id"], second["nodes"][0]["node_id"]
            )
            self.assertNotEqual(
                first["vulnerability"][0]["removed_node_ids"],
                second["vulnerability"][0]["removed_node_ids"],
            )
            self.assertEqual(first["edge_count"], 0)
            self.assertEqual(second["edge_count"], 0)

    def test_clipped_parcel_leaves_only_positive_area_components(self):
        # A parcel barely crossing the zone boundary yields one clipped node.
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 1, 1))],
                [
                    parcel_feature("P", "forest", box(0.999999, 0, 2, 1)),
                    parcel_feature("Q", "forest", box(2, 0, 3, 1)),
                ],
            )
            report = workspace.run("forest", "0.001")
            zone = zone_of(report, "Z")
            self.assertEqual(zone["node_count"], 1)
            self.assertGreater(zone["target_area_m2"], 0)
            self.assertAlmostEqual(zone["target_area_m2"], 1e-6, places=12)


class RankingTests(unittest.TestCase):
    def test_equal_losses_rank_by_parcel_id(self):
        # Two identical isolated squares: loss 0 each, tie broken by ID.
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 3, 1))],
                [
                    parcel_feature("B", "forest", box(0, 0, 1, 1)),
                    parcel_feature("A", "forest", box(2, 0, 3, 1)),
                ],
            )
            report = workspace.run("forest", "1")
            zone = zone_of(report, "Z")
            self.assertEqual(
                [(entry["rank"], entry["parcel_id"]) for entry in zone["vulnerability"]],
                [(1, "A"), (2, "B")],
            )


class DeterminismTests(unittest.TestCase):
    def test_rerun_is_byte_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            first = temp / "first.json"
            second = temp / "second.json"
            connectivity(
                DATA / "connectivity_zones.geojson",
                DATA / "connectivity_parcels.geojson",
                "forest",
                "1",
                first,
            )
            connectivity(
                DATA / "connectivity_zones.geojson",
                DATA / "connectivity_parcels.geojson",
                "forest",
                "1",
                second,
            )
            self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_reordered_features_change_hash_but_not_graph_semantics(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            zones = json.loads((DATA / "connectivity_zones.geojson").read_text())
            parcels = json.loads((DATA / "connectivity_parcels.geojson").read_text())
            reordered = json.loads(json.dumps(parcels))
            reordered["features"].reverse()
            (temp / "reordered.geojson").write_text(json.dumps(reordered))
            first = temp / "a.json"
            second = temp / "b.json"
            connectivity(
                DATA / "connectivity_zones.geojson",
                DATA / "connectivity_parcels.geojson",
                "forest",
                "1",
                first,
            )
            connectivity(
                DATA / "connectivity_zones.geojson",
                temp / "reordered.geojson",
                "forest",
                "1",
                second,
            )
            report_a = json.loads(first.read_text())
            report_b = json.loads(second.read_text())
            self.assertNotEqual(
                report_a["input_sha256"]["parcels"],
                report_b["input_sha256"]["parcels"],
            )
            self.assertEqual(report_a["zones"], report_b["zones"])
            self.assertEqual(report_a["definitions"], report_b["definitions"])

    def test_threshold_text_is_canonical(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 1, 1))],
                [parcel_feature("P", "forest", box(0, 0, 1, 1))],
            )
            for value, text in (("1.0", "1"), ("0.50", "0.5"), ("1e-2", "0.01")):
                report = workspace.run("forest", value)
                self.assertEqual(report["shared_edge_threshold_m"], text)


class FailureAndAtomicityTests(unittest.TestCase):
    def test_invalid_input_keeps_previous_report(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 1, 1))],
                [parcel_feature("P", "forest", box(0, 0, 1, 1))],
            )
            workspace.run("forest", "1")
            original = workspace.report_bytes()
            workspace.parcels_path.write_text(
                json.dumps(collection([parcel_feature("P", "forest", box(0, 0, 1, 1)),
                                       parcel_feature("Q", "forest", box(0, 0, 1, 1))]))
            )
            with self.assertRaisesRegex(ValueError, "overlapping"):
                workspace.run("forest", "1")
            self.assertEqual(workspace.report_bytes(), original)
            self.assertEqual(sorted(p.name for p in Path(temp).iterdir()),
                             [
                                 "case_parcels.geojson",
                                 "case_report.json",
                                 "case_zones.geojson",
                             ])

    def test_invalid_threshold_and_class_keep_previous_report(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 1, 1))],
                [parcel_feature("P", "forest", box(0, 0, 1, 1))],
            )
            workspace.run("forest", "1")
            original = workspace.report_bytes()
            for threshold in ("0", "-1", "nan", float("nan"), True):
                with self.assertRaises(ValueError):
                    workspace.run("forest", threshold)
                self.assertEqual(workspace.report_bytes(), original)
            for target in ("", None, 5):
                with self.assertRaises(ValueError):
                    workspace.run(target, "1")
                self.assertEqual(workspace.report_bytes(), original)

    def test_whole_input_is_validated_even_for_unused_features(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 1, 1))],
                [
                    parcel_feature("G", "grass", box(0, 0, 1, 1)),
                    parcel_feature("H", "grass", box(0, 0, 1, 1)),
                ],
            )
            with self.assertRaisesRegex(ValueError, "overlapping parcel"):
                workspace.run("forest", "1")
            self.assertFalse(workspace.output.exists())

    def test_missing_files_and_directory_output_raise(self):
        with tempfile.TemporaryDirectory() as temp:
            temp = Path(temp)
            with self.assertRaises(OSError):
                connectivity(
                    temp / "missing.geojson",
                    temp / "missing2.geojson",
                    "forest",
                    "1",
                    temp / "out.json",
                )
            zones = temp / "zones.geojson"
            parcels = temp / "parcels.geojson"
            zones.write_text(json.dumps(collection([zone_feature("Z", box(0, 0, 1, 1))])))
            parcels.write_text(json.dumps(collection([])))
            directory = temp / "report_dir"
            directory.mkdir()
            with self.assertRaisesRegex(ValueError, "not a directory"):
                connectivity(zones, parcels, "forest", "1", directory)
            self.assertTrue(directory.is_dir())
            self.assertEqual(list(directory.iterdir()), [])

    def test_new_report_is_created_atomically_without_leftovers(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(
                temp,
                [zone_feature("Z", box(0, 0, 1, 1))],
                [parcel_feature("P", "forest", box(0, 0, 1, 1))],
            )
            workspace.run("forest", "1")
            self.assertEqual(
                sorted(p.name for p in Path(temp).iterdir()),
                ["case_parcels.geojson", "case_report.json", "case_zones.geojson"],
            )


class CliTests(unittest.TestCase):
    def test_cli_writes_json_and_reports_zone_maxima(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "connectivity.json"
            command = [
                PYTHON, "-m", "landcover_cube.connectivity",
                "--zones", str(DATA / "connectivity_zones.geojson"),
                "--parcels", str(DATA / "connectivity_parcels.geojson"),
                "--class", "forest",
                "--threshold", "1",
                "--output", str(output),
            ]
            completed = subprocess.run(
                command, cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(json.loads(completed.stdout), {"CHAIN": 3.0})
            report = json.loads(output.read_text())
            zone = zone_of(report, "CHAIN")
            self.assertEqual(zone["baseline"]["maximum_component_area_m2"], 3.0)
            self.assertEqual(
                [entry["parcel_id"] for entry in zone["vulnerability"]],
                ["B", "A", "C"],
            )

    def test_cli_aliases_and_failure_exit_code(self):
        with tempfile.TemporaryDirectory() as temp:
            output = Path(temp) / "aliases.json"
            completed = subprocess.run(
                [
                    PYTHON, "-m", "landcover_cube.connectivity",
                    "--zones", str(DATA / "connectivity_zones.geojson"),
                    "--parcels", str(DATA / "connectivity_parcels.geojson"),
                    "--target-class", "forest",
                    "--shared-edge-threshold", "1.5",
                    "--output", str(output),
                ],
                cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(
                json.loads(output.read_text())["shared_edge_threshold_m"], "1.5"
            )
            failed = subprocess.run(
                [
                    PYTHON, "-m", "landcover_cube.connectivity",
                    "--zones", str(DATA / "connectivity_zones.geojson"),
                    "--parcels", str(DATA / "connectivity_parcels.geojson"),
                    "--class", "forest",
                    "--threshold", "0",
                    "--output", str(output),
                ],
                cwd=ROOT, env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True, text=True, check=False,
            )
            self.assertEqual(failed.returncode, 2)
            self.assertIn("shared-edge threshold", failed.stderr)
            # Failed CLI run leaves the earlier report unchanged.
            self.assertEqual(
                json.loads(output.read_text())["shared_edge_threshold_m"], "1.5"
            )


if __name__ == "__main__":
    unittest.main()
