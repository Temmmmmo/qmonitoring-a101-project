"""Generate the bounded core-to-Revit test packet without private DXF/RVT inputs."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from rebar.application.core_revit_trial import make_core_revit_trial_sample


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--check", type=Path, help="Check an existing sample without rewriting it")
    args = parser.parse_args()
    if args.output and args.check:
        parser.error("choose --output or --check")
    packet = make_core_revit_trial_sample()
    content = json.dumps(packet, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if args.check:
        if json.loads(args.check.read_text(encoding="utf-8")) != packet:
            raise ValueError("sample differs from the current core output")
        print("Sample matches core output")
    elif args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            stream.write(content)
        print(args.output)
    else:
        print(content, end="")


if __name__ == "__main__":
    main()
