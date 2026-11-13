"""Reconstruct land-cover parcels from surveyed nodes and unordered edges.

The survey hand-off is topology only: numbered nodes with LOCAL_METERS
coordinates, boundary edges that reference two nodes, and parcels that list
the edge IDs they own. Edges are straight line segments between their node
positions; they are never snapped, buffered or supplemented with implicit
segments.

Every parcel's edge set must decompose into closed, simple loops with each used
node of degree two. Ring containment is resolved by strict containment depth
parity: even depth is a shell, odd depth a hole, even depth inside a hole an
independent island face. The resulting polygons are checked with the existing
parcel parser and same-layer positive-area overlap rule.

Rings are canonicalised (orientation, start position, ordering) so feature,
edge and endpoint order in the survey document cannot change the emitted
GeoJSON. The audit JSON keeps the source SHA256, the ring hierarchy and, for
every canonical segment, the edge identity, traversal direction and both
source positions.

Both outputs are written in a private sibling directory and the finished
directory is renamed into place only after both files are complete. Existing
targets are refused; failures leave no partial target and never touch inputs.
"""
import argparse
import hashlib
import json
import math
import os
import shutil
import tempfile
from collections import defaultdict
from pathlib import Path

from shapely.errors import GEOSException
from shapely.geometry import LinearRing, Point, Polygon, shape

from .source import parse_document, validate_layer

SCHEMA_VERSION = 1
MAX_COORDINATE = 1e6
DOCUMENT_KEYS = {"type", "coordinate_system", "nodes", "edges", "parcels"}
NODE_KEYS = {"id", "x", "y"}
EDGE_KEYS = {"id", "a", "b"}
PARCEL_KEYS = {"parcel_id", "class", "edge_ids"}

DEFINITIONS = {
    "source_sha256": (
        "SHA256 of the exact survey document bytes that were parsed. Parsing, "
        "reconstruction and this digest all use the same single read."
    ),
    "nodes": (
        "Declared survey nodes in id order. source_index is the zero-based "
        "position in the input nodes array and is retained for provenance only."
    ),
    "edges": (
        "Declared straight boundary segments in id order. a and b are node IDs, "
        "length_m is the Euclidean segment length, parcel_ids are the parcels "
        "that reference the edge and source_index is its input array position."
    ),
    "depth": (
        "Number of other loops of the same parcel whose polygon strictly "
        "contains this loop. Depth 0 is an outer shell, odd depth a hole and "
        "even depth above zero an island shell inside a hole."
    ),
    "role": (
        "shell for even containment depth (outer boundary or hole island), "
        "hole for odd depth. Every even-depth loop becomes its own polygon "
        "face together with the odd-depth loops it directly contains."
    ),
    "parent_ring_index": (
        "The directly containing loop (depth - 1), or null for an outer shell."
    ),
    "segments": (
        "Canonical loop traversal. from_node -> to_node follows the emitted "
        "ring order; direction is forward when that traversal matches the "
        "source edge's a -> b endpoints and reverse otherwise. "
        "edge_source_index, from_source_index and to_source_index are the "
        "zero-based positions in the input edges/nodes arrays."
    ),
    "canonical_rings": (
        "Shells are counter-clockwise and holes clockwise. Each ring is rotated "
        "to its lexicographically smallest coordinate rotation; rings, faces "
        "and parcels are ordered deterministically. Reordering input features "
        "or reversing edge endpoints therefore does not change "
        "parcels.geojson."
    ),
    "area_m2": (
        "GEOS planar area in LOCAL_METERS with no rounding, snapping or area "
        "threshold; loop areas are absolute ring areas."
    ),
}


class _DuplicateKey(ValueError):
    pass


def _object_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateKey(f"duplicate JSON key {key!r}")
        result[key] = value
    return result


def _reject_constant(text):
    raise _DuplicateKey(f"non-finite JSON number {text}")


def _decode(data, prefix):
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(f"{prefix}invalid JSON: not UTF-8: {exc}") from exc
    try:
        return json.loads(
            text, object_pairs_hook=_object_pairs, parse_constant=_reject_constant
        )
    except (json.JSONDecodeError, _DuplicateKey) as exc:
        raise ValueError(f"{prefix}invalid JSON: {exc}") from exc


def _exact_keys(value, expected, where):
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError(
            f"{where}must have exactly keys {sorted(expected)}"
        )


def _identity(value, where):
    if not isinstance(value, str) or not value:
        raise ValueError(f"{where}must be a nonempty string")
    return value


def _coordinate(value, where):
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ValueError(f"{where}must be a finite number")
    if abs(value) > MAX_COORDINATE:
        raise ValueError(f"{where}must be within +/-{MAX_COORDINATE:g}")
    return float(value)


def parse_network(document, prefix="network: "):
    """Validate a decoded SurveyNetwork and return its resolved records."""
    if not isinstance(document, dict) or document.get("type") != "SurveyNetwork":
        raise ValueError(f"{prefix}requires a SurveyNetwork document")
    if document.get("coordinate_system") != "LOCAL_METERS":
        raise ValueError(f"{prefix}requires explicit LOCAL_METERS coordinates")
    if set(document) != DOCUMENT_KEYS:
        raise ValueError(
            f"{prefix}document must have exactly keys {sorted(DOCUMENT_KEYS)}"
        )
    raw_nodes = document["nodes"]
    raw_edges = document["edges"]
    raw_parcels = document["parcels"]
    for name, value in (
        ("nodes", raw_nodes),
        ("edges", raw_edges),
        ("parcels", raw_parcels),
    ):
        if not isinstance(value, list):
            raise ValueError(f"{prefix}{name} must be an array")

    nodes = []
    node_by_id = {}
    node_ids = set()
    for index, record in enumerate(raw_nodes):
        where = f"{prefix}node {index}: "
        _exact_keys(record, NODE_KEYS, where)
        node_id = _identity(record["id"], f"{where}id ")
        if node_id in node_ids:
            raise ValueError(f"{where}duplicate node id {node_id!r}")
        node_ids.add(node_id)
        node = {
            "id": node_id,
            "x": _coordinate(record["x"], f"{where}x"),
            "y": _coordinate(record["y"], f"{where}y"),
            "source_index": index,
        }
        nodes.append(node)
        node_by_id[node_id] = node

    edges = []
    edge_by_id = {}
    edge_ids = set()
    endpoint_pairs = {}
    for index, record in enumerate(raw_edges):
        where = f"{prefix}edge {index}: "
        _exact_keys(record, EDGE_KEYS, where)
        edge_id = _identity(record["id"], f"{where}id ")
        if edge_id in edge_ids:
            raise ValueError(f"{where}duplicate edge id {edge_id!r}")
        edge_ids.add(edge_id)
        left = _identity(record["a"], f"{where}a ")
        right = _identity(record["b"], f"{where}b ")
        if left not in node_by_id:
            raise ValueError(f"{where}references unknown node {left!r}")
        if right not in node_by_id:
            raise ValueError(f"{where}references unknown node {right!r}")
        if left == right:
            raise ValueError(f"{where}is a zero-length edge (same node at both ends)")
        left_node = node_by_id[left]
        right_node = node_by_id[right]
        length = math.hypot(
            right_node["x"] - left_node["x"], right_node["y"] - left_node["y"]
        )
        if length <= 0:
            raise ValueError(
                f"{where}is zero-length because its two nodes share coordinates"
            )
        pair = (left, right) if left < right else (right, left)
        if pair in endpoint_pairs:
            raise ValueError(
                f"{where}duplicates the boundary between nodes {pair[0]!r} and "
                f"{pair[1]!r} (edge {endpoint_pairs[pair]!r})"
            )
        endpoint_pairs[pair] = edge_id
        edge = {
            "id": edge_id,
            "a": left,
            "b": right,
            "length_m": length,
            "source_index": index,
        }
        edges.append(edge)
        edge_by_id[edge_id] = edge

    parcels = []
    parcel_ids = set()
    usage = defaultdict(list)
    for index, record in enumerate(raw_parcels):
        where = f"{prefix}parcel {index}: "
        _exact_keys(record, PARCEL_KEYS, where)
        parcel_id = _identity(record["parcel_id"], f"{where}parcel_id ")
        if parcel_id in parcel_ids:
            raise ValueError(f"{where}duplicate parcel_id {parcel_id!r}")
        parcel_ids.add(parcel_id)
        klass = _identity(record["class"], f"{where}class ")
        edge_ids = record["edge_ids"]
        if not isinstance(edge_ids, list) or not edge_ids:
            raise ValueError(f"{where}edge_ids must be a nonempty array")
        seen = set()
        resolved = []
        for position, edge_id in enumerate(edge_ids):
            edge_id = _identity(edge_id, f"{where}edge_ids[{position}] ")
            if edge_id in seen:
                raise ValueError(
                    f"{where}references edge {edge_id!r} more than once"
                )
            seen.add(edge_id)
            if edge_id not in edge_by_id:
                raise ValueError(
                    f"{where}references unknown edge {edge_id!r}"
                )
            usage[edge_id].append(parcel_id)
            resolved.append(edge_id)
        parcels.append(
            {
                "parcel_id": parcel_id,
                "class": klass,
                "edge_ids": resolved,
                "source_index": index,
            }
        )

    for edge in edges:
        owners = usage.get(edge["id"], [])
        if len(owners) > 2:
            raise ValueError(
                f"{prefix}edge {edge['id']!r} belongs to more than two parcels "
                f"({', '.join(sorted(owners))})"
            )
        edge["parcel_ids"] = sorted(owners)

    return {
        "nodes": nodes,
        "node_by_id": node_by_id,
        "edges": edges,
        "edge_by_id": edge_by_id,
        "edge_by_pair": endpoint_pairs,
        "parcels": parcels,
    }


def _other_endpoint(edge, node_id):
    return edge["b"] if edge["a"] == node_id else edge["a"]


def _recover_cycles(parcel, edge_by_id):
    """Traverse a parcel's edges into simple input-order loops."""
    where = f"parcel {parcel['parcel_id']!r}: "
    adjacency = defaultdict(list)
    for edge_id in parcel["edge_ids"]:
        edge = edge_by_id[edge_id]
        adjacency[edge["a"]].append(edge_id)
        adjacency[edge["b"]].append(edge_id)
    for node_id in sorted(adjacency):
        degree = len(adjacency[node_id])
        if degree != 2:
            raise ValueError(
                f"{where}node {node_id!r} has degree {degree}, expected 2"
            )

    visited = set()
    cycles = []
    for first_edge_id in sorted(parcel["edge_ids"]):
        if first_edge_id in visited:
            continue
        current_edge = edge_by_id[first_edge_id]
        start_node = current_edge["a"]
        current_node = start_node
        node_sequence = []
        edge_sequence = []
        while True:
            if current_edge["id"] in visited:
                raise ValueError(
                    f"{where}edge {current_edge['id']!r} is used by two loops"
                )
            visited.add(current_edge["id"])
            edge_sequence.append(current_edge["id"])
            node_sequence.append(current_node)
            next_node = _other_endpoint(current_edge, current_node)
            if next_node == start_node:
                break
            if next_node in node_sequence:
                raise ValueError(
                    f"{where}loop visits node {next_node!r} twice"
                )
            candidates = [
                edge_id
                for edge_id in adjacency[next_node]
                if edge_id != current_edge["id"]
            ]
            if len(candidates) != 1:
                raise ValueError(
                    f"{where}node {next_node!r} does not continue one loop"
                )
            current_edge = edge_by_id[candidates[0]]
            current_node = next_node
        if len(node_sequence) < 3:
            raise ValueError(
                f"{where}loop has fewer than three distinct nodes"
            )
        cycles.append({"node_ids": node_sequence, "edge_ids": edge_sequence})

    if len(visited) != len(parcel["edge_ids"]):
        raise ValueError(f"{where}edge set does not decompose into closed loops")
    return cycles


def _signed_area2(coordinates):
    total = 0.0
    for index in range(len(coordinates)):
        x1, y1 = coordinates[index]
        x2, y2 = coordinates[(index + 1) % len(coordinates)]
        total += x1 * y2 - x2 * y1
    return total


def _smallest_rotation(node_ids, node_by_id):
    coordinates = [(node_by_id[node_id]["x"], node_by_id[node_id]["y"]) for node_id in node_ids]
    count = len(node_ids)
    best = None
    best_key = None
    for offset in range(count):
        rotated_nodes = tuple(node_ids[offset:] + node_ids[:offset])
        rotated_coords = tuple(coordinates[offset:] + coordinates[:offset])
        key = (rotated_coords, rotated_nodes)
        if best_key is None or key < best_key:
            best_key = key
            best = offset
    return node_ids[best:] + node_ids[:best]


def _canonical_loop(node_ids, node_by_id, shell):
    """Orient, rotate and close one loop without snapping its coordinates."""
    sequence = list(node_ids)
    coordinates = [
        (node_by_id[node_id]["x"], node_by_id[node_id]["y"]) for node_id in sequence
    ]
    area2 = _signed_area2(coordinates)
    if area2 == 0:
        raise ValueError("loop has zero area")
    if (shell and area2 < 0) or (not shell and area2 > 0):
        sequence.reverse()
    sequence = _smallest_rotation(sequence, node_by_id)
    ring_coordinates = [
        (node_by_id[node_id]["x"], node_by_id[node_id]["y"]) for node_id in sequence
    ]
    ring_coordinates.append(ring_coordinates[0])
    try:
        ring = LinearRing(ring_coordinates)
    except (GEOSException, ValueError) as exc:
        raise ValueError(f"malformed loop: {exc}") from exc
    if not ring.is_simple:
        raise ValueError("loop self-intersects or touches itself")
    polygon = Polygon(ring_coordinates)
    if polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
        raise ValueError("loop is not a valid positive-area ring")
    return {
        "node_ids": sequence,
        "coordinates": ring_coordinates,
        "ring": ring,
        "polygon": polygon,
        "area_m2": polygon.area,
    }


def _loop_key(loop):
    return tuple(loop["coordinates"])


def _positions(ring_coordinates):
    """Ring coordinates as JSON-ready positions without changing the numbers."""
    return [[x, y] for x, y in ring_coordinates]


def _reconstruct(parcel, nodes_by_id, edge_by_id):
    """Build canonical ring/face records and emitted GeoJSON for one parcel."""
    where = f"parcel {parcel['parcel_id']!r}: "
    cycles = _recover_cycles(parcel, edge_by_id)

    # A first pass establishes the pure geometric nesting depth. Ring
    # orientation is not involved in containment.
    raw = []
    for cycle in cycles:
        coordinates = [
            (nodes_by_id[node_id]["x"], nodes_by_id[node_id]["y"])
            for node_id in cycle["node_ids"]
        ]
        try:
            ring = LinearRing(coordinates + [coordinates[0]])
        except (GEOSException, ValueError) as exc:
            raise ValueError(f"{where}malformed loop: {exc}") from exc
        if not ring.is_simple:
            raise ValueError(f"{where}loop self-intersects or touches itself")
        polygon = Polygon(coordinates)
        if polygon.is_empty or not polygon.is_valid or polygon.area <= 0:
            raise ValueError(f"{where}loop is not a valid positive-area ring")
        raw.append({"cycle": cycle, "ring": ring, "polygon": polygon})

    for left in range(len(raw)):
        for right in range(left + 1, len(raw)):
            try:
                intersection = raw[left]["ring"].intersection(raw[right]["ring"])
            except GEOSException as exc:
                raise ValueError(
                    f"{where}loop intersection failed: {exc}"
                ) from exc
            if not intersection.is_empty:
                raise ValueError(
                    f"{where}loops touch or cross each other "
                    f"(loops {left} and {right})"
                )

    depths = []
    for index, item in enumerate(raw):
        depth = 0
        for other_index, other in enumerate(raw):
            if other_index == index:
                continue
            if other["polygon"].contains(Point(item["ring"].coords[0])):
                depth += 1
        depths.append(depth)

    loops = []
    for index, item in enumerate(raw):
        shell = depths[index] % 2 == 0
        try:
            loop = _canonical_loop(
                item["cycle"]["node_ids"], nodes_by_id, shell
            )
        except ValueError as exc:
            raise ValueError(f"{where}{exc}") from exc
        loop["depth"] = depths[index]
        loop["edge_ids"] = item["cycle"]["edge_ids"]
        loops.append(loop)

    loops.sort(key=_loop_key)
    for index, loop in enumerate(loops):
        loop["ring_index"] = index

    parents = []
    for index, loop in enumerate(loops):
        if loop["depth"] == 0:
            parents.append(None)
            continue
        container = Point(loop["ring"].coords[0])
        candidates = [
            other_index
            for other_index, other in enumerate(loops)
            if other_index != index
            and other["depth"] == loop["depth"] - 1
            and other["polygon"].contains(container)
        ]
        if len(candidates) != 1:
            raise ValueError(
                f"{where}loop {index} has an ambiguous containment hierarchy"
            )
        parents.append(candidates[0])
    for index, loop in enumerate(loops):
        loop["parent_ring_index"] = parents[index]

    faces = []
    for index, loop in enumerate(loops):
        if loop["depth"] % 2 != 0:
            continue
        holes = [
            other_index
            for other_index, other in enumerate(loops)
            if other["depth"] % 2 == 1
            and parents[other_index] == index
        ]
        try:
            face = Polygon(
                loop["coordinates"],
                [loops[hole]["coordinates"] for hole in holes],
            )
        except (GEOSException, ValueError) as exc:
            raise ValueError(f"{where}malformed face: {exc}") from exc
        if face.is_empty or not face.is_valid or face.area <= 0:
            raise ValueError(f"{where}face is not a valid positive-area polygon")
        faces.append({"shell": index, "holes": holes, "polygon": face})

    if not faces:
        raise ValueError(f"{where}has no positive-area shell")
    if len(faces) == 1:
        geometry_json = {
            "type": "Polygon",
            "coordinates": [_positions(loops[faces[0]["shell"]]["coordinates"])]
            + [
                _positions(loops[hole]["coordinates"])
                for hole in faces[0]["holes"]
            ],
        }
        geometry = faces[0]["polygon"]
    else:
        geometry_json = {
            "type": "MultiPolygon",
            "coordinates": [
                [_positions(loops[face["shell"]]["coordinates"])]
                + [
                    _positions(loops[hole]["coordinates"])
                    for hole in face["holes"]
                ]
                for face in faces
            ],
        }
        try:
            geometry = shape(geometry_json)
        except (GEOSException, ValueError, TypeError) as exc:
            raise ValueError(f"{where}malformed multipolygon: {exc}") from exc
        if geometry.is_empty or not geometry.is_valid or geometry.area <= 0:
            raise ValueError(f"{where}is not a valid MultiPolygon")
    return {
        "loops": loops,
        "faces": faces,
        "geometry": geometry,
        "geometry_json": geometry_json,
        "area_m2": geometry.area,
    }


def _segment_records(loop, parcel_edge_ids, edge_by_pair, edge_by_id, node_by_id):
    node_ids = loop["node_ids"]
    records = []
    for index, from_node in enumerate(node_ids):
        to_node = node_ids[(index + 1) % len(node_ids)]
        pair = (from_node, to_node) if from_node < to_node else (to_node, from_node)
        edge_id = edge_by_pair.get(pair)
        if edge_id is None or edge_id not in parcel_edge_ids:
            raise ValueError(
                f"canonical loop segment {from_node!r}->{to_node!r} does not "
                "match exactly one declared edge"
            )
        edge = edge_by_id[edge_id]
        records.append(
            {
                "edge_id": edge["id"],
                "from_node": from_node,
                "to_node": to_node,
                "direction": (
                    "forward"
                    if edge["a"] == from_node and edge["b"] == to_node
                    else "reverse"
                ),
                "edge_source_index": edge["source_index"],
                "from_source_index": node_by_id[from_node]["source_index"],
                "to_source_index": node_by_id[to_node]["source_index"],
            }
        )
    return records


def _render(records, node_by_id, edge_by_id, edge_by_pair):
    features = []
    audit_parcels = []
    for parcel in sorted(records, key=lambda item: item["parcel_id"]):
        result = parcel["result"]
        parcel_edge_id_set = set(parcel["edge_ids"])
        features.append(
            {
                "type": "Feature",
                "properties": {
                    "parcel_id": parcel["parcel_id"],
                    "class": parcel["class"],
                },
                "geometry": result["geometry_json"],
            }
        )
        ring_records = []
        for loop in result["loops"]:
            ring_records.append(
                {
                    "ring_index": loop["ring_index"],
                    "depth": loop["depth"],
                    "role": "shell" if loop["depth"] % 2 == 0 else "hole",
                    "parent_ring_index": loop["parent_ring_index"],
                    "area_m2": loop["area_m2"],
                    "node_ids": list(loop["node_ids"]),
                    "segments": _segment_records(
                        loop,
                        parcel_edge_id_set,
                        edge_by_pair,
                        edge_by_id,
                        node_by_id,
                    ),
                }
            )
        audit_parcels.append(
            {
                "parcel_id": parcel["parcel_id"],
                "class": parcel["class"],
                "source_index": parcel["source_index"],
                "edge_ids": list(parcel["edge_ids"]),
                "geometry_type": result["geometry_json"]["type"],
                "area_m2": result["area_m2"],
                "ring_count": len(result["loops"]),
                "face_count": len(result["faces"]),
                "rings": ring_records,
                "faces": [
                    {
                        "shell_ring_index": face["shell"],
                        "hole_ring_indices": list(face["holes"]),
                    }
                    for face in result["faces"]
                ],
            }
        )
    return features, audit_parcels


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


def acquire(network_path, output):
    """Reconstruct ``parcels.geojson`` and the per-edge audit for one survey.

    The network/source bytes are read exactly once and both parsed and hashed
    from that same value. Validation happens before any output is created.
    Both artifacts are written into a private temporary directory and published
    as one new directory only after both files are complete.
    """
    network_path = Path(network_path)
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    try:
        source_bytes = network_path.read_bytes()
    except OSError as exc:
        raise OSError(f"network input unreadable: {exc}") from exc
    network = parse_network(_decode(source_bytes, "network: "))
    node_by_id = network["node_by_id"]
    edge_by_id = network["edge_by_id"]
    edge_by_pair = network["edge_by_pair"]

    records = []
    for parcel in network["parcels"]:
        try:
            result = _reconstruct(parcel, node_by_id, edge_by_id)
        except ValueError as exc:
            if str(exc).startswith("parcel "):
                raise
            raise ValueError(f"parcel {parcel['parcel_id']!r}: {exc}") from exc
        records.append({**parcel, "result": result})

    features, audit_parcels = _render(
        records, node_by_id, edge_by_id, edge_by_pair
    )
    collection = {
        "type": "FeatureCollection",
        "coordinate_system": "LOCAL_METERS",
        "features": features,
    }
    # Reuse the normal parcel parser and same-layer overlap rule, so anything
    # published here is directly consumable by the existing entries.
    parsed_features = parse_document(collection, "parcel", "acquisition: ")
    validate_layer(parsed_features, "parcel", "acquisition: ")

    audit = {
        "schema_version": SCHEMA_VERSION,
        "mode": "acquisition",
        "coordinate_system": "LOCAL_METERS",
        "source_sha256": hashlib.sha256(source_bytes).hexdigest(),
        "source": {
            "nodes": len(network["nodes"]),
            "edges": len(network["edges"]),
            "parcels": len(network["parcels"]),
        },
        "definitions": DEFINITIONS,
        "nodes": [
            {
                "id": node["id"],
                "x": node["x"],
                "y": node["y"],
                "source_index": node["source_index"],
            }
            for node in sorted(network["nodes"], key=lambda item: item["id"])
        ],
        "edges": [
            {
                "id": edge["id"],
                "a": edge["a"],
                "b": edge["b"],
                "length_m": edge["length_m"],
                "parcel_ids": list(edge["parcel_ids"]),
                "source_index": edge["source_index"],
            }
            for edge in sorted(network["edges"], key=lambda item: item["id"])
        ],
        "parcels": audit_parcels,
    }

    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".landcover-acquisition-", dir=parent))
    publish = work / "publish"
    publish.mkdir()
    try:
        _write_json(publish / "parcels.geojson", collection)
        _write_json(publish / "acquisition_audit.json", audit)
        if output.exists():
            raise FileExistsError(f"output already exists: {output}")
        publish.rename(output)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    shutil.rmtree(work, ignore_errors=True)
    return audit


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Reconstruct parcels and a per-edge audit from surveyed nodes and "
            "unordered boundary edges"
        )
    )
    parser.add_argument(
        "--network",
        type=Path,
        default=Path("data/acquisition_network.json"),
        help="SurveyNetwork JSON document",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("run/acquisition"),
        help="new directory for parcels.geojson and acquisition_audit.json",
    )
    args = parser.parse_args(argv)
    try:
        audit = acquire(args.network, args.output)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"acquisition failed: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "acquisition interrupted\n")
    except Exception as exc:
        parser.exit(1, f"acquisition failed: {type(exc).__name__}: {exc}\n")
    print(
        json.dumps(
            {
                parcel["parcel_id"]: parcel["area_m2"]
                for parcel in audit["parcels"]
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
