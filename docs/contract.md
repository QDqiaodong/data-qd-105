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
