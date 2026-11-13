"""Strict planar polygon feature collections, never implicit lon/lat areas."""
import json
import math
from pathlib import Path
from shapely import STRtree
from shapely.errors import GEOSException
from shapely.geometry import shape

_PROPERTIES={"zone":{"zone_id"},"parcel":{"parcel_id","class"}}


def read_document_bytes(path):
    """Capture the input bytes exactly once; callers parse and hash this value."""
    return Path(path).read_bytes()


def decode_bytes(data,prefix=""):
    try:document=json.loads(data)
    except (json.JSONDecodeError,UnicodeDecodeError) as exc:raise ValueError(f"{prefix}invalid JSON: {exc}") from exc
    return document


def _coordinates(value,where):
    if not isinstance(value,list) or not value:raise ValueError(f"{where}missing coordinates")
    if isinstance(value[0],list):
        for child in value:_coordinates(child,where)
    elif len(value)!=2 or any(type(v) not in {float,int} or not math.isfinite(v) or abs(v)>1e6 for v in value):
        raise ValueError(f"{where}requires bounded finite 2D coordinates")


def parse_document(document,kind,prefix=""):
    """Validate a decoded FeatureCollection and return properties+geometry records."""
    if not isinstance(document,dict) or document.get("type")!="FeatureCollection" or document.get("coordinate_system")!="LOCAL_METERS":
        raise ValueError(f"{prefix}requires FeatureCollection with explicit LOCAL_METERS coordinates")
    raw=document.get("features")
    if not isinstance(raw,list) or (kind=="zone" and not raw):raise ValueError(f"{prefix}invalid/empty feature collection")
    expected=_PROPERTIES[kind];result=[];ids=set()
    for index,feature in enumerate(raw):
        where=f"{prefix}feature {index}: "
        if not isinstance(feature,dict) or feature.get("type")!="Feature":raise ValueError(f"{where}invalid feature")
        props=feature.get("properties")
        if not isinstance(props,dict) or set(props)!=expected or any(not isinstance(v,str) or not v for v in props.values()):
            raise ValueError(f"{where}invalid feature properties")
        identity=props["zone_id" if kind=="zone" else "parcel_id"]
        if identity in ids:raise ValueError(f"{where}duplicate feature identity {identity!r}")
        ids.add(identity)
        geometry=feature.get("geometry")
        if not isinstance(geometry,dict) or geometry.get("type") not in {"Polygon","MultiPolygon"}:
            raise ValueError(f"{where}only polygons accepted")
        _coordinates(geometry.get("coordinates"),where)
        polygons=[geometry["coordinates"]] if geometry["type"]=="Polygon" else geometry["coordinates"]
        for rings in polygons:
            if not isinstance(rings,list) or not rings:raise ValueError(f"{where}polygon requires rings")
            for ring in rings:
                if not isinstance(ring,list) or len(ring)<4 or ring[0]!=ring[-1]:
                    raise ValueError(f"{where}polygon rings require >=4 positions and explicit closure")
        try:polygon=shape(geometry)
        except (ValueError,TypeError,IndexError,GEOSException) as exc:raise ValueError(f"{where}malformed polygon") from exc
        if polygon.is_empty or not polygon.is_valid or polygon.area<=0:
            raise ValueError(f"{where}invalid or zero-area topology")
        result.append({**props,"geometry":polygon})
    return result


def validate_layer(features,kind,prefix="",stats=None,observer=None):
    """Reject positive-area same-layer overlaps using bbox candidates only.

    Candidate pairs are unordered feature pairs whose bounding boxes intersect,
    deduplicated across all index queries. Only those pairs receive one exact
    areal intersection test. Touching edges/points therefore stay legal.
    """
    exact=0;candidates=0
    if len(features)>1:
        geometries=[f["geometry"] for f in features]
        tree=STRtree(geometries)
        pairs=set()
        for index,geometry in enumerate(geometries):
            for hit in tree.query(geometry):
                other=int(hit)
                if other!=index:pairs.add((index,other) if index<other else (other,index))
        candidates=len(pairs)
        if observer is not None:observer({"event":"topology_candidates","kind":kind,"candidate_pairs":candidates})
        for left,right in sorted(pairs):
            exact+=1
            if observer is not None:observer({"event":"exact_topology_check","kind":kind,"left":left,"right":right})
            if geometries[left].intersection(geometries[right]).area>0:
                left_id=features[left]["zone_id" if kind=="zone" else "parcel_id"]
                right_id=features[right]["zone_id" if kind=="zone" else "parcel_id"]
                raise ValueError(f"{prefix}overlapping {kind} polygons (features {left}:{left_id!r} and {right}:{right_id!r})")
    if stats is not None:stats.update({"candidate_pairs":candidates,"exact_checks":exact})


def load_input(path,kind,prefix="",observer=None,indexed=True):
    """Read bytes once, parse/validate that same content, and return the bytes.

    ``indexed=False`` keeps the baseline's exhaustive pairwise exact checks;
    the tiled entry uses the spatial candidate index so unrelated features do
    not enter geometry operations.
    """
    data=read_document_bytes(path)
    features=parse_document(decode_bytes(data,prefix),kind,prefix)
    stats={}
    if indexed:validate_layer(features,kind,prefix,stats,observer)
    else:validate_layer_pairwise(features,kind,prefix,stats,observer)
    return data,features,stats


def validate_layer_pairwise(features,kind,prefix="",stats=None,observer=None):
    """Baseline same-layer rule: exact-check every unordered pair once."""
    exact=0;candidates=0
    for left in range(len(features)):
        for right in range(left+1,len(features)):
            candidates+=1;exact+=1
            if observer is not None:observer({"event":"exact_topology_check","kind":kind,"left":left,"right":right})
            if features[left]["geometry"].intersection(features[right]["geometry"]).area>0:
                left_id=features[left]["zone_id" if kind=="zone" else "parcel_id"]
                right_id=features[right]["zone_id" if kind=="zone" else "parcel_id"]
                raise ValueError(f"{prefix}overlapping {kind} polygons (features {left}:{left_id!r} and {right}:{right_id!r})")
    if stats is not None:stats.update({"candidate_pairs":candidates,"exact_checks":exact})


def load(path,kind):
    return load_input(path,kind,indexed=False)[1]
