"""Areal overlay with explicit uncovered geometry and conservation checks."""
import math
from collections import defaultdict
from shapely import union_all
from shapely.geometry import mapping,GeometryCollection


def polygonal(geometry):
    if geometry.geom_type in {"Polygon","MultiPolygon"}:return geometry
    if geometry.geom_type=="GeometryCollection":
        parts=[polygonal(g) for g in geometry.geoms]
        return union_all([p for p in parts if not p.is_empty])
    return GeometryCollection()


def overlay(zones,parcels):
    footprint=union_all([p["geometry"] for p in parcels])
    fragments=[];summaries=[]
    for zone in sorted(zones,key=lambda z:z["zone_id"]):
        geom=zone["geometry"];classes=defaultdict(float);covered=0.0
        for parcel in sorted(parcels,key=lambda p:p["parcel_id"]):
            intersection=polygonal(geom.intersection(parcel["geometry"]))
            if intersection.is_empty or intersection.area==0:continue
            area=intersection.area;covered+=area;classes[parcel["class"]]+=area
            fragments.append({"type":"Feature","properties":{"zone_id":zone["zone_id"],"parcel_id":parcel["parcel_id"],"class":parcel["class"],"area_m2":area,"coverage_kind":"classified"},"geometry":mapping(intersection)})
        missing=polygonal(geom.difference(footprint));uncovered=missing.area
        if uncovered>0:
            fragments.append({"type":"Feature","properties":{"zone_id":zone["zone_id"],"parcel_id":None,"class":None,"area_m2":uncovered,"coverage_kind":"unclassified"},"geometry":mapping(missing)})
        if not math.isclose(covered+uncovered,geom.area,rel_tol=1e-9,abs_tol=1e-8):raise ValueError("area conservation failed")
        summaries.append({"zone_id":zone["zone_id"],"zone_area_m2":geom.area,"classified_area_m2":covered,"unclassified_area_m2":uncovered,"coverage_fraction":covered/geom.area,"class_area_m2":dict(sorted(classes.items()))})
    return fragments,summaries
