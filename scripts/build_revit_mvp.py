"""Revalidate real sources and create a new, ready-to-open DEMO packet (never RVT)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/"src"))
sys.path.insert(0, str(ROOT/"integrations/pyrevit/QMonitoring.extension/lib"))

from qm_mvp_packet import make_demo_plan, mesh_signature, validate_packet
from qm_trial_input import _reject_constant, _unique_object
from rebar.application.revit_mvp import build_revit_mvp_packet
from rebar.dxf_ingest import read_mosaic


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for key in ("dxf", "shk", "reference", "cad-report", "depth-report", "output"):
        parser.add_argument("--"+key, type=Path, required=True)
    args = parser.parse_args()
    if args.output.suffix.lower() != ".json" or args.output.exists():
        parser.error("output должен быть новым JSON")
    try:
        paths = {k: getattr(args, k) for k in ("dxf", "shk", "reference", "cad_report", "depth_report")}
        raw = {}
        for k, path in paths.items():
            if path.suffix.lower() != ("."+k if k in ("dxf", "shk") else ".json"):
                raise ValueError("Unexpected extension " + k)
            limit = 64 * 1024 * 1024 if k == "dxf" else 32 * 1024 * 1024
            with path.open("rb") as stream:
                raw[k] = stream.read(limit + 1)
            if not raw[k] or len(raw[k]) > limit:
                raise ValueError("Empty or oversized input " + k)
        hashes = {k: hashlib.sha256(value).hexdigest() for k, value in raw.items()}
        parsed = {k: json.loads(raw[k].decode("utf-8-sig"), object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                  for k in ("reference", "cad_report", "depth_report")}
        packet = build_revit_mvp_packet(read_mosaic(str(args.dxf), str(args.shk)), parsed["depth_report"],
            parsed["reference"], parsed["cad_report"], source_sha256=hashes)
        validate_packet(packet)
        for mode in ("cad_geometry", "cad_geometry_instance"):
            if mesh_signature(parsed["cad_report"][mode]) != packet["cad"]["mesh_signature"]:
                raise ValueError("Portable CAD signature differs")
        types = {str(int(t["nominal_diameter_mm"])): t for t in parsed["reference"]["reference_bar_types"]}
        plan = make_demo_plan(packet, parsed["reference"]["floor"], types)
        for k, path in paths.items():
            with path.open("rb") as stream:
                if hashlib.sha256(stream.read(len(raw[k])+1)).hexdigest() != hashes[k]:
                    raise ValueError("Source changed during build: " + k)
        content = json.dumps(packet, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(content)
        print(args.output)
        print("DEMO ONLY:", plan["zone_count"], "zones;", len(plan["runs"]), "Rebar sets;",
              plan["physical_bar_count"], "bars;", round(plan["additional_mass_kg"], 3), "kg")
    except (ValueError, KeyError, TypeError, OSError, OverflowError, RecursionError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
