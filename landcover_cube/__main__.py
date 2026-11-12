import argparse
import json
from pathlib import Path
from .pipeline import run


def main():
    parser=argparse.ArgumentParser(description="Build synthetic planar land-cover overlay data")
    parser.add_argument("--zones",type=Path,default=Path("data/zones.geojson"))
    parser.add_argument("--parcels",type=Path,default=Path("data/parcels.geojson"))
    parser.add_argument("--output",type=Path,default=Path("run/coverage"))
    args=parser.parse_args()
    try:report=run(args.zones,args.parcels,args.output)
    except (ValueError,OSError) as exc:parser.exit(2,f"overlay failed: {exc}\n")
    print(json.dumps(report["zones"],sort_keys=True))


if __name__=="__main__":main()
