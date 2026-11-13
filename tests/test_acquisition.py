"""Acquisition: survey topology to canonical parcels plus per-edge audit."""
import copy
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from shapely.geometry import shape

from landcover_cube.acquisition import acquire

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
PYTHON = sys.executable

NESTED_NODES = [
    ("N00", 0, 0),
    ("N01", 10, 0),
    ("N02", 10, 10),
    ("N03", 0, 10),
    ("N04", 2, 2),
    ("N05", 8, 2),
    ("N06", 8, 8),
    ("N07", 2, 8),
    ("N08", 4, 4),
    ("N09", 6, 4),
    ("N10", 6, 6),
    ("N11", 4, 6),
    ("N12", 20, 0),
    ("N13", 20, 10),
]
NESTED_EDGES = [
    ("E01", "N00", "N01"),
    ("E02", "N01", "N02"),
    ("E03", "N02", "N03"),
    ("E04", "N03", "N00"),
    ("E05", "N04", "N05"),
    ("E06", "N05", "N06"),
    ("E07", "N06", "N07"),
    ("E08", "N07", "N04"),
    ("E09", "N08", "N09"),
    ("E10", "N09", "N10"),
    ("E11", "N10", "N11"),
    ("E12", "N11", "N08"),
    ("E13", "N01", "N12"),
    ("E14", "N12", "N13"),
    ("E15", "N13", "N02"),
]
NESTED_PARCELS = [
    {
        "parcel_id": "P_NEST",
        "class": "woodland",
        "edge_ids": [f"E{index:02d}" for index in range(1, 13)],
    },
    {
        "parcel_id": "P_EAST",
        "class": "grass",
        "edge_ids": ["E13", "E14", "E15", "E02"],
    },
]


def network(nodes=None, edges=None, parcels=None, **overrides):
    document = {
        "type": "SurveyNetwork",
        "coordinate_system": "LOCAL_METERS",
        "nodes": [
            {"id": node_id, "x": x, "y": y} for node_id, x, y in (nodes or [])
        ],
        "edges": [
            {"id": edge_id, "a": a, "b": b} for edge_id, a, b in (edges or [])
        ],
        "parcels": copy.deepcopy(list(parcels or [])),
    }
    document.update(overrides)
    return document


def nested_network(**overrides):
    return network(NESTED_NODES, NESTED_EDGES, NESTED_PARCELS, **overrides)


class Workspace:
    def __init__(self, temp, document, name="case", output=None):
        self.root = Path(temp)
        self.network_path = self.root / f"{name}_network.geojson"
        self.network_path.write_text(json.dumps(document), encoding="utf-8")
        self.output = output or (self.root / f"{name}_out")

    def run(self):
        return acquire(self.network_path, self.output)

    def geojson_bytes(self):
        return (self.output / "parcels.geojson").read_bytes()

    def geojson(self):
        return json.loads(self.geojson_bytes())

    def audit(self):
        return json.loads((self.output / "acquisition_audit.json").read_text())


def parcel_record(audit, parcel_id):
    return next(item for item in audit["parcels"] if item["parcel_id"] == parcel_id)


def assert_acquire_fails(test, document, message=None):
    with tempfile.TemporaryDirectory() as temp:
        workspace = Workspace(temp, document)
        before = workspace.network_path.read_bytes()
        with test.assertRaisesRegex(ValueError, message) if message else test.assertRaises(ValueError):
            workspace.run()
        test.assertFalse(workspace.output.exists())
        test.assertEqual(workspace.network_path.read_bytes(), before)
        test.assertEqual(
            [
                path.name
                for path in workspace.root.iterdir()
                if path.name.startswith(".landcover-acquisition-")
            ],
            [],
        )


class NestedSquaresTests(unittest.TestCase):
    """Three concentric squares of side 10/6/2: shell, hole, island."""

    def test_area_ring_hierarchy_and_multipolygon(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, nested_network())
            audit = workspace.run()
            features = {
                feature["properties"]["parcel_id"]: feature
                for feature in workspace.geojson()["features"]
            }
            self.assertEqual(set(features), {"P_NEST", "P_EAST"})
            nested = features["P_NEST"]
            self.assertEqual(nested["properties"]["class"], "woodland")
            self.assertEqual(nested["geometry"]["type"], "MultiPolygon")
            self.assertEqual(len(nested["geometry"]["coordinates"]), 2)
            geometry = shape(nested["geometry"])
            self.assertEqual(geometry.area, 68.0)
            self.assertEqual([part.area for part in geometry.geoms], [64.0, 4.0])
            self.assertEqual(shape(features["P_EAST"]["geometry"]).area, 100.0)

            record = parcel_record(audit, "P_NEST")
            self.assertEqual(record["area_m2"], 68.0)
            self.assertEqual(record["ring_count"], 3)
            self.assertEqual(record["face_count"], 2)
            self.assertEqual(record["geometry_type"], "MultiPolygon")
            by_index = {ring["ring_index"]: ring for ring in record["rings"]}
            self.assertEqual(
                [(ring["depth"], ring["role"]) for ring in by_index.values()],
                [(0, "shell"), (1, "hole"), (2, "shell")],
            )
            self.assertEqual(
                [ring["parent_ring_index"] for ring in by_index.values()],
                [None, 0, 1],
            )
            self.assertEqual(
                [ring["area_m2"] for ring in by_index.values()], [100.0, 36.0, 4.0]
            )
            self.assertEqual(
                record["faces"],
                [
                    {"shell_ring_index": 0, "hole_ring_indices": [1]},
                    {"shell_ring_index": 2, "hole_ring_indices": []},
                ],
            )
            for ring in record["rings"]:
                self.assertEqual(len(ring["segments"]), 4)
            self.assertEqual(by_index[0]["node_ids"], ["N00", "N01", "N02", "N03"])
            self.assertEqual(by_index[1]["node_ids"], ["N04", "N07", "N06", "N05"])
            self.assertEqual(by_index[2]["node_ids"], ["N08", "N09", "N10", "N11"])
            # Emitted rings: shell CCW, hole CW, island shell CCW.
            emitted = nested["geometry"]["coordinates"]
            self.assertEqual(
                [list(face[0][0]) for face in emitted],
                [[0.0, 0.0], [4.0, 4.0]],
            )
            self.assertEqual(
                [list(ring[0]) for ring in emitted[0][1:]],
                [[2.0, 2.0]],
            )
            # The hole ring is clockwise.
            hole = emitted[0][1]
            area2 = sum(
                hole[index][0] * hole[(index + 1) % 4][1]
                - hole[(index + 1) % 4][0] * hole[index][1]
                for index in range(4)
            )
            self.assertLess(area2, 0)
            self.assertEqual(len(emitted[0]), 2)

    def test_declared_edge_ids_are_retained_in_the_audit(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, nested_network())
            audit = workspace.run()
            self.assertEqual(
                parcel_record(audit, "P_NEST")["edge_ids"],
                [f"E{index:02d}" for index in range(1, 13)],
            )
            self.assertEqual(
                parcel_record(audit, "P_EAST")["edge_ids"],
                ["E13", "E14", "E15", "E02"],
            )

    def test_audit_source_hash_edges_and_segment_provenance(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, nested_network())
            audit = workspace.run()
            self.assertEqual(audit["mode"], "acquisition")
            self.assertEqual(audit["coordinate_system"], "LOCAL_METERS")
            self.assertEqual(
                audit["source_sha256"],
                hashlib.sha256(workspace.network_path.read_bytes()).hexdigest(),
            )
            self.assertEqual(
                audit["source"], {"nodes": 14, "edges": 15, "parcels": 2}
            )
            self.assertEqual(
                [edge["id"] for edge in audit["edges"]],
                [f"E{index:02d}" for index in range(1, 16)],
            )
            edges = {edge["id"]: edge for edge in audit["edges"]}
            self.assertEqual(edges["E02"]["parcel_ids"], ["P_EAST", "P_NEST"])
            self.assertEqual(edges["E02"]["length_m"], 10.0)
            self.assertEqual(edges["E02"]["source_index"], 1)
            self.assertEqual(edges["E03"]["parcel_ids"], ["P_NEST"])
            self.assertEqual(
                sorted(edge["id"] for edge in audit["edges"] if edge["parcel_ids"]),
                [f"E{index:02d}" for index in range(1, 16)],
            )

            record = parcel_record(audit, "P_EAST")
            ring = record["rings"][0]
            # E02 is declared N01->N02 in the survey but the canonical ring
            # traverses N02->N01, so it is reported as reverse.
            self.assertEqual(
                [segment["edge_id"] for segment in ring["segments"]],
                ["E13", "E14", "E15", "E02"],
            )
            self.assertEqual(
                [segment["direction"] for segment in ring["segments"]],
                ["forward", "forward", "forward", "reverse"],
            )
            self.assertEqual(
                [segment["from_node"] for segment in ring["segments"]],
                ["N01", "N12", "N13", "N02"],
            )
            self.assertEqual(
                [segment["to_node"] for segment in ring["segments"]],
                ["N12", "N13", "N02", "N01"],
            )
            self.assertEqual(
                [segment["from_source_index"] for segment in ring["segments"]],
                [1, 12, 13, 2],
            )
            self.assertEqual(
                [segment["to_source_index"] for segment in ring["segments"]],
                [12, 13, 2, 1],
            )
            self.assertEqual(
                [segment["edge_source_index"] for segment in ring["segments"]],
                [12, 13, 14, 1],
            )
            # Every ring of every parcel is fully covered by segment records.
            for parcel in audit["parcels"]:
                for ring_record in parcel["rings"]:
                    self.assertEqual(
                        ring_record["segments"][0]["from_node"],
                        ring_record["node_ids"][0],
                    )
                    self.assertEqual(
                        sum(
                            1
                            for segment in ring_record["segments"]
                            if segment["edge_id"]
                            not in [s["edge_id"] for s in ring_record["segments"]]
                        ),
                        0,
                    )
                    self.assertEqual(
                        [segment["direction"] for segment in ring_record["segments"]].count(
                            "forward"
                        )
                        + [segment["direction"] for segment in ring_record["segments"]].count(
                            "reverse"
                        ),
                        len(ring_record["segments"]),
                    )


class ReorderingAndReversalTests(unittest.TestCase):
    def test_shuffled_inputs_and_reversed_endpoints_preserve_geojson(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Workspace(root, nested_network(), "original").run()
            original = (root / "original_out/parcels.geojson").read_bytes()
            original_hash = hashlib.sha256(
                (root / "original_network.geojson").read_bytes()
            ).hexdigest()

            nodes = list(reversed(NESTED_NODES))
            edges = [
                (edge_id, right, left) if index % 3 == 0 else (edge_id, left, right)
                for index, (edge_id, left, right) in enumerate(
                    reversed(NESTED_EDGES)
                )
            ]
            reordered_parcels = [
                {
                    "parcel_id": "P_NEST",
                    "class": "woodland",
                    "edge_ids": [f"E{index:02d}" for index in range(12, 0, -1)],
                },
                {
                    "parcel_id": "P_EAST",
                    "class": "grass",
                    "edge_ids": ["E02", "E15", "E14", "E13"],
                },
            ]
            Workspace(
                root,
                network(nodes, edges, reordered_parcels),
                "reordered",
            ).run()
            self.assertEqual(
                (root / "reordered_out/parcels.geojson").read_bytes(), original
            )
            reordered_audit = json.loads(
                (root / "reordered_out/acquisition_audit.json").read_text()
            )
            self.assertNotEqual(reordered_audit["source_sha256"], original_hash)
            # Geometry is unchanged, but the audit truthfully records the reversed
            # declared endpoints for the affected edges.
            edges_by_id = {edge["id"]: edge for edge in reordered_audit["edges"]}
            self.assertEqual((edges_by_id["E01"]["a"], edges_by_id["E01"]["b"]), ("N00", "N01"))
            self.assertEqual((edges_by_id["E15"]["a"], edges_by_id["E15"]["b"]), ("N02", "N13"))
            self.assertEqual(
                [ring["depth"] for ring in parcel_record(reordered_audit, "P_NEST")["rings"]],
                [0, 1, 2],
            )

    def test_rotation_of_canonical_ring_is_lexicographic(self):
        with tempfile.TemporaryDirectory() as temp:
            nodes = [("S", 5, 0), ("T", 0, 0), ("U", 0, 5), ("V", 5, 5)]
            edges = [("e1", "S", "T"), ("e2", "T", "U"), ("e3", "U", "V"), ("e4", "V", "S")]
            parcels = [{"parcel_id": "P", "class": "forest", "edge_ids": ["e1", "e2", "e3", "e4"]}]
            workspace = Workspace(temp, network(nodes, edges, parcels))
            audit = workspace.run()
            record = parcel_record(audit, "P")
            # Reversed to CCW and rotated to the smallest coordinate rotation:
            # coordinates start at (0,0) then (5,0), (5,5), (0,5).
            self.assertEqual(record["rings"][0]["node_ids"], ["T", "S", "V", "U"])
            workspace_geometry = workspace.geojson()["features"][0]["geometry"]
            self.assertEqual(
                workspace_geometry["coordinates"][0],
                [[0.0, 0.0], [5.0, 0.0], [5.0, 5.0], [0.0, 5.0], [0.0, 0.0]],
            )


class DeeperNestingTests(unittest.TestCase):
    def test_two_holes_in_one_face_stay_one_polygon(self):
        nodes = [
            ("O0", 0, 0), ("O1", 10, 0), ("O2", 10, 10), ("O3", 0, 10),
            ("H1a", 1, 1), ("H1b", 3, 1), ("H1c", 3, 3), ("H1d", 1, 3),
            ("H2a", 6, 6), ("H2b", 8, 6), ("H2c", 8, 8), ("H2d", 6, 8),
        ]
        edges = [
            ("o1", "O0", "O1"), ("o2", "O1", "O2"), ("o3", "O2", "O3"), ("o4", "O3", "O0"),
            ("h1", "H1a", "H1b"), ("h2", "H1b", "H1c"), ("h3", "H1c", "H1d"), ("h4", "H1d", "H1a"),
            ("i1", "H2a", "H2b"), ("i2", "H2b", "H2c"), ("i3", "H2c", "H2d"), ("i4", "H2d", "H2a"),
        ]
        parcels = [{"parcel_id": "P", "class": "forest", "edge_ids": [e[0] for e in edges]}]
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, network(nodes, edges, parcels))
            audit = workspace.run()
            feature = workspace.geojson()["features"][0]
            self.assertEqual(feature["geometry"]["type"], "Polygon")
            self.assertEqual(len(feature["geometry"]["coordinates"]), 3)
            self.assertEqual(shape(feature["geometry"]).area, 92.0)
            record = parcel_record(audit, "P")
            self.assertEqual(
                [(ring["depth"], ring["role"]) for ring in record["rings"]],
                [(0, "shell"), (1, "hole"), (1, "hole")],
            )
            self.assertEqual(
                record["faces"],
                [{"shell_ring_index": 0, "hole_ring_indices": [1, 2]}],
            )

    def test_four_level_nesting_keeps_holes_of_islands(self):
        # 10 / 6 / 4 / 2 concentric squares: 100-36+16-4 = 76.
        nodes = [
            ("A0", 0, 0), ("A1", 10, 0), ("A2", 10, 10), ("A3", 0, 10),
            ("B0", 2, 2), ("B1", 8, 2), ("B2", 8, 8), ("B3", 2, 8),
            ("C0", 3, 3), ("C1", 7, 3), ("C2", 7, 7), ("C3", 3, 7),
            ("D0", 4, 4), ("D1", 6, 4), ("D2", 6, 6), ("D3", 4, 6),
        ]
        edges = []
        for prefix, start in (("a", 0), ("b", 4), ("c", 8), ("d", 12)):
            for offset in range(4):
                edges.append(
                    (
                        f"{prefix}{offset + 1}",
                        nodes[start + offset][0],
                        nodes[start + (offset + 1) % 4][0],
                    )
                )
        parcels = [{"parcel_id": "P", "class": "forest", "edge_ids": [e[0] for e in edges]}]
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, network(nodes, edges, parcels))
            audit = workspace.run()
            feature = workspace.geojson()["features"][0]
            self.assertEqual(feature["geometry"]["type"], "MultiPolygon")
            self.assertEqual(shape(feature["geometry"]).area, 76.0)
            self.assertEqual(
                [part.area for part in shape(feature["geometry"]).geoms],
                [64.0, 12.0],
            )
            record = parcel_record(audit, "P")
            self.assertEqual(
                [(ring["depth"], ring["role"]) for ring in record["rings"]],
                [(0, "shell"), (1, "hole"), (2, "shell"), (3, "hole")],
            )
            self.assertEqual(
                record["faces"],
                [
                    {"shell_ring_index": 0, "hole_ring_indices": [1]},
                    {"shell_ring_index": 2, "hole_ring_indices": [3]},
                ],
            )


class SharedEdgeAndMultiFaceTests(unittest.TestCase):
    """A shared edge belongs to exactly two parcels; disjoint faces stay legal."""

    def shared_document(self):
        nodes = [
            ("LA", 0, 0),
            ("P", 1, 0),
            ("Q", 1, 1),
            ("LD", 0, 1),
            ("RB", 2, 0),
            ("RC", 2, 1),
        ]
        edges = [
            ("L1", "LA", "P"),
            ("S1", "P", "Q"),
            ("L3", "Q", "LD"),
            ("L4", "LD", "LA"),
            ("R1", "P", "RB"),
            ("R2", "RB", "RC"),
            ("R3", "RC", "Q"),
        ]
        parcels = [
            {"parcel_id": "L", "class": "forest", "edge_ids": ["L1", "S1", "L3", "L4"]},
            {"parcel_id": "R", "class": "grass", "edge_ids": ["R1", "R2", "R3", "S1"]},
        ]
        return network(nodes, edges, parcels)

    def test_two_parcels_share_one_edge(self):
        with tempfile.TemporaryDirectory() as temp:
            # Both parcels reference the same declared edge S1.
            workspace = Workspace(temp, self.shared_document())
            audit = workspace.run()
            features = workspace.geojson()["features"]
            self.assertEqual(len(features), 2)
            self.assertEqual(
                sorted(shape(feature["geometry"]).area for feature in features),
                [1.0, 1.0],
            )
            edges = {edge["id"]: edge for edge in audit["edges"]}
            self.assertEqual(edges["S1"]["parcel_ids"], ["L", "R"])
            self.assertEqual((edges["S1"]["a"], edges["S1"]["b"]), ("P", "Q"))
            for parcel in audit["parcels"]:
                used = [
                    segment["edge_id"]
                    for ring in parcel["rings"]
                    for segment in ring["segments"]
                ]
                self.assertEqual(len(used), len(set(used)))
                self.assertIn("S1", used)

    def test_edge_ids_are_checked_for_existence_before_ownership(self):
        nodes = [("A", 0, 0), ("B", 1, 0), ("C", 1, 1), ("D", 0, 1)]
        edges = [("e1", "A", "B"), ("e2", "B", "C"), ("e3", "C", "D"), ("e4", "D", "A")]
        parcels = [
            {"parcel_id": "P", "class": "forest", "edge_ids": ["e1", "e2", "e3", "e4"]},
            {"parcel_id": "Q", "class": "water", "edge_ids": ["missing"]},
        ]
        document = network(nodes, edges, parcels)
        assert_acquire_fails(self, document, "unknown edge")

    def test_duplicate_json_keys_and_non_finite_numbers_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "dup.json"
            path.write_text(
                '{"type":"SurveyNetwork","coordinate_system":"LOCAL_METERS",'
                '"nodes":[],"nodes":[],"edges":[],"parcels":[]}',
                encoding="utf-8",
            )
            output = Path(temp) / "out"
            with self.assertRaisesRegex(ValueError, "duplicate JSON key"):
                acquire(path, output)
            self.assertFalse(output.exists())
            path.write_text(
                '{"type":"SurveyNetwork","coordinate_system":"LOCAL_METERS",'
                '"nodes":[],"edges":[],"parcels":[],"extra":NaN}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                acquire(path, output)
            self.assertFalse(output.exists())
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "invalid JSON"):
                acquire(path, output)
            self.assertFalse(output.exists())

    def test_unused_node_or_edge_may_be_present(self):
        # Nodes and edges outside any parcel are allowed; only referenced
        # topology is reconstructed.
        with tempfile.TemporaryDirectory() as temp:
            nodes = [
                ("A", 0, 0), ("B", 2, 0), ("C", 2, 2), ("D", 0, 2),
                ("Z", 50, 50),
            ]
            edges = [
                ("e1", "A", "B"), ("e2", "B", "C"), ("e3", "C", "D"),
                ("e4", "D", "A"), ("e5", "Z", "A"),
            ]
            parcels = [
                {"parcel_id": "P", "class": "forest", "edge_ids": ["e1", "e2", "e3", "e4"]}
            ]
            workspace = Workspace(temp, network(nodes, edges, parcels))
            audit = workspace.run()
            self.assertEqual(workspace.geojson()["features"][0]["properties"]["parcel_id"], "P")
            self.assertEqual(audit["source"], {"nodes": 5, "edges": 5, "parcels": 1})
            unused = next(edge for edge in audit["edges"] if edge["id"] == "e5")
            self.assertEqual(unused["parcel_ids"], [])

    def test_three_parcels_sharing_one_edge_rejected(self):
        document = self.shared_document()
        document["parcels"].append(
            {"parcel_id": "T", "class": "water", "edge_ids": ["S1"]}
        )
        assert_acquire_fails(self, document, "more than two parcels")

    def test_disconnected_faces_become_multipolygon(self):
        with tempfile.TemporaryDirectory() as temp:
            first = [("A0", 0, 0), ("A1", 2, 0), ("A2", 2, 2), ("A3", 0, 2)]
            second = [("B0", 10, 0), ("B1", 12, 0), ("B2", 12, 2), ("B3", 10, 2)]
            edges = [
                ("EA1", "A0", "A1"),
                ("EA2", "A1", "A2"),
                ("EA3", "A2", "A3"),
                ("EA4", "A3", "A0"),
                ("EB1", "B0", "B1"),
                ("EB2", "B1", "B2"),
                ("EB3", "B2", "B3"),
                ("EB4", "B3", "B0"),
            ]
            parcel = {
                "parcel_id": "P",
                "class": "water",
                "edge_ids": ["EA1", "EA2", "EA3", "EA4", "EB1", "EB2", "EB3", "EB4"],
            }
            workspace = Workspace(temp, network(first + second, edges, [parcel]))
            audit = workspace.run()
            feature = workspace.geojson()["features"][0]
            self.assertEqual(feature["geometry"]["type"], "MultiPolygon")
            self.assertEqual(shape(feature["geometry"]).area, 8.0)
            record = parcel_record(audit, "P")
            self.assertEqual(record["face_count"], 2)
            self.assertEqual(record["ring_count"], 2)
            self.assertEqual(
                [(ring["depth"], ring["role"]) for ring in record["rings"]],
                [(0, "shell"), (0, "shell")],
            )


class RejectionTests(unittest.TestCase):
    def test_broken_chain_is_rejected(self):
        nodes = [("A", 0, 0), ("B", 1, 0), ("C", 1, 1)]
        edges = [("e1", "A", "B"), ("e2", "B", "C")]
        parcels = [{"parcel_id": "P", "class": "forest", "edge_ids": ["e1", "e2"]}]
        assert_acquire_fails(
            self, network(nodes, edges, parcels), "degree 1, expected 2"
        )

    def test_dangling_extra_edge_is_rejected(self):
        nodes = [("A", 0, 0), ("B", 1, 0), ("C", 1, 1), ("D", 0, 1), ("X", 5, 5)]
        edges = [
            ("e1", "A", "B"),
            ("e2", "B", "C"),
            ("e3", "C", "D"),
            ("e4", "D", "A"),
            ("e5", "C", "X"),
        ]
        parcels = [{"parcel_id": "P", "class": "forest", "edge_ids": ["e1", "e2", "e3", "e4", "e5"]}]
        assert_acquire_fails(self, network(nodes, edges, parcels), "degree")

    def test_zero_length_edge_and_duplicate_boundary(self):
        nodes = [("A", 0, 0), ("B", 0, 0), ("C", 1, 0)]
        assert_acquire_fails(
            self,
            network(nodes, [("e1", "A", "B")], []),
            "zero-length",
        )
        nodes = [("A", 0, 0), ("B", 1, 0), ("C", 1, 1), ("D", 0, 1)]
        edges = [("e1", "A", "B"), ("e2", "B", "A")]
        assert_acquire_fails(
            self, network(nodes, edges, []), "duplicates the boundary"
        )

    def test_collinear_backtracking_loop_rejected(self):
        # B->C->D retraces part of the boundary, so the ring is not simple even
        # though every node has degree two.
        nodes = [("A", 0, 0), ("B", 2, 0), ("C", 1, 0), ("D", 2, 1), ("E", 0, 1)]
        ids = [node[0] for node in nodes]
        edges = [
            (f"e{index}", ids[index], ids[(index + 1) % len(ids)])
            for index in range(len(ids))
        ]
        parcels = [
            {
                "parcel_id": "P",
                "class": "forest",
                "edge_ids": [edge[0] for edge in edges],
            }
        ]
        assert_acquire_fails(self, network(nodes, edges, parcels), "self-intersect")

    def test_self_intersecting_bowtie_rejected(self):
        nodes = [("A", 0, 0), ("B", 1, 1), ("C", 1, 0), ("D", 0, 1)]
        edges = [("e1", "A", "B"), ("e2", "B", "C"), ("e3", "C", "D"), ("e4", "D", "A")]
        parcels = [{"parcel_id": "P", "class": "forest", "edge_ids": ["e1", "e2", "e3", "e4"]}]
        assert_acquire_fails(self, network(nodes, edges, parcels), "self-intersect")

    def test_loops_touching_each_other_rejected(self):
        nodes = [
            ("A", 0, 0), ("B", 1, 0), ("C", 1, 1), ("D", 0, 1),
            ("E", 1, 1), ("F", 2, 1), ("G", 2, 2), ("H", 1, 2),
        ]
        edges = [
            ("a1", "A", "B"), ("a2", "B", "C"), ("a3", "C", "D"), ("a4", "D", "A"),
            ("b1", "E", "F"), ("b2", "F", "G"), ("b3", "G", "H"), ("b4", "H", "E"),
        ]
        parcels = [{"parcel_id": "P", "class": "forest", "edge_ids": [e[0] for e in edges]}]
        assert_acquire_fails(self, network(nodes, edges, parcels), "touch or cross")

    def test_loops_crossing_each_other_rejected(self):
        nodes = [
            ("A", 0, 0), ("B", 3, 0), ("C", 3, 3), ("D", 0, 3),
            ("E", 1, -1), ("F", 3, 2), ("G", 1, 2), ("H", 0, 1),
        ]
        edges = [
            ("a1", "A", "B"), ("a2", "B", "C"), ("a3", "C", "D"), ("a4", "D", "A"),
            ("b1", "E", "F"), ("b2", "F", "G"), ("b3", "G", "H"), ("b4", "H", "E"),
        ]
        parcels = [{"parcel_id": "P", "class": "forest", "edge_ids": [e[0] for e in edges]}]
        assert_acquire_fails(self, network(nodes, edges, parcels), "touch or cross")

    def test_duplicate_parcel_edge_reference_and_ownership_limit(self):
        nodes = [("A", 0, 0), ("B", 1, 0), ("C", 1, 1), ("D", 0, 1)]
        edges = [("e1", "A", "B"), ("e2", "B", "C"), ("e3", "C", "D"), ("e4", "D", "A")]
        parcels = [{"parcel_id": "P", "class": "forest", "edge_ids": ["e1", "e2", "e1", "e3", "e4"]}]
        assert_acquire_fails(self, network(nodes, edges, parcels), "more than once")

    def test_out_of_range_and_bad_structure(self):
        nodes = [("A", 0, 0), ("B", 1e7, 0), ("C", 1, 1), ("D", 0, 1)]
        edges = [("e1", "A", "B"), ("e2", "B", "C"), ("e3", "C", "D"), ("e4", "D", "A")]
        parcels = [{"parcel_id": "P", "class": "forest", "edge_ids": ["e1", "e2", "e3", "e4"]}]
        assert_acquire_fails(self, network(nodes, edges, parcels), "within")
        assert_acquire_fails(self, nested_network(type="FeatureCollection"), "SurveyNetwork")
        assert_acquire_fails(self, nested_network(coordinate_system="EPSG:4326"), "LOCAL_METERS")
        document = nested_network()
        document["nodes"][0]["extra"] = 1
        assert_acquire_fails(self, document, "exactly keys")
        document = nested_network()
        document["edges"][0]["id"] = "E02"
        assert_acquire_fails(self, document, "duplicate edge id")
        document = nested_network()
        document["parcels"][0]["edge_ids"].append("nope")
        assert_acquire_fails(self, document, "unknown edge")
        document = nested_network()
        document["edges"][0]["b"] = "nope"
        assert_acquire_fails(self, document, "unknown node")

    def test_overlapping_parcels_rejected_with_core_rule(self):
        with tempfile.TemporaryDirectory() as temp:
            first = [("MA0", 0, 0), ("MA1", 2, 0), ("MA2", 2, 2), ("MA3", 0, 2)]
            second = [("MB0", 0, 0), ("MB1", 2, 0), ("MB2", 2, 2), ("MB3", 0, 2)]
            edges = [
                ("MAe1", "MA0", "MA1"), ("MAe2", "MA1", "MA2"),
                ("MAe3", "MA2", "MA3"), ("MAe4", "MA3", "MA0"),
                ("MBe1", "MB0", "MB1"), ("MBe2", "MB1", "MB2"),
                ("MBe3", "MB2", "MB3"), ("MBe4", "MB3", "MB0"),
            ]
            parcels = [
                {"parcel_id": "A", "class": "forest", "edge_ids": ["MAe1", "MAe2", "MAe3", "MAe4"]},
                {"parcel_id": "B", "class": "forest", "edge_ids": ["MBe1", "MBe2", "MBe3", "MBe4"]},
            ]
            assert_acquire_fails(
                self, network(first + second, edges, parcels), "overlapping parcel polygons"
            )

    def test_invalid_input_leaves_no_partial_target(self):
        with tempfile.TemporaryDirectory() as temp:
            document = nested_network()
            document["nodes"][0]["x"] = "bad"
            workspace = Workspace(temp, document)
            before = workspace.network_path.read_bytes()
            with self.assertRaises(ValueError):
                workspace.run()
            self.assertFalse(workspace.output.exists())
            self.assertEqual(workspace.network_path.read_bytes(), before)
            self.assertEqual(
                [
                    path.name
                    for path in workspace.root.iterdir()
                    if path.name.startswith(".landcover-acquisition-")
                ],
                [],
            )

    def test_existing_target_survives_a_failed_attempt_unmodified(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, nested_network())
            workspace.output.mkdir()
            marker = workspace.output / "keep.txt"
            marker.write_text("existing", encoding="utf-8")
            before = workspace.network_path.read_bytes()
            with self.assertRaises(FileExistsError):
                workspace.run()
            self.assertEqual(
                sorted(path.name for path in workspace.output.iterdir()), ["keep.txt"]
            )
            self.assertEqual(marker.read_text(encoding="utf-8"), "existing")
            self.assertEqual(workspace.network_path.read_bytes(), before)


class PublishTests(unittest.TestCase):
    def test_output_directory_contains_exactly_two_products(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, nested_network())
            workspace.run()
            self.assertEqual(
                sorted(path.name for path in workspace.output.iterdir()),
                ["acquisition_audit.json", "parcels.geojson"],
            )
            self.assertEqual(
                [path.name for path in workspace.root.iterdir() if path.name.startswith(".")],
                [],
            )

    def test_existing_target_is_refused_even_when_empty(self):
        with tempfile.TemporaryDirectory() as temp:
            workspace = Workspace(temp, nested_network())
            workspace.output.mkdir()
            with self.assertRaisesRegex(FileExistsError, "already exists"):
                workspace.run()
            self.assertEqual([path.name for path in workspace.output.iterdir()], [])

    def test_success_is_byte_deterministic(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            Workspace(root, nested_network(), "one").run()
            Workspace(root, nested_network(), "two").run()
            for name in ("parcels.geojson", "acquisition_audit.json"):
                self.assertEqual(
                    (root / "one_out" / name).read_bytes(),
                    (root / "two_out" / name).read_bytes(),
                )


class CommandLineTests(unittest.TestCase):
    def cli(self, *arguments):
        return subprocess.run(
            [PYTHON, "-m", "landcover_cube.acquisition", *arguments],
            cwd=ROOT,
            env={**os.environ, "PYTHONPATH": str(ROOT)},
            capture_output=True,
            text=True,
            check=False,
        )

    def test_cli_output_feeds_the_core_coverage_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            acquisition = self.cli(
                "--network",
                str(DATA / "acquisition_network.json"),
                "--output",
                str(root / "acquired"),
            )
            self.assertEqual(acquisition.returncode, 0, acquisition.stderr)
            self.assertEqual(
                json.loads(acquisition.stdout), {"P_EAST": 100.0, "P_NEST": 68.0}
            )
            audit = json.loads(
                (root / "acquired/acquisition_audit.json").read_text()
            )
            self.assertEqual(
                audit["source_sha256"],
                hashlib.sha256(
                    (DATA / "acquisition_network.json").read_bytes()
                ).hexdigest(),
            )
            before = (DATA / "acquisition_network.json").read_bytes()

            coverage = subprocess.run(
                [
                    PYTHON,
                    "-m",
                    "landcover_cube",
                    "--parcels",
                    str(root / "acquired/parcels.geojson"),
                    "--output",
                    str(root / "coverage"),
                ],
                cwd=ROOT,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(coverage.returncode, 0, coverage.stderr)
            report = json.loads((root / "coverage/coverage.json").read_text())
            summaries = {zone["zone_id"]: zone for zone in report["zones"]}
            self.assertEqual(summaries["Z1"]["class_area_m2"], {"woodland": 68.0})
            self.assertEqual(summaries["Z1"]["unclassified_area_m2"], 32.0)
            self.assertEqual(summaries["Z2"]["class_area_m2"], {"grass": 100.0})
            self.assertEqual(
                report["input_sha256"]["parcels"],
                hashlib.sha256(
                    (root / "acquired/parcels.geojson").read_bytes()
                ).hexdigest(),
            )
            self.assertEqual(
                (DATA / "acquisition_network.json").read_bytes(), before
            )

    def test_cli_rejects_broken_chain_without_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            document = network(
                [("A", 0, 0), ("B", 1, 0), ("C", 1, 1)],
                [("e1", "A", "B"), ("e2", "B", "C")],
                [{"parcel_id": "P", "class": "forest", "edge_ids": ["e1", "e2"]}],
            )
            source = root / "broken.geojson"
            source.write_text(json.dumps(document), encoding="utf-8")
            completed = self.cli(
                "--network", str(source), "--output", str(root / "out")
            )
            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("acquisition failed", completed.stderr)
            self.assertFalse((root / "out").exists())


if __name__ == "__main__":
    unittest.main()
