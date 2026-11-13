"""Offline tiled planar overlay with private SQLite staging and atomic publish.

The grid is anchored at (0, 0): block (i, j) is the closed rectangle
[i*s, (i+1)*s] x [j*s, (j+1)*s] where s is the decimal tile size. Geometry
boundaries may be shared by adjacent blocks; only positive-area polygon pieces
are facts. Block indices use true floor division, including negative values.
"""
import argparse
import hashlib
import json
import math
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import closing
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from pathlib import Path

from shapely import STRtree, from_wkb, to_wkb, union_all
from shapely.geometry import box, mapping

from .overlay import polygonal
from .source import load_input

MAX_TILE_SIZE = Decimal(1000000)
# Practical guard, not part of the input contract: a tile size that would need
# absurdly many blocks is rejected before allocation. This keeps an otherwise
# "valid decimal" such as 1e-100 from exhausting memory; the count is far above
# what the synthetic workloads or tests require.
MAX_BLOCKS = 5000000
DEFINITIONS = {
    "topology_candidate_pairs": (
        "Unordered same-layer feature pairs whose bounding boxes intersect, "
        "deduplicated across all spatial-index queries. Each pair is "
        "exact-tested at most once."
    ),
    "exact_topology_checks": (
        "Actual GEOS areal intersection calls made for deduplicated same-layer "
        "candidate pairs."
    ),
    "zone_candidates": (
        "Index-query zone hits for one block, before exact clipping. Not an "
        "exact geometric operation count."
    ),
    "zone_parts": (
        "Zones whose exact intersection with one block has positive polygon area."
    ),
    "parcel_candidates": (
        "Index-query parcel hits for one block. This is an index hit count, not "
        "an exact geometric operation count."
    ),
    "classified_clips": (
        "Exact zone-part/parcel intersection calls performed per block, one per "
        "candidate pair."
    ),
    "unclassified_clips": (
        "Exact zone-part difference calls performed per block against the union "
        "of that block's candidate parcel geometries."
    ),
    "staged_pieces": (
        "Positive-area polygon pieces written to the private SQLite staging "
        "database."
    ),
    "final_facts": (
        "Merged (zone_id, coverage_kind, parcel_id) facts after every tile piece "
        "for a business key is unioned."
    ),
}


def _decimal_text(text):
    """Strictly parse decimal text; reject bools, NaN/Infinity, underscores, spaces."""
    if not isinstance(text, str) or not text:
        raise ValueError("tile-size must be finite decimal text")
    index = 0
    length = len(text)
    if text[index] in "+-":
        index += 1
    digits = 0
    while index < length and text[index].isdigit():
        index += 1
        digits += 1
    if index < length and text[index] == ".":
        index += 1
        while index < length and text[index].isdigit():
            index += 1
            digits += 1
    if digits == 0:
        raise ValueError("tile-size must be finite decimal text")
    if index < length and text[index] in "eE":
        index += 1
        if index < length and text[index] in "+-":
            index += 1
        exponent_digits = 0
        while index < length and text[index].isdigit():
            index += 1
            exponent_digits += 1
        if exponent_digits == 0:
            raise ValueError("tile-size must be finite decimal text")
    if index != length:
        raise ValueError("tile-size must be finite decimal text")
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError("tile-size must be finite decimal text") from exc


def parse_tile_size(value):
    """Validate a tile size in meters: finite decimal in (0, 1000000]."""
    if isinstance(value, bool):
        raise ValueError("tile-size must be a finite decimal number in (0, 1000000]")
    if isinstance(value, Decimal):
        parsed = value
    elif isinstance(value, int):
        parsed = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("tile-size must be a finite decimal number in (0, 1000000]")
        parsed = Decimal(repr(value))
    elif isinstance(value, str):
        parsed = _decimal_text(value)
    else:
        raise ValueError("tile-size must be a finite decimal number in (0, 1000000]")
    if not parsed.is_finite() or parsed <= 0 or parsed > MAX_TILE_SIZE:
        raise ValueError("tile-size must be a finite decimal number in (0, 1000000]")
    return parsed


def tile_text(size):
    """Canonical non-exponential decimal text for a validated tile size."""
    normalized = size.normalize()
    if normalized == normalized.to_integral_value():
        return format(normalized.to_integral_value(), "f")
    return format(normalized, "f")


def _exact_decimal(value):
    """Exact rational for a float's shortest round-trip decimal text."""
    return Fraction(Decimal(repr(float(value))))


def _floor_index(value, size):
    """True floor division on decimal coordinates, never truncating to zero."""
    return math.floor(_exact_decimal(value) / Fraction(size))


def _block_bounds(i, j, size):
    x0 = float(Fraction(i) * Fraction(size))
    y0 = float(Fraction(j) * Fraction(size))
    x1 = float(Fraction(i + 1) * Fraction(size))
    y1 = float(Fraction(j + 1) * Fraction(size))
    return x0, y0, x1, y1


def _candidate_blocks(zones, size):
    """Blocks that can meet a zone bbox with positive interior area.

    Lower bounds use true floor division (never truncation toward zero). Upper
    bounds use exact decimal ceiling minus one, so a zone bound exactly on a
    grid line does not add the block touching it only along that line. Any
    float-noise block that still gets through is dropped when its exact zone
    intersection has zero area, so shared edges never become duplicate facts
    and no positive-area piece can be lost.

    A cheap Decimal magnitude check rejects an absurd block count before exact
    rational arithmetic could materialize huge integers for a tiny tile size.
    """
    blocks = set()
    for zone in zones:
        minx, miny, maxx, maxy = zone["geometry"].bounds
        width = Decimal(repr(maxx)) - Decimal(repr(minx))
        height = Decimal(repr(maxy)) - Decimal(repr(miny))
        small = size.adjusted()
        if small < -20 or width.adjusted() - small > 7 or height.adjusted() - small > 7:
            raise ValueError(
                f"tile-size {_report_text(size)} produces too many blocks for "
                f"zone {zone['zone_id']!r}; increase tile-size"
            )
        upper_x = width / size + 3
        upper_y = height / size + 3
        if upper_x > MAX_BLOCKS or upper_y > MAX_BLOCKS:
            raise ValueError(
                f"tile-size {_report_text(size)} produces too many blocks for "
                f"zone {zone['zone_id']!r}; increase tile-size"
            )
        i_start = _floor_index(minx, size)
        j_start = _floor_index(miny, size)
        i_stop = math.ceil(_exact_decimal(maxx) / Fraction(size)) - 1
        j_stop = math.ceil(_exact_decimal(maxy) / Fraction(size)) - 1
        if (i_stop - i_start + 1) * (j_stop - j_start + 1) + len(blocks) > MAX_BLOCKS:
            raise ValueError(
                f"tile-size {_report_text(size)} produces too many blocks for "
                f"zone {zone['zone_id']!r}; increase tile-size"
            )
        for i in range(i_start, i_stop + 1):
            for j in range(j_start, j_stop + 1):
                blocks.add((i, j))
    return sorted(blocks)


def _report_text(size):
    """Short tile-size label safe for error messages (bounded output length)."""
    text = format(size, "f")
    return text if len(text) <= 40 else text[:37] + "..."


def _insert_piece(db, zone_id, coverage_kind, parcel_id, klass, geometry, i, j):
    db.execute(
        "INSERT INTO pieces(zone_id,coverage_kind,parcel_id,class,geometry_wkb,"
        "block_i,block_j) VALUES (?,?,?,?,?,?,?)",
        (
            zone_id,
            coverage_kind,
            parcel_id,
            klass,
            to_wkb(geometry),
            i,
            j,
        ),
    )


def _merge_pieces(rows):
    """Union every staged piece for one business key into one areal geometry."""
    merged = polygonal(union_all([from_wkb(row[0]) for row in rows]))
    if merged.is_empty or merged.area <= 0:
        raise ValueError("merged business key has non-positive area")
    return merged


def _stage_blocks(zones, parcels, size, db, observer):
    zone_tree = STRtree([zone["geometry"] for zone in zones])
    parcel_tree = STRtree([parcel["geometry"] for parcel in parcels])
    parcel_order = sorted(range(len(parcels)), key=lambda k: parcels[k]["parcel_id"])
    zone_order = sorted(range(len(zones)), key=lambda k: zones[k]["zone_id"])
    block_reports = []
    for i, j in _candidate_blocks(zones, size):
        try:
            x0, y0, x1, y1 = _block_bounds(i, j, size)
            block_geom = box(x0, y0, x1, y1)
            zone_hits = set(int(k) for k in zone_tree.query(block_geom))
            zone_parts = []
            for k in zone_order:
                if k not in zone_hits:
                    continue
                part = polygonal(zones[k]["geometry"].intersection(block_geom))
                if not part.is_empty and part.area > 0:
                    zone_parts.append((k, part))
            if not zone_parts:
                continue
            parcel_hits = set(int(k) for k in parcel_tree.query(block_geom))
            candidates = [k for k in parcel_order if k in parcel_hits]
            candidate_union = (
                union_all([parcels[k]["geometry"] for k in candidates])
                if candidates
                else None
            )
            classified_clips = 0
            unclassified_clips = 0
            staged = 0
            if observer is not None:
                observer({"event": "block", "i": i, "j": j})
            for k, part in zone_parts:
                zone_id = zones[k]["zone_id"]
                for p in candidates:
                    classified_clips += 1
                    if observer is not None:
                        observer(
                            {
                                "event": "classified_clip",
                                "i": i,
                                "j": j,
                                "zone_id": zone_id,
                                "parcel_id": parcels[p]["parcel_id"],
                            }
                        )
                    try:
                        clipped = polygonal(part.intersection(parcels[p]["geometry"]))
                    except Exception as exc:
                        raise ValueError(
                            f"zone {zone_id!r} parcel "
                            f"{parcels[p]['parcel_id']!r}: {exc}"
                        ) from exc
                    if clipped.is_empty or clipped.area <= 0:
                        continue
                    _insert_piece(
                        db,
                        zone_id,
                        "classified",
                        parcels[p]["parcel_id"],
                        parcels[p]["class"],
                        clipped,
                        i,
                        j,
                    )
                    staged += 1
                    if observer is not None:
                        observer(
                            {
                                "event": "staged_piece",
                                "coverage_kind": "classified",
                                "zone_id": zone_id,
                                "parcel_id": parcels[p]["parcel_id"],
                            }
                        )
                if candidate_union is None:
                    missing = part
                else:
                    unclassified_clips += 1
                    if observer is not None:
                        observer(
                            {
                                "event": "unclassified_clip",
                                "i": i,
                                "j": j,
                                "zone_id": zone_id,
                            }
                        )
                    try:
                        missing = polygonal(part.difference(candidate_union))
                    except Exception as exc:
                        raise ValueError(f"zone {zone_id!r}: {exc}") from exc
                if not missing.is_empty and missing.area > 0:
                    _insert_piece(
                        db, zone_id, "unclassified", None, None, missing, i, j
                    )
                    staged += 1
                    if observer is not None:
                        observer(
                            {
                                "event": "staged_piece",
                                "coverage_kind": "unclassified",
                                "zone_id": zone_id,
                                "parcel_id": None,
                            }
                        )
            block_reports.append(
                {
                    "i": i,
                    "j": j,
                    "zone_candidates": len(zone_hits),
                    "zone_parts": len(zone_parts),
                    "parcel_candidates": len(candidates),
                    "classified_clips": classified_clips,
                    "unclassified_clips": unclassified_clips,
                    "staged_pieces": staged,
                }
            )
        except Exception as exc:
            raise ValueError(f"block ({i},{j}): {exc}") from exc
    return block_reports


def _merge_and_check(db, zones, observer=None):
    zone_by_id = {zone["zone_id"]: zone for zone in zones}
    keys = [
        tuple(row)
        for row in db.execute(
            "SELECT DISTINCT zone_id,coverage_kind,parcel_id FROM pieces"
            " ORDER BY zone_id,coverage_kind,parcel_id IS NULL,parcel_id"
        )
    ]
    fragments = []
    for zone_id, coverage_kind, parcel_id in keys:
        try:
            rows = db.execute(
                "SELECT geometry_wkb FROM pieces"
                " WHERE zone_id=? AND coverage_kind=? AND parcel_id IS ?"
                " ORDER BY seq",
                (zone_id, coverage_kind, parcel_id),
            ).fetchall()
            if observer is not None:
                observer(
                    {
                        "event": "merge_key",
                        "zone_id": zone_id,
                        "coverage_kind": coverage_kind,
                        "parcel_id": parcel_id,
                        "pieces": len(rows),
                    }
                )
            geometry = _merge_pieces(rows)
            klass = None
            if coverage_kind == "classified":
                klass = db.execute(
                    "SELECT class FROM pieces WHERE zone_id=? AND coverage_kind="
                    "'classified' AND parcel_id=? LIMIT 1",
                    (zone_id, parcel_id),
                ).fetchone()[0]
        except Exception as exc:
            raise ValueError(
                f"merge zone {zone_id!r} parcel {parcel_id!r}: {exc}"
            ) from exc
        fragments.append(
            {
                "zone_id": zone_id,
                "coverage_kind": coverage_kind,
                "parcel_id": parcel_id,
                "class": klass,
                "area_m2": geometry.area,
                "geometry": geometry,
            }
        )
    summaries = _check_results(fragments, zone_by_id)
    return fragments, summaries


def _check_results(fragments, zone_by_id):
    by_zone = defaultdict(list)
    for fragment in fragments:
        by_zone[fragment["zone_id"]].append(fragment)
    summaries = []
    for zone_id in sorted(zone_by_id):
        zone_geom = zone_by_id[zone_id]["geometry"]
        zone_area = zone_geom.area
        zone_fragments = by_zone[zone_id]
        classified = sum(
            f["area_m2"] for f in zone_fragments if f["coverage_kind"] == "classified"
        )
        unclassified = sum(
            f["area_m2"] for f in zone_fragments if f["coverage_kind"] == "unclassified"
        )
        if not math.isclose(
            classified + unclassified, zone_area, rel_tol=1e-9, abs_tol=1e-8
        ):
            raise ValueError(
                f"zone {zone_id!r}: area conservation failed "
                f"({classified + unclassified} vs {zone_area})"
            )
        tolerance = max(1e-8, 1e-9 * zone_area)
        try:
            facts_union = union_all([f["geometry"] for f in zone_fragments])
            symmetric = polygonal(facts_union.symmetric_difference(zone_geom)).area
        except Exception as exc:
            raise ValueError(f"zone {zone_id!r}: geometry comparison failed: {exc}") from exc
        if symmetric > tolerance:
            raise ValueError(
                f"zone {zone_id!r}: final facts do not match zone geometry "
                f"(symmetric difference {symmetric})"
            )
        classes = defaultdict(float)
        for fragment in zone_fragments:
            if fragment["coverage_kind"] == "classified":
                classes[fragment["class"]] += fragment["area_m2"]
        summaries.append(
            {
                "zone_id": zone_id,
                "zone_area_m2": zone_area,
                "classified_area_m2": classified,
                "unclassified_area_m2": unclassified,
                "coverage_fraction": classified / zone_area,
                "class_area_m2": dict(sorted(classes.items())),
            }
        )
    tree = STRtree([fragment["geometry"] for fragment in fragments])
    zone_areas = {zone_id: zone["geometry"].area for zone_id, zone in zone_by_id.items()}
    for index, fragment in enumerate(fragments):
        candidates = sorted(int(hit) for hit in tree.query(fragment["geometry"]))
        for other in candidates:
            if other <= index:
                continue
            other_fragment = fragments[other]
            pair_tolerance = max(
                1e-8,
                1e-9
                * min(
                    zone_areas[fragment["zone_id"]],
                    zone_areas[other_fragment["zone_id"]],
                ),
            )
            if (
                fragment["geometry"].intersection(
                    other_fragment["geometry"]
                ).area
                > pair_tolerance
            ):
                raise ValueError(
                    "final facts overlap with positive area: "
                    f"{fragment['zone_id']!r}/{fragment['parcel_id']!r} and "
                    f"{other_fragment['zone_id']!r}/"
                    f"{other_fragment['parcel_id']!r}"
                )
    return summaries


def _geometry_json(geometry):
    return json.dumps(mapping(geometry), sort_keys=True, allow_nan=False)


def _write_json(path, payload):
    path.write_text(
        json.dumps(payload, sort_keys=True, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def _diagnostics(zones, parcels, size, zone_stats, parcel_stats, block_reports, facts):
    counts = defaultdict(int)
    for block in block_reports:
        counts["zone_candidates"] += block["zone_candidates"]
        counts["parcel_candidates"] += block["parcel_candidates"]
        counts["classified_clips"] += block["classified_clips"]
        counts["unclassified_clips"] += block["unclassified_clips"]
        counts["staged_pieces"] += block["staged_pieces"]
    return {
        "schema_version": 1,
        "mode": "tiled",
        "tile_size": tile_text(size),
        "grid_anchor": [0, 0],
        "definitions": DEFINITIONS,
        "inputs": {
            "zones": {
                "features": len(zones),
                "candidate_pairs": zone_stats["candidate_pairs"],
                "exact_topology_checks": zone_stats["exact_checks"],
            },
            "parcels": {
                "features": len(parcels),
                "candidate_pairs": parcel_stats["candidate_pairs"],
                "exact_topology_checks": parcel_stats["exact_checks"],
            },
        },
        "blocks": {"processed_count": len(block_reports), "items": block_reports},
        "totals": {
            "zone_candidates": counts["zone_candidates"],
            "parcel_candidates": counts["parcel_candidates"],
            "classified_clips": counts["classified_clips"],
            "unclassified_clips": counts["unclassified_clips"],
            "staged_pieces": counts["staged_pieces"],
            "final_facts": len(facts),
        },
    }


def tiled(zones_path, parcels_path, tile_size, output, observer=None):
    """Build a tiled overlay into a previously nonexistent output directory.

    ``observer`` is an optional test/instrumentation callback receiving event
    dictionaries from the actual execution path. It does not change results.
    """
    size = parse_tile_size(tile_size)
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")
    try:
        zone_bytes, zones, zone_stats = load_input(
            zones_path, "zone", "zones: ", observer=observer
        )
    except OSError as exc:
        raise OSError(f"zones input unreadable: {exc}") from exc
    try:
        parcel_bytes, parcels, parcel_stats = load_input(
            parcels_path, "parcel", "parcels: ", observer=observer
        )
    except OSError as exc:
        raise OSError(f"parcels input unreadable: {exc}") from exc
    input_hashes = {
        "zones": hashlib.sha256(zone_bytes).hexdigest(),
        "parcels": hashlib.sha256(parcel_bytes).hexdigest(),
    }
    parent = output.parent
    parent.mkdir(parents=True, exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=".landcover-tiled-", dir=parent))
    publish = work / "publish"
    publish.mkdir()
    try:
        with closing(sqlite3.connect(work / "staging.sqlite")) as db:
            with db:
                db.execute(
                    "CREATE TABLE pieces("
                    "seq INTEGER PRIMARY KEY AUTOINCREMENT,"
                    "zone_id TEXT NOT NULL,"
                    "coverage_kind TEXT NOT NULL,"
                    "parcel_id TEXT,"
                    "class TEXT,"
                    "geometry_wkb BLOB NOT NULL,"
                    "block_i INTEGER NOT NULL,"
                    "block_j INTEGER NOT NULL)"
                )
                block_reports = _stage_blocks(zones, parcels, size, db, observer)
            fragments, summaries = _merge_and_check(db, zones, observer)
        report = {
            "schema_version": 1,
            "mode": "tiled",
            "coordinate_system": "LOCAL_METERS",
            "tile_size": tile_text(size),
            "grid_anchor": [0, 0],
            "zones": summaries,
            "fragment_count": len(fragments),
            "input_sha256": input_hashes,
        }
        collection = {
            "type": "FeatureCollection",
            "coordinate_system": "LOCAL_METERS",
            "features": [
                {
                    "type": "Feature",
                    "properties": {
                        "fragment_id": fragment_id,
                        "zone_id": fragment["zone_id"],
                        "parcel_id": fragment["parcel_id"],
                        "class": fragment["class"],
                        "area_m2": fragment["area_m2"],
                        "coverage_kind": fragment["coverage_kind"],
                    },
                    "geometry": mapping(fragment["geometry"]),
                }
                for fragment_id, fragment in enumerate(fragments, 1)
            ],
        }
        diagnostics = _diagnostics(
            zones,
            parcels,
            size,
            zone_stats,
            parcel_stats,
            block_reports,
            fragments,
        )
        with closing(sqlite3.connect(publish / "coverage.sqlite")) as db:
            db.executescript(
                "CREATE TABLE fragments("
                "fragment_id INTEGER PRIMARY KEY,zone_id TEXT,parcel_id TEXT,"
                "class TEXT,area_m2 REAL,coverage_kind TEXT,geometry_json TEXT);"
                "CREATE TABLE zone_summary("
                "zone_id TEXT PRIMARY KEY,zone_area_m2 REAL,"
                "classified_area_m2 REAL,unclassified_area_m2 REAL,"
                "coverage_fraction REAL);"
                "CREATE TABLE input_hashes(name TEXT PRIMARY KEY,sha256 TEXT);"
            )
            db.executemany(
                "INSERT INTO fragments VALUES (?,?,?,?,?,?,?)",
                [
                    (
                        fragment_id,
                        fragment["zone_id"],
                        fragment["parcel_id"],
                        fragment["class"],
                        fragment["area_m2"],
                        fragment["coverage_kind"],
                        _geometry_json(fragment["geometry"]),
                    )
                    for fragment_id, fragment in enumerate(fragments, 1)
                ],
            )
            db.executemany(
                "INSERT INTO zone_summary VALUES (?,?,?,?,?)",
                [
                    tuple(
                        summary[key]
                        for key in (
                            "zone_id",
                            "zone_area_m2",
                            "classified_area_m2",
                            "unclassified_area_m2",
                            "coverage_fraction",
                        )
                    )
                    for summary in summaries
                ],
            )
            db.executemany(
                "INSERT INTO input_hashes VALUES (?,?)", input_hashes.items()
            )
            db.commit()
        _write_json(publish / "fragments.geojson", collection)
        _write_json(publish / "coverage.json", report)
        _write_json(publish / "build_diagnostics.json", diagnostics)
        if output.exists():
            raise FileExistsError(f"output already exists: {output}")
        publish.rename(output)
    except BaseException:
        shutil.rmtree(work, ignore_errors=True)
        raise
    shutil.rmtree(work, ignore_errors=True)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Build a tiled synthetic planar land-cover overlay"
    )
    parser.add_argument("--zones", type=Path, default=Path("data/zones.geojson"))
    parser.add_argument("--parcels", type=Path, default=Path("data/parcels.geojson"))
    parser.add_argument("--tile-size", required=True)
    parser.add_argument("--output", type=Path, default=Path("run/tiled"))
    args = parser.parse_args(argv)
    try:
        report = tiled(args.zones, args.parcels, args.tile_size, args.output)
    except (ValueError, OSError) as exc:
        parser.exit(1, f"tiled overlay failed: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "tiled overlay interrupted\n")
    except Exception as exc:
        parser.exit(1, f"tiled overlay failed: {type(exc).__name__}: {exc}\n")
    print(json.dumps(report["zones"], sort_keys=True))


if __name__ == "__main__":
    main()
