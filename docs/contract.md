# Planar land-cover profile v1

Two JSON FeatureCollections use GeoJSON geometry encoding, with mandatory foreign
member coordinate_system=LOCAL_METERS. Coordinates are synthetic engineering
XY meters, NOT RFC7946 longitude/latitude. No EPSG identity, geodesic area or
reprojection is claimed. Each coordinate is finite, two-dimensional, |v|<=10^6.
Rings must have >=4 positions and explicit closure.
Only valid nonempty positive-area Polygon/MultiPolygon geometries are accepted;
self-intersection is rejected rather than silently repaired.

Zone properties exactly {zone_id}; parcel properties exactly {parcel_id,class};
IDs/classes are nonempty strings, IDs unique within each input. Zone collection
must be nonempty; empty parcel collection is allowed. Zones cannot overlap with
positive area, nor can parcels; shared boundaries are allowed. A polygon hole is
real uncovered area unless another non-overlapping parcel fills it.

One fragment is a zone/parcel intersection with positive area. GeometryCollection
line/point-only pieces are discarded because this is areal accounting. Uncovered
zone geometry becomes an explicit fragment with null parcel/class and
coverage_kind=unclassified. A real class named 'unclassified' remains classified;
coverage_kind disambiguates it. Parcels outside all zones contribute no summary.
Per-zone classified + uncovered must equal zone area within rel 1e-9 / abs 1e-8 m2.
Coverage fraction = classified/zone area; class areas are summed clipped areas,
not parcel centroid counts. GEOS floating precision is used, no rounding grid.

Outputs: fragments.geojson, coverage.json, coverage.sqlite (fragments with encoded
geometry, zone_summary, input_hashes). SQLite is not SpatiaLite and geometry is
queried as JSON text; numeric area fields are directly queryable. Full recompute
replaces named tables and reports deterministically, single writer. DB commits
before JSON files; no multi-file crash publication protocol. Input errors occur
before writing new results. Baseline uses pairwise topology checks and nested
zone/parcel overlay, without spatial indexing, tiling, mixed CRS or overlap repair.

## Tiled build mode

`python -m landcover_cube.tiled --zones Z --parcels P --tile-size S --output O`
(Python: `landcover_cube.tiled.tiled(zones_path, parcels_path, tile_size, output,
observer=None)`) consumes the same two FeatureCollections and the same validity,
identity, ring, Polygon/MultiPolygon and same-layer positive-area-overlap contract.
The whole of both inputs is validated first; parcels outside every zone are not
skipped. Empty parcels are allowed; zones must be nonempty. `tile-size` is a
finite decimal number of meters, strictly greater than 0 and at most 1000000;
booleans, NaN/Infinity and malformed text are rejected. As a practical
implementation guard (not a change to that numeric contract), a tile size that
would require more than 5,000,000 blocks for any single zone is rejected before
allocation with a message asking to increase tile-size; normal synthetic
datasets are far below this bound.

The grid is anchored at (0,0): block (i,j) is `[i*s,(i+1)*s] x [j*s,(j+1)*s]`,
with negative i/j allowed. Shared grid boundaries contribute zero area; only
positive-area polygon parts become facts. Grid indices use true floor division,
never truncation toward zero. Zones may cross blocks, contain holes or multiple
components; parcels may cross zones and blocks.

Scope reduction is real. Same-layer topology validation builds a bbox index,
computes the union of unordered candidate pairs across queries and calls exact
areal intersection at most once per pair. Only blocks whose exact zone
intersection has positive area are processed. Per block, the spatial index
returns candidate parcels; each candidate is intersected with the zone part,
and uncovered area is `zone_part - union(all candidate parcel geometries in
that block)`, so a temporarily uncovered position is never mistaken for final
empty space and no uncovered fragment is emitted from per-parcel processing
alone. Local positive-area pieces are written to a private SQLite staging table
as they are produced (not accumulated as a Python list of all blocks). After
staging, every `(zone_id, coverage_kind, parcel_id)` business key is loaded as a
group, unioned into one Polygon/MultiPolygon, and only then are final facts,
reports and SQLite rows written. Disconnected components and holes are retained;
a real class named `unclassified` stays `coverage_kind=classified` with that
class string. No snapping, simplification, buffering or area threshold is
applied; any positive-area piece is kept. Area conservation uses the baseline
rel_tol=1e-9 / abs_tol=1e-8 m2. The union of final facts is compared with each
zone by symmetric difference, and final facts are checked for positive-area
overlap, both within max(1e-8, 1e-9*zone_area_m2); equal area in the wrong
location therefore fails. Invalid input or same-layer overlap rejects the whole
run rather than being tolerated by a final area check.

Final fact granularity remains `(zone_id, parcel_id)`; unclassified remains one
row per `zone_id` with null parcel/class. `fragment_id` is a stable 1-based
order over `(zone_id, coverage_kind, parcel_id)`: zone_id ascending, then
coverage_kind (`classified` before `unclassified`), then parcel_id ascending
with null sorted first.
`fragments.geojson` keeps LOCAL_METERS; SQLite geometry stays JSON text and is
not SpatiaLite. Each GeoJSON feature also carries `fragment_id` as a property,
matching the SQLite primary key of the same fact.

The new target directory must not pre-exist and contains `fragments.geojson`,
`coverage.json`, `coverage.sqlite` and `build_diagnostics.json`. The first two
inherit the field meanings above. coverage.json adds `mode=tiled`, `tile_size`,
and `grid_anchor=[0,0]`; the baseline mode is unchanged. The three business
outputs come from the same merged facts: GeoJSON, coverage.json and every
SQLite `fragments`/`zone_summary`/`input_hashes` row. Both input files are read
once; parsing, summary hashing and later computation use that same captured
byte content, never a re-read after parsing. Diagnostics record, from the
actual execution path: per-processed-block (i,j), input feature counts, layer
candidate-pair and exact topology-check counts, per-block index candidate and
actual clip counts, staged positive-area piece counts, and final fact counts.
Definitions distinguish index hits from exact geometric operations. Diagnostics
contain no duration, wall-clock time or absolute machine path.

Diagnostic keys (all emitted from the actual run):

- `inputs.zones` / `inputs.parcels`: `features` (validated feature count),
  `candidate_pairs` (deduplicated unordered bbox-candidate pairs), and
  `exact_topology_checks` (actual GEOS intersection calls on those pairs).
- `blocks.items[]`: `i`, `j`, `zone_candidates` (index zone hits for the block),
  `zone_parts` (zones with a positive-area block intersection),
  `parcel_candidates` (index parcel hits, not exact operations),
  `classified_clips` (zone-part/parcel intersections actually executed),
  `unclassified_clips` (zone-part minus block candidate-union differences),
  and `staged_pieces` (positive-area pieces inserted into private staging).
- `totals`: sums of the above plus `final_facts` (merged business-key facts).

`blocks.processed_count` counts blocks whose zone part had positive area; blocks
touching a zone only along a grid line are not counted and emit no facts.

Determinism: identical input bytes and tile size reproduce byte-identical JSON
and GeoJSON, and logically identical SQLite business-key content (database file
bytes need not match). Changing tile size may change partitioning, diagnostics
and geometry encoding but not business keys, classes, spatial meaning or area
indicators within the above tolerances. Reordering input features changes the
source digest truthfully while preserving final business semantics.

Failure atomicity: inputs are validated before any target is published; work
happens in a private temporary directory next to the target. All files are
written and the database closed before the finished directory is published.
An existing target is refused and never overwritten. Success or failure never
modifies the input files. Errors in input, geometry, merge, database, or report
writing exit nonzero (CLI) / raise, identify the input role, feature, block or
zone where applicable, leave no partial official target, and clean this run's
temporary directory; after fixing the cause the same target can simply be
retried. Only single-writer normal/exception safety is claimed, not power-loss
recovery or concurrent publish protocols.

GEOS floating-point limits: areas come from GEOS planar 64-bit floating-point
geometry. Exact grid lines are represented as the same float values on both
sides of a boundary, so an areal part with zero width contributes exactly zero
area; nevertheless coordinates are not snapped to a decimal grid, and results
carrying genuine numerical noise are accepted only within the stated tolerances.
A negative coordinate exactly on (or a tiny distance either side of) a grid line
uses decimal-text-based floor semantics; a part whose true area is positive but
numerically ~1e-16 may be reported with a slightly different magnitude than in
the baseline, while still being retained rather than dropped.

When `tile-size` is not an exact binary fraction (e.g. 0.1 or 2.3), tile edges
are computed as the nearest float of the exact decimal product `k*s`; the same
float value is used on both sides of a shared edge, so adjacent block pieces
meet exactly and the union has no spurious overlap or gap. Coordinates near a
grid line are not snapped: a parcel corner that should lie on the line but
differs by float rounding may create a genuinely positive sliver. Because
slivers are never deleted, such a sliver is reported (usually far below 1e-8
m2) rather than silently absorbed; this is within the stated area tolerances
and is an acknowledged GEOS/float limitation, not a repair step.

## Connectivity vulnerability mode

`python -m landcover_cube.connectivity --zones Z --parcels P --class C
--threshold T --output O`
(Python: `landcover_cube.connectivity.connectivity(zones_path, parcels_path,
target_class, shared_edge_threshold, output)`) is an independent analysis that
reads the same two FeatureCollections and applies the same whole-input
validity, identity, ring, Polygon/MultiPolygon and same-layer positive-area
overlap contract as the overlay entries. Both inputs are fully validated before
any computation or output. `--class`/`--target-class` selects the class string
(an ordinary class string, including a real class literally named
`unclassified`); `--threshold`/`--shared-edge-threshold` is a positive finite
decimal number of meters, rejecting booleans, NaN/Infinity, `+`-separated
junk, underscores, spaces and empty text. `--output` names one JSON report
file (default `run/connectivity/connectivity.json`); an existing directory at
that path is rejected. It uses only the pinned Shapely/NumPy stack and does not
change the baseline or tiled entry points.

Per zone, each target-class parcel is clipped to the zone; every positive-area
Polygon component of the clip is one node with canonicalized geometry (holes
kept). Components of the same parcel, and nodes of different zones, are
separate nodes and are never connected implicitly. A shared-boundary candidate
pair is linked only if the union of LineString pieces in the intersection of
the two node boundaries has total length >= the threshold. Boundary
intersections are reduced to line parts, so a point-only contact has zero
shared-edge length and never creates an edge; a hole boundary is a real
boundary. The reported `shared_boundary_length_m` is the GEOS float length and
the threshold comparison uses its shortest round-trip decimal text, so equality
connects and the decision can be re-derived from the encoded geometry.

Each zone's baseline components and their areas are computed from the retained
edges. Vulnerability removes all nodes of one parcel in that zone
simultaneously, recomputes the remaining components from the remaining edges,
and reports remaining component members/areas, the largest remaining component
and `absolute_loss_m2` = baseline maximum - remaining maximum (0 when either
is empty). `relative_loss` = absolute loss / baseline maximum (0 when the
baseline maximum is 0). Entries are ranked per zone by relative loss
descending, then parcel_id ascending, and numbered from 1. A zone with no
target-class nodes reports zero nodes, zero edges, baseline maximum 0, no
components and an empty vulnerability list.

The report contains `schema_version`, `mode=connectivity`,
`coordinate_system=LOCAL_METERS`, `target_class`, canonical
`shared_edge_threshold_m` text, both `input_sha256` digests, input feature
counts, a `definitions` object, a `caveat` string, and per zone: stable node
records (`node_id`, parcel, class, component index, area, canonical GeoJSON),
edges with both node IDs and the canonical shared-boundary geometry, baseline
components, and the vulnerability ranking. Node IDs are length-prefixed
`<zone_id>|<parcel_id>|<component_index>` tuples that remain unambiguous when
IDs contain `|`. Geometry is GEOS-normalized without rounding or snapping.

Geometric edge contact is only a landscape-structure indicator: a shared edge
does not imply species passage, habitat quality or functional connectivity;
the report states this caveat.

Determinism and failure behavior: identical input bytes and arguments produce
byte-identical report JSON, and reordering input features changes only the
input digest. Invalid input, an invalid class or threshold, or a report-writing
failure raises/exit-nonzero and leaves any previously existing report at
`--output` intact; the new report is written to a temporary sibling file,
flushed and atomically renamed only on success. Only single-writer
normal/exception safety is claimed.

## Survey acquisition mode

`python -m landcover_cube.acquisition --network N --output O`
(Python: `landcover_cube.acquisition.acquire(network_path, output)`) turns a
survey hand-off into the two documents the existing coverage entry already
reads: `parcels.geojson` (a LOCAL_METERS FeatureCollection with exactly
`{parcel_id,class}` properties) and `acquisition_audit.json`. The survey
document is JSON with `type=SurveyNetwork`, `coordinate_system=LOCAL_METERS`,
and exactly the members `nodes`, `edges`, `parcels`:

- node records: exactly `{id,x,y}`; `id` a nonempty string unique among nodes;
  `x`/`y` finite numbers with |v|<=10^6.
- edge records: exactly `{id,a,b}`; `id` unique among edges; `a`/`b` are node
  IDs that exist and differ. The edge is the straight segment between those two
  node positions; it is never snapped, split, buffered or supplemented. Two
  edges may not connect the same unordered node pair, and an edge whose endpoints
  coincide in coordinates is zero-length.
- parcel records: exactly `{parcel_id,class,edge_ids}`; `parcel_id` unique among
  parcels; `class` nonempty; `edge_ids` a nonempty array of existing edge IDs
  with no repetition inside one parcel. An edge may be referenced by at most two
  parcels.

The document decoder rejects duplicate JSON object keys and NaN/Infinity
constants, so the parsed bytes and the audited bytes are unambiguous.

Each parcel's own edge set must decompose into closed loops: every node used by
that parcel has degree exactly two, every loop uses each of its edges once and
visits each node once, and every edge is consumed. Loops are (straight-segment)
rings; each ring must be simple, non-zero-area and a valid polygon. Within a
parcel, loops may not touch or cross each other. Across parcels the same rules
apply through the existing same-layer parser: `parcels.geojson` is re-parsed by
`landcover_cube.source.parse_document` as an ordinary parcel layer and any
positive-area overlap is rejected, so two parcels may share a boundary (the
shared edge is referenced by both) but not an area. Faces of one parcel that do
not share an edge are separate polygons of one MultiPolygon.

Containment uses strict geometric containment depth: the depth of a loop is the
number of *other* loops of the same parcel whose polygon contains it. Even depth
0 is an outer shell; odd depth is a hole; even depth >=2 is an island shell
inside a hole and becomes its own Polygon face. Each even-depth loop becomes one
face whose holes are its directly contained odd-depth loops (parent = the
unique loop at depth-1 that contains it). Nested squares of side 10/6/2 inside
one parcel therefore form a MultiPolygon of two faces with areas 64 and 4, total
68.

Canonicalisation makes the emitted `parcels.geojson` independent of input
feature order, edge order, parcel edge-list order and edge endpoint
orientation: shells are counter-clockwise and holes clockwise (ring orientation
is fixed from the declared node positions), each ring is rotated to its
lexicographically smallest coordinate rotation, and faces, parcels and rings are
ordered deterministically. Only the emitted geometry is invariant; the audit
truthfully records the source digests and the declared endpoint order.

The audit (`acquisition_audit.json`) contains `schema_version`, `mode=acquisition`,
`coordinate_system`, the single-read `source_sha256`, input counts, a
`definitions` object, and:

- `nodes` in ID order with `id`, `x`, `y`, `source_index` (input array position).
- `edges` in ID order with `id`, `a`, `b`, `length_m`, the sorted
  `parcel_ids` that reference it, `source_index`.
- `parcels` in ID order with `parcel_id`, `class`, `source_index`, the declared
  `edge_ids`, `geometry_type`, `area_m2`, `ring_count`, `face_count`, `rings` and
  `faces`.
  Every ring has `ring_index`, `depth`, `role` (`shell`/`hole`),
  `parent_ring_index` (null for an outer shell), `area_m2`, `node_ids` (canonical
  traversal) and `segments`. Every segment has `edge_id`, `from_node`,
  `to_node`, `direction` (`forward` when the canonical traversal matches the
  declared `a`->`b`, else `reverse`), `edge_source_index`,
  `from_source_index` and `to_source_index`. Each face lists
  `shell_ring_index` and `hole_ring_indices`. These records let a reader
  re-derive every emitted ring and its provenance from the audit alone.

Determinism: identical survey bytes reproduce byte-identical
`parcels.geojson` and `acquisition_audit.json`. Shuffling node/edge/parcel
records, reversing edge endpoints or reordering a parcel's edge list leaves
`parcels.geojson` byte-identical but changes the truthful audit (source hash,
`source_index`, `direction`). No coordinates are rounded, snapped or repaired.

Publication: the target must not already exist. Both files are written inside a
private temporary directory next to the target, and that directory is renamed
into place only after both are complete; failures remove the temporary directory
and leave no partial target, and input bytes are never modified. The CLI exits
nonzero on rejection. The emitted `parcels.geojson` is accepted directly by
`python -m landcover_cube --parcels ...`; the acquisition step itself does not
compute coverage, and no new dependency is introduced beyond the pinned
Shapely/NumPy stack.

The bundled `data/acquisition_network.json` is a synthetic fixture: three
concentric squares (side 10, 6, 2) owned by parcel `P_NEST` (area 68), plus a
`P_EAST` square sharing the outer parcel's right boundary (area 100). No real
survey, parcel or coordinate system is represented.
