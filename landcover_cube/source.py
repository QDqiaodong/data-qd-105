"""Strict planar polygon feature collections, never implicit lon/lat areas."""
import json
import math
from shapely.geometry import shape


def load(path,kind):
    with open(path,encoding="utf-8") as stream:document=json.load(stream)
    if not isinstance(document,dict) or document.get("type")!="FeatureCollection" or document.get("coordinate_system")!="LOCAL_METERS":
        raise ValueError("requires FeatureCollection with explicit LOCAL_METERS coordinates")
    raw=document.get("features")
    if not isinstance(raw,list) or (kind=="zone" and not raw):raise ValueError("invalid/empty feature collection")
    result=[];ids=set()
    for feature in raw:
        if not isinstance(feature,dict) or feature.get("type")!="Feature":raise ValueError("invalid feature")
        props=feature.get("properties")
        expected={"zone_id"} if kind=="zone" else {"parcel_id","class"}
        if not isinstance(props,dict) or set(props)!=expected or any(not isinstance(v,str) or not v for v in props.values()):raise ValueError("invalid feature properties")
        identity=props["zone_id" if kind=="zone" else "parcel_id"]
        if identity in ids:raise ValueError("duplicate feature identity")
        ids.add(identity)
        geometry=feature.get("geometry")
        if not isinstance(geometry,dict) or geometry.get("type") not in {"Polygon","MultiPolygon"}:raise ValueError("only polygons accepted")
        def coordinates(value):
            if not isinstance(value,list) or not value:raise ValueError("missing coordinates")
            if isinstance(value[0],list):
                for child in value:coordinates(child)
            elif len(value)!=2 or any(type(v) not in {float,int} or not math.isfinite(v) or abs(v)>1e6 for v in value):
                raise ValueError("requires bounded finite 2D coordinates")
        coordinates(geometry.get("coordinates"))
        polygons = [geometry["coordinates"]] if geometry["type"] == "Polygon" else geometry["coordinates"]
        for rings in polygons:
            if not isinstance(rings,list) or not rings:
                raise ValueError("polygon requires rings")
            for ring in rings:
                if not isinstance(ring,list) or len(ring)<4 or ring[0]!=ring[-1]:
                    raise ValueError("polygon rings require >=4 positions and explicit closure")
        try:polygon=shape(geometry)
        except (ValueError,TypeError,IndexError) as exc:raise ValueError("malformed polygon") from exc
        if polygon.is_empty or not polygon.is_valid or polygon.area<=0:raise ValueError("invalid or zero-area topology")
        result.append({**props,"geometry":polygon})
    # Touching edges are legitimate. Positive-area overlaps would double-count.
    for i,left in enumerate(result):
        for right in result[i+1:]:
            if left["geometry"].intersection(right["geometry"]).area>0:
                raise ValueError(f"overlapping {kind} polygons")
    return result
