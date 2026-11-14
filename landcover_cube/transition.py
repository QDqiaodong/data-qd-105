"""Two-period land-cover transition geometry, area matrix and SQLite store.

One shared statistical-zone collection is overlaid with a before-period and
an after-period parcel collection. All three inputs are fully validated
under the existing planar contract before any computation. Transitions are
purely spatial: parcel identities are never paired across periods. Inside
one zone, all positive-area geometry of one (before coverage_kind/class,
after coverage_kind/class) combination is merged into a single
Polygon/MultiPolygon, retaining holes and disconnected components.
Uncovered area uses coverage_kind=unclassified with a null class, so a real
class named 'unclassified' is never mixed with genuinely uncovered area.
Unchanged combinations (forest -> forest) are ordinary cells and are kept.

Per zone the matrix rows are the before-period categories and the columns
the after-period categories; row totals equal the before-period category
areas, column totals the after-period category areas, and the grand total
conserves the zone area within the baseline rel 1e-9 / abs 1e-8 m2
tolerance. transitions.geojson, matrix.json and transitions.sqlite are
built from the same merged facts and all carry the three input SHA256
digests.

The target directory must not exist. All outputs are written in a private
sibling directory and published as one new directory only when complete;
invalid input or a write failure leaves no partial target and never
modifies the inputs.
"""
import argparse
import hashlib
import json
import math
import os
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from pathlib import Path

from shapely import STRtree, union_all
from shapely.geometry import mapping

from .overlay import polygonal
from .source import load_input

SCHEMA_VERSION = 1

DEFINITIONS = {
    "category": (
        "For one period and one zone, the union of all clipped parcel area "
        "with the same class is one ('classified', class) category and all "
        "uncovered zone area is the single ('unclassified', null) category. "
        "A real class literally named 'unclassified' stays a classified "
        "category and is never mixed with the null-class uncovered category."
    ),
    "transition": (
        "The positive-area intersection of one before-period category "
        "geometry with one after-period category geometry in the same zone, "
        "merged into one Polygon/MultiPolygon per (zone, before category, "
        "after category) combination. Holes and disconnected components are "
        "retained. The computation is purely spatial: parcel identities are "
        "never paired across periods, and unchanged combinations (the same "
        "class in both periods) are ordinary cells."
    ),
    "matrix": (
        "Per zone, rows are the before-period categories and columns the "
        "after-period categories, both ordered by (coverage_kind, class) "
        "with classified first. areas_m2 is dense: combinations without "
        "positive-area geometry are 0 and have no transition fact. "
        "row_totals_m2 are the before-period category areas and "
        "column_totals_m2 the after-period category areas; total_m2 "
        "conserves the zone area within rel 1e-9 / abs 1e-8 m2."
    ),
    "transition_id": (
        "Stable 1-based order over (zone_id, before category, after "
        "category): zone_id ascending, then the before category, then the "
        "after category. Only positive-area facts receive an ID; the same "
        "ID is the SQLite transitions primary key and a GeoJSON property."
    ),
    "geometry_encoding": (
        "Transition geometries are GEOS planar results encoded as GeoJSON "
        "in LOCAL_METERS with no rounding, snapping or area threshold. "
        "SQLite stores the same geometry as JSON text; it is not SpatiaLite."
    ),
    "input_sha256": (
        "SHA256 of the exact bytes parsed for each of the three inputs "
        "(zones, parcels_before, parcels_after). The same digests appear in "
        "matrix.json, as a foreign member of transitions.geojson and in the "
        "SQLite input_hashes table."
    ),
}


def _category_order(category):
    """Deterministic category order: classified by class, then unclassified."""
    kind, klass = category
    return (kind, klass if klass is not None else "")


def _zone_categories(zone_geometry, parcels):
    """Unioned positive-area geometry per (coverage_kind, class) in one zone.

    Classified categories are keyed by the parcel class string; all uncovered
    area is one ('unclassified', None) category. A real class named
    'unclassified' stays ('classified', 'unclassified') and is never merged
    with the null-class uncovered category.
    """
    ordered = sorted(parcels, key=lambda item: item["parcel_id"])
    by_class = defaultdict(list)
    for parcel in ordered:
        clipped = polygonal(zone_geometry.intersection(parcel["geometry"]))
        if clipped.is_empty or clipped.area <= 0:
            continue
        by_class[parcel["class"]].append(clipped)
    categories = {}
    for klass in sorted(by_class):
        categories[("classified", klass)] = polygonal(union_all(by_class[klass]))
    footprint = union_all([parcel["geometry"] for parcel in ordered])
    missing = polygonal(zone_geometry.difference(footprint))
    if not missing.is_empty and missing.area > 0:
        categories[("unclassified", None)] = missing
    return categories


def _check_zone_geometry(zone_id, zone_geometry, facts):
    """Final facts must tile the zone without positive-area overlap."""
    tolerance = max(1e-8, 1e-9 * zone_geometry.area)
    geometries = [fact["geometry"] for fact in facts]
    try:
        facts_union = union_all(geometries)
        symmetric = polygonal(facts_union.symmetric_difference(zone_geometry)).area
    except Exception as exc:
        raise ValueError(
            f"zone {zone_id!r}: geometry comparison failed: {exc}"
        ) from exc
    if symmetric > tolerance:
        raise ValueError(
            f"zone {zone_id!r}: transition geometries do not match zone "
            f"geometry (symmetric difference {symmetric})"
        )
    tree = STRtree(geometries)
    for index, geometry in enumerate(geometries):
        for hit in tree.query(geometry):
            other = int(hit)
            if other <= index:
                continue
            if geometry.intersection(geometries[other]).area > tolerance:
                raise ValueError(
                    f"zone {zone_id!r}: transition geometries overlap with "
                    "positive area"
                )


def _zone_transitions(zone, before_parcels, after_parcels):
    """Matrix, summary and positive-area facts for one zone."""
    zone_id = zone["zone_id"]
    zone_geometry = zone["geometry"]
    before = _zone_categories(zone_geometry, before_parcels)
    after = _zone_categories(zone_geometry, after_parcels)
    rows = sorted(before, key=_category_order)
    columns = sorted(after, key=_category_order)
    cell_geometry = {}
    areas = []
    for before_key in rows:
        row = []
        for after_key in columns:
            try:
                geometry = polygonal(
                    before[before_key].intersection(after[after_key])
                )
            except Exception as exc:
                raise ValueError(
                    f"zone {zone_id!r}: transition intersection failed: {exc}"
                ) from exc
            area = 0.0 if geometry.is_empty else geometry.area
            row.append(area)
            cell_geometry[(before_key, after_key)] = (
                geometry if area > 0 else None
            )
        areas.append(row)
    row_totals = [math.fsum(row) for row in areas]
    column_totals = [
        math.fsum(areas[i][j] for i in range(len(rows)))
        for j in range(len(columns))
    ]
    total = math.fsum(row_totals)
    zone_area = zone_geometry.area
    if not math.isclose(total, zone_area, rel_tol=1e-9, abs_tol=1e-8):
        raise ValueError(
            f"zone {zone_id!r}: transition total {total} does not conserve "
            f"zone area {zone_area}"
        )
    for key, row_total in zip(rows, row_totals):
        if not math.isclose(
            row_total, before[key].area, rel_tol=1e-9, abs_tol=1e-8
        ):
            raise ValueError(
                f"zone {zone_id!r}: before-period row total {row_total} does "
                f"not match category area {before[key].area}"
            )
    for key, column_total in zip(columns, column_totals):
        if not math.isclose(
            column_total, after[key].area, rel_tol=1e-9, abs_tol=1e-8
        ):
            raise ValueError(
                f"zone {zone_id!r}: after-period column total {column_total} "
                f"does not match category area {after[key].area}"
            )
    facts = []
    for before_key in rows:
        for after_key in columns:
            geometry = cell_geometry[(before_key, after_key)]
            if geometry is None:
                continue
            facts.append(
                {
                    "zone_id": zone_id,
                    "before_coverage_kind": before_key[0],
                    "before_class": before_key[1],
                    "after_coverage_kind": after_key[0],
                    "after_class": after_key[1],
                    "area_m2": geometry.area,
                    "geometry": geometry,
                }
            )
    _check_zone_geometry(zone_id, zone_geometry, facts)
    matrix = {
        "zone_id": zone_id,
        "zone_area_m2": zone_area,
        "rows": [
            {"coverage_kind": kind, "class": klass} for kind, klass in rows
        ],
        "columns": [
            {"coverage_kind": kind, "class": klass} for kind, klass in columns
        ],
        "areas_m2": areas,
        "row_totals_m2": row_totals,
        "column_totals_m2": column_totals,
        "total_m2": total,
    }
    summary = {
        "zone_id": zone_id,
        "zone_area_m2": zone_area,
        "before_classified_area_m2": math.fsum(
            before[key].area for key in rows if key[0] == "classified"
        ),
        "before_unclassified_area_m2": math.fsum(
            before[key].area for key in rows if key[0] == "unclassified"
        ),
        "after_classified_area_m2": math.fsum(
            after[key].area for key in columns if key[0] == "classified"
        ),
        "after_unclassified_area_m2": math.fsum(
            after[key].area for key in columns if key[0] == "unclassified"
        ),
        "total_transition_m2": total,
    }
    return matrix, summary, facts


def _geometry_json(geometry):
    return json.dumps(mapping(geometry), sort_keys=True, allow_nan=False)


def _write_json(path, payload):
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(payload, sort_keys=True, indent=2, allow_nan=False)
            + "\n"
        )
        handle.flush()
        os.fsync(handle.fileno())


def _write_sqlite(path, facts, matrices, summaries, input_hashes):
    """Write the queryable store and commit before any JSON file exists."""
    with closing(sqlite3.connect(path)) as db:
        db.executescript(
            "CREATE TABLE transitions("
            "transition_id INTEGER PRIMARY KEY,zone_id TEXT,"
            "before_coverage_kind TEXT,before_class TEXT,"
            "after_coverage_kind TEXT,after_class TEXT,"
            "area_m2 REAL,geometry_json TEXT);"
            "CREATE TABLE matrix_cells("
            "zone_id TEXT,before_coverage_kind TEXT,before_class TEXT,"
            "after_coverage_kind TEXT,after_class TEXT,area_m2 REAL);"
            "CREATE TABLE zone_summary("
            "zone_id TEXT PRIMARY KEY,zone_area_m2 REAL,"
            "before_classified_area_m2 REAL,before_unclassified_area_m2 REAL,"
            "after_classified_area_m2 REAL,after_unclassified_area_m2 REAL,"
            "total_transition_m2 REAL);"
            "CREATE TABLE input_hashes(name TEXT PRIMARY KEY,sha256 TEXT);"
        )
        db.executemany(
            "INSERT INTO transitions VALUES (?,?,?,?,?,?,?,?)",
            [
                (
                    transition_id,
                    fact["zone_id"],
                    fact["before_coverage_kind"],
                    fact["before_class"],
                    fact["after_coverage_kind"],
                    fact["after_class"],
                    fact["area_m2"],
                    _geometry_json(fact["geometry"]),
                )
                for transition_id, fact in enumerate(facts, 1)
            ],
        )
        db.executemany(
            "INSERT INTO matrix_cells VALUES (?,?,?,?,?,?)",
            [
                (
                    matrix["zone_id"],
                    matrix["rows"][i]["coverage_kind"],
                    matrix["rows"][i]["class"],
                    matrix["columns"][j]["coverage_kind"],
                    matrix["columns"][j]["class"],
                    matrix["areas_m2"][i][j],
                )
                for matrix in matrices
                for i in range(len(matrix["rows"]))
                for j in range(len(matrix["columns"]))
            ],
        )
        db.executemany(
            "INSERT INTO zone_summary VALUES (?,?,?,?,?,?,?)",
            [
                tuple(
                    summary[key]
                    for key in (
                        "zone_id",
                        "zone_area_m2",
                        "before_classified_area_m2",
                        "before_unclassified_area_m2",
                        "after_classified_area_m2",
                        "after_unclassified_area_m2",
                        "total_transition_m2",
                    )
                )
                for summary in summaries
            ],
        )
        db.executemany(
            "INSERT INTO input_hashes VALUES (?,?)", input_hashes.items()
        )
        db.commit()


def transition(zones_path, before_path, after_path, output):
    """Build the two-period transition outputs into a new directory.

    All three inputs are fully validated before any output is created. The
    finished directory (transitions.geojson, matrix.json,
    transitions.sqlite) is published atomically; failures leave no partial
    target and never modify the inputs.
    """
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    try:
        zone_bytes, zones, _ = load_input(zones_path, "zone", "zones: ")
    except OSError as exc:
        raise OSError(f"zones input unreadable: {exc}") from exc
    try:
        before_bytes, before_parcels, _ = load_input(
            before_path, "parcel", "parcels_before: "
        )
    except OSError as exc:
        raise OSError(f"parcels_before input unreadable: {exc}") from exc
    try:
        after_bytes, after_parcels, _ = load_input(
            after_path, "parcel", "parcels_after: "
        )
    except OSError as exc:
        raise OSError(f"parcels_after input unreadable: {exc}") from exc
    input_hashes = {
        "zones": hashlib.sha256(zone_bytes).hexdigest(),
        "parcels_before": hashlib.sha256(before_bytes).hexdigest(),
        "parcels_after": hashlib.sha256(after_bytes).hexdigest(),
    }
    matrices = []
    summaries = []
    facts = []
    for zone in sorted(zones, key=lambda item: item["zone_id"]):
        matrix, summary, zone_facts = _zone_transitions(
            zone, before_parcels, after_parcels
        )
        matrices.append(matrix)
        summaries.append(summary)
        facts.extend(zone_facts)
    report = {
        "schema_version": SCHEMA_VERSION,
        "mode": "transition",
        "coordinate_system": "LOCAL_METERS",
        "input_sha256": input_hashes,
        "feature_count": {
            "zones": len(zones),
            "parcels_before": len(before_parcels),
            "parcels_after": len(after_parcels),
        },
        "definitions": DEFINITIONS,
        "zone_count": len(matrices),
        "transition_count": len(facts),
        "zones": matrices,
    }
    collection = {
        "type": "FeatureCollection",
        "coordinate_system": "LOCAL_METERS",
        "input_sha256": input_hashes,
        "features": [
            {
                "type": "Feature",
                "properties": {
                    "transition_id": transition_id,
                    "zone_id": fact["zone_id"],
                    "before_coverage_kind": fact["before_coverage_kind"],
                    "before_class": fact["before_class"],
                    "after_coverage_kind": fact["after_coverage_kind"],
                    "after_class": fact["after_class"],
                    "area_m2": fact["area_m2"],
                },
                "geometry": mapping(fact["geometry"]),
            }
            for transition_id, fact in enumerate(facts, 1)
        ],
    }
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".landcover-transition-", dir=parent))
    publish = work / "publish"
    publish.mkdir()
    try:
        _write_sqlite(
            publish / "transitions.sqlite",
            facts,
            matrices,
            summaries,
            input_hashes,
        )
        _write_json(publish / "transitions.geojson", collection)
        _write_json(publish / "matrix.json", report)
        if output.exists():
            raise FileExistsError(f"output already exists: {output}")
        publish.rename(output)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    shutil.rmtree(work, ignore_errors=True)
    return report


def _category_label(category):
    if category["coverage_kind"] == "classified":
        return category["class"]
    return "uncovered"


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=(
            "Build two-period land-cover transition geometry, area matrix "
            "and SQLite store"
        )
    )
    parser.add_argument(
        "--zones", type=Path, default=Path("data/transition_zones.geojson")
    )
    parser.add_argument(
        "--before",
        "--parcels-before",
        dest="before",
        type=Path,
        default=Path("data/transition_parcels_before.geojson"),
        help="before-period parcel FeatureCollection",
    )
    parser.add_argument(
        "--after",
        "--parcels-after",
        dest="after",
        type=Path,
        default=Path("data/transition_parcels_after.geojson"),
        help="after-period parcel FeatureCollection",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("run/transition"),
        help="new directory for transitions.geojson, matrix.json and "
        "transitions.sqlite",
    )
    args = parser.parse_args(argv)
    try:
        report = transition(args.zones, args.before, args.after, args.output)
    except (ValueError, OSError) as exc:
        parser.exit(2, f"transition failed: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "transition interrupted\n")
    except Exception as exc:
        parser.exit(1, f"transition failed: {type(exc).__name__}: {exc}\n")
    summary = {}
    for matrix in report["zones"]:
        combos = {}
        for i, row in enumerate(matrix["rows"]):
            for j, column in enumerate(matrix["columns"]):
                area = matrix["areas_m2"][i][j]
                if area > 0:
                    combos[
                        f"{_category_label(row)}->{_category_label(column)}"
                    ] = area
        summary[matrix["zone_id"]] = combos
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
