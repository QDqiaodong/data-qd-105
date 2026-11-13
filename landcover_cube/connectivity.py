"""Independent connectivity-vulnerability analysis for target-class land cover.

Each statistical zone gets its own graph. A node is one positive-area Polygon
component of a target-class parcel clipped to that zone; holes are retained and
components of the same parcel are never joined implicitly. Two nodes are linked
only when their true shared boundary has linear length at least the threshold;
point-only contact has zero shared-edge length and creates no edge.

Vulnerability removes all nodes of one parcel in one zone at a time, reports the
remaining connected components and the largest remaining component, and ranks
parcels by relative loss of that maximum. The report is a single deterministic
JSON file containing canonical node geometry, per-edge shared-boundary evidence
and the input SHA256 digests, so the calculation can be reproduced independently.
"""
import argparse
import hashlib
import json
import math
import os
import re
import tempfile
from decimal import Decimal, InvalidOperation
from pathlib import Path

from shapely import STRtree, normalize, union_all
from shapely.geometry import mapping

from .overlay import polygonal
from .source import load_input

SCHEMA_VERSION = 1
_DECIMAL_TEXT = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")

DEFINITIONS = {
    "node_id": (
        "Length-prefixed tuple text '<len>:<zone_id>|<len>:<parcel_id>|"
        "<component_index>'. It is unambiguous even when IDs contain '|' and "
        "is stable because components are ordered by normalized geometry."
    ),
    "node": (
        "For one zone, a target-class parcel is clipped to that zone; every "
        "positive-area Polygon component of the clip is one node. Holes are "
        "retained. Components of the same parcel are separate nodes and are "
        "never connected implicitly."
    ),
    "edge": (
        "An edge exists only when the total linear length of the true shared "
        "boundary of two nodes is greater than or equal to "
        "shared_edge_threshold_m. Boundary intersections are restricted to "
        "LineString parts, so point contact contributes zero length and no "
        "edge. Hole boundaries are real boundaries."
    ),
    "graph": (
        "Every statistical zone has its own graph. Nodes and edges never cross "
        "between zones."
    ),
    "shared_boundary_length_m": (
        "Length of the union of LineString pieces in the intersection of the "
        "two node boundaries."
    ),
    "shared_edge_comparison": (
        "The threshold is compared with the shortest round-trip decimal text "
        "of the GEOS float shared-boundary length, using exact decimal "
        "comparison; equality connects. This is the same float length reported "
        "as shared_boundary_length_m, so re-deriving length from the encoded "
        "geometry reproduces the edge decision."
    ),
    "baseline_maximum_component_area_m2": (
        "Largest sum of node areas in one connected component before any "
        "parcel removal; 0 when the zone has no target-class nodes."
    ),
    "vulnerability": (
        "For each parcel represented in a zone, every node of that parcel in "
        "that zone is removed simultaneously; the remaining graph components "
        "are recomputed from the edges that remain."
    ),
    "absolute_loss_m2": (
        "Baseline maximum component area minus maximum remaining component "
        "area after removing one parcel; 0 for an unchanged or empty maximum."
    ),
    "relative_loss": (
        "absolute_loss_m2 divided by baseline maximum component area. It is 0 "
        "when the baseline has zero area."
    ),
    "ranking": (
        "Per zone, vulnerability entries are ordered by relative_loss "
        "descending, then parcel_id ascending."
    ),
    "shared_boundary_checks": (
        "Unordered node pairs returned by the bounding-box candidate index for "
        "one zone and exact-tested once; point-only bbox contacts are included "
        "here but contribute no edge."
    ),
    "geometry_encoding": (
        "Node and shared-boundary geometries are canonicalized with GEOS "
        "normalization and encoded as GeoJSON in LOCAL_METERS; no rounding or "
        "snapping is applied."
    ),
}
CAVEAT = (
    "Geometric boundary connectivity is a landscape-structure indicator only. "
    "A shared edge does not guarantee species passage, habitat quality or "
    "functional ecological connectivity."
)


def parse_shared_edge_threshold(value):
    """Return a positive finite Decimal threshold; reject bools and non-decimals."""
    if isinstance(value, bool):
        raise ValueError(
            "shared-edge threshold must be a positive finite decimal number"
        )
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(
                "shared-edge threshold must be a positive finite decimal number"
            )
        parsed = Decimal(repr(value))
    elif isinstance(value, str):
        if not _DECIMAL_TEXT.fullmatch(value):
            raise ValueError(
                "shared-edge threshold must be a positive finite decimal number"
            )
        try:
            parsed = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(
                "shared-edge threshold must be a positive finite decimal number"
            ) from exc
    else:
        raise ValueError(
            "shared-edge threshold must be a positive finite decimal number"
        )
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError(
            "shared-edge threshold must be a positive finite decimal number"
        )
    return parsed


def _threshold_text(size):
    """Canonical text for a validated threshold, avoiding enormous fixed forms."""
    normalized = size.normalize()
    if normalized == 0:
        return "0"
    adjusted = normalized.adjusted()
    if -20 <= adjusted <= 20:
        if normalized == normalized.to_integral_value():
            return format(normalized.to_integral_value(), "f")
        return format(normalized, "f")
    return str(normalized)


def _node_id(zone_id, parcel_id, component_index):
    """Unambiguous node identity even when IDs contain the '|' separator."""
    return (
        f"{len(zone_id)}:{zone_id}|{len(parcel_id)}:{parcel_id}"
        f"|{component_index}"
    )


def _geometry_key(geometry):
    """Deterministic component order key independent of input feature order."""
    return (geometry.bounds, geometry.area, geometry.wkt)


def _components(geometry):
    """Positive-area Polygon components as canonical geometries, holes retained."""
    polygonal_geometry = polygonal(geometry)
    if polygonal_geometry.is_empty:
        return []
    if polygonal_geometry.geom_type == "Polygon":
        candidates = [polygonal_geometry]
    elif polygonal_geometry.geom_type == "MultiPolygon":
        candidates = list(polygonal_geometry.geoms)
    else:
        return []
    result = []
    for component in candidates:
        if component.is_empty or component.area <= 0:
            continue
        normalized = normalize(component)
        if normalized.is_empty or normalized.area <= 0:
            continue
        result.append(normalized)
    result.sort(key=_geometry_key)
    return result


def _linear_parts(geometry):
    """LineString pieces of a geometry, recursively, ignoring points and areas."""
    if geometry.is_empty:
        return []
    kind = geometry.geom_type
    if kind in {"LineString", "LinearRing"}:
        return [geometry]
    if hasattr(geometry, "geoms"):
        result = []
        for child in geometry.geoms:
            result.extend(_linear_parts(child))
        return result
    return []


def _shared_boundary(left, right):
    """Canonical union of line parts of the two boundaries; None if none."""
    try:
        intersection = left.boundary.intersection(right.boundary)
    except Exception as exc:
        raise ValueError(f"shared-boundary computation failed: {exc}") from exc
    parts = _linear_parts(intersection)
    if not parts:
        return None
    shared = normalize(union_all(parts))
    if shared.is_empty:
        return None
    return shared


def _zone_graph(zone, parcels, target_class, threshold):
    zone_id = zone["zone_id"]
    zone_geometry = zone["geometry"]
    nodes = []
    for parcel in sorted(parcels, key=lambda item: item["parcel_id"]):
        if parcel["class"] != target_class:
            continue
        parcel_id = parcel["parcel_id"]
        try:
            clipped = zone_geometry.intersection(parcel["geometry"])
        except Exception as exc:
            raise ValueError(
                f"zone {zone_id!r} parcel {parcel_id!r} clip failed: {exc}"
            ) from exc
        for component_index, component in enumerate(_components(clipped)):
            nodes.append(
                {
                    "node_id": _node_id(zone_id, parcel_id, component_index),
                    "parcel_id": parcel_id,
                    "class": target_class,
                    "component_index": component_index,
                    "area_m2": component.area,
                    "geometry": component,
                }
            )

    tree = STRtree([node["geometry"] for node in nodes])
    candidate_pairs = set()
    for index, node in enumerate(nodes):
        for hit in tree.query(node["geometry"]):
            other = int(hit)
            if other > index:
                candidate_pairs.add((index, other))
            elif other < index:
                candidate_pairs.add((other, index))

    edges = []
    for left_index, right_index in sorted(candidate_pairs):
        left = nodes[left_index]
        right = nodes[right_index]
        shared = _shared_boundary(left["geometry"], right["geometry"])
        if shared is None:
            continue
        length = shared.length
        if length <= 0:
            continue
        if Decimal(repr(length)) < threshold:
            continue
        edges.append(
            {
                "node_a": left["node_id"],
                "node_b": right["node_id"],
                "parcel_a": left["parcel_id"],
                "parcel_b": right["parcel_id"],
                "shared_boundary_length_m": length,
                "shared_boundary": mapping(shared),
            }
        )
    return nodes, edges, len(candidate_pairs)


def _component_records(indices, edges, nodes):
    """Connected components of the induced graph, ordered by smallest node index."""
    remaining = set(indices)
    index_by_id = {
        nodes[index]["node_id"]: index for index in remaining
    }
    adjacency = {index: [] for index in remaining}
    for edge in edges:
        left = index_by_id.get(edge["node_a"])
        right = index_by_id.get(edge["node_b"])
        if left is None or right is None:
            continue
        adjacency[left].append(right)
        adjacency[right].append(left)
    visited = set()
    components = []
    for start in sorted(remaining):
        if start in visited:
            continue
        stack = [start]
        visited.add(start)
        members = []
        while stack:
            current = stack.pop()
            members.append(current)
            for neighbour in adjacency[current]:
                if neighbour not in visited:
                    visited.add(neighbour)
                    stack.append(neighbour)
        members.sort()
        components.append(
            {
                "node_ids": [nodes[index]["node_id"] for index in members],
                "parcel_ids": sorted({nodes[index]["parcel_id"] for index in members}),
                "node_count": len(members),
                "area_m2": math.fsum(nodes[index]["area_m2"] for index in members),
            }
        )
    components.sort(key=lambda component: component["node_ids"][0])
    return components


def analyze(zones, parcels, target_class, threshold, input_hashes):
    """Build the deterministic connectivity report from validated features."""
    if not isinstance(target_class, str) or not target_class:
        raise ValueError("target class must be a nonempty string")
    if not isinstance(threshold, Decimal):
        threshold = parse_shared_edge_threshold(threshold)
    zone_reports = []
    for zone in sorted(zones, key=lambda item: item["zone_id"]):
        zone_id = zone["zone_id"]
        nodes, edges, checks = _zone_graph(zone, parcels, target_class, threshold)
        baseline_components = _component_records(
            range(len(nodes)), edges, nodes
        )
        baseline_max = max(
            (component["area_m2"] for component in baseline_components), default=0.0
        )

        vulnerability = []
        for parcel_id in sorted({node["parcel_id"] for node in nodes}):
            removed = [
                index
                for index, node in enumerate(nodes)
                if node["parcel_id"] == parcel_id
            ]
            removed_set = set(removed)
            remaining_indices = [
                index for index in range(len(nodes)) if index not in removed_set
            ]
            remaining_components = _component_records(
                remaining_indices, edges, nodes
            )
            maximum_remaining = max(
                (component["area_m2"] for component in remaining_components),
                default=0.0,
            )
            absolute_loss = max(0.0, baseline_max - maximum_remaining)
            relative_loss = (
                absolute_loss / baseline_max if baseline_max > 0 else 0.0
            )
            vulnerability.append(
                {
                    "parcel_id": parcel_id,
                    "removed_node_ids": [
                        nodes[index]["node_id"] for index in removed
                    ],
                    "removed_area_m2": math.fsum(
                        nodes[index]["area_m2"] for index in removed
                    ),
                    "remaining_node_count": len(remaining_indices),
                    "remaining_component_count": len(remaining_components),
                    "remaining_components": remaining_components,
                    "maximum_remaining_component_area_m2": maximum_remaining,
                    "absolute_loss_m2": absolute_loss,
                    "relative_loss": relative_loss,
                }
            )
        vulnerability.sort(
            key=lambda item: (-item["relative_loss"], item["parcel_id"])
        )
        for rank, item in enumerate(vulnerability, 1):
            item["rank"] = rank

        zone_reports.append(
            {
                "zone_id": zone_id,
                "zone_area_m2": zone["geometry"].area,
                "target_class": target_class,
                "target_area_m2": math.fsum(node["area_m2"] for node in nodes),
                "node_count": len(nodes),
                "edge_count": len(edges),
                "shared_boundary_checks": checks,
                "nodes": [
                    {
                        "node_id": node["node_id"],
                        "parcel_id": node["parcel_id"],
                        "class": node["class"],
                        "component_index": node["component_index"],
                        "area_m2": node["area_m2"],
                        "geometry": mapping(node["geometry"]),
                    }
                    for node in nodes
                ],
                "edges": edges,
                "baseline": {
                    "node_count": len(nodes),
                    "edge_count": len(edges),
                    "component_count": len(baseline_components),
                    "maximum_component_area_m2": baseline_max,
                    "components": baseline_components,
                },
                "vulnerability": vulnerability,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "mode": "connectivity",
        "coordinate_system": "LOCAL_METERS",
        "target_class": target_class,
        "shared_edge_threshold_m": _threshold_text(threshold),
        "input_sha256": {key: input_hashes[key] for key in ("zones", "parcels")},
        "feature_count": {"zones": len(zones), "parcels": len(parcels)},
        "definitions": DEFINITIONS,
        "caveat": CAVEAT,
        "zone_count": len(zone_reports),
        "zones": zone_reports,
    }


def _write_report(report, output):
    """Atomically replace the report; any earlier report survives a failure."""
    output = Path(output)
    if output.exists() and output.is_dir():
        raise ValueError(f"output must be a JSON file path, not a directory: {output}")
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{output.name}.", suffix=".tmp", dir=parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(report, sort_keys=True, indent=2, allow_nan=False) + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, output)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def connectivity(
    zones_path, parcels_path, target_class, shared_edge_threshold, output
):
    """Analyze connectivity and write one JSON report to ``output``.

    Both inputs are fully validated before computation. Existing output is
    replaced only after a complete report has been written successfully.
    """
    if not isinstance(target_class, str) or not target_class:
        raise ValueError("target class must be a nonempty string")
    threshold = parse_shared_edge_threshold(shared_edge_threshold)
    try:
        zone_bytes, zones, _ = load_input(zones_path, "zone", "zones: ")
    except OSError as exc:
        raise OSError(f"zones input unreadable: {exc}") from exc
    try:
        parcel_bytes, parcels, _ = load_input(parcels_path, "parcel", "parcels: ")
    except OSError as exc:
        raise OSError(f"parcels input unreadable: {exc}") from exc
    input_hashes = {
        "zones": hashlib.sha256(zone_bytes).hexdigest(),
        "parcels": hashlib.sha256(parcel_bytes).hexdigest(),
    }
    report = analyze(zones, parcels, target_class, threshold, input_hashes)
    _write_report(report, output)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Analyze target-class connectivity vulnerability per statistical zone"
        )
    )
    parser.add_argument("--zones", type=Path, default=Path("data/zones.geojson"))
    parser.add_argument("--parcels", type=Path, default=Path("data/parcels.geojson"))
    parser.add_argument(
        "--class",
        "--target-class",
        dest="target_class",
        required=True,
        help="target land-cover class",
    )
    parser.add_argument(
        "--threshold",
        "--shared-edge-threshold",
        dest="threshold",
        required=True,
        help="positive finite shared-edge length threshold in meters",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("run/connectivity/connectivity.json"),
        help="JSON report file path",
    )
    args = parser.parse_args(argv)
    try:
        report = connectivity(
            args.zones, args.parcels, args.target_class, args.threshold, args.output
        )
    except (ValueError, OSError) as exc:
        parser.exit(2, f"connectivity failed: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "connectivity interrupted\n")
    except Exception as exc:
        parser.exit(1, f"connectivity failed: {type(exc).__name__}: {exc}\n")
    print(
        json.dumps(
            {
                zone["zone_id"]: zone["baseline"]["maximum_component_area_m2"]
                for zone in report["zones"]
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
