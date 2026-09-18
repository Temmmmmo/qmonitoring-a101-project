#!/usr/bin/env python3
"""Ridge/LOPO по готовым полным zone-merge отчётам и проверенным числам инженера.

Manifest JSON: {"schema_version":"zone-preference-manifest/v1","cases":[
  {"case_id":"...","report_path":"...json","reference_mass_kg":1234,
   "reference_bar_count":100,"reference_source":"проверенная спецификация PDF"}]}
Пути к отчётам относительны каталогу manifest. 3-й и 9-й этажи одного проекта
должны иметь один case_id: LOPO удерживает их вместе.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from rebar.learning.zone_preference import ZoneObservation, build_bundle, evaluate, input_fingerprint, validated_front


def _json_bytes(data: bytes, name: str):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"{name}: повторный JSON-ключ {key!r}")
            result[key] = value
        return result

    return json.loads(data, object_pairs_hook=unique, parse_constant=lambda value: (_ for _ in ()).throw(
        ValueError(f"{name}: недопустимое число {value}")))


def load_manifest(path: Path) -> tuple[tuple[ZoneObservation, ...], str]:
    data = path.read_bytes()
    manifest = _json_bytes(data, str(path))
    if not isinstance(manifest, dict) or set(manifest) != {"schema_version", "cases"} or manifest["schema_version"] != "zone-preference-manifest/v1":
        raise ValueError("нужен manifest zone-preference-manifest/v1 с полем cases")
    if not isinstance(manifest["cases"], list) or not manifest["cases"]:
        raise ValueError("manifest должен содержать непустой список cases")
    observations, seen_paths, references = [], set(), {}
    expected = {"case_id", "report_path", "reference_mass_kg", "reference_bar_count", "reference_source"}
    for row in manifest["cases"]:
        if not isinstance(row, dict) or set(row) != expected:
            raise ValueError("каждая запись cases должна содержать case_id, report_path, reference_mass_kg, reference_bar_count, reference_source")
        case = row["case_id"]
        if not isinstance(case, str) or not case.strip() or not isinstance(row["reference_source"], str) or not row["reference_source"].strip():
            raise ValueError("нужны непустые case_id и reference_source")
        mass, bars = row["reference_mass_kg"], row["reference_bar_count"]
        # weak_distance validates positive finite values, including bool/zero rejection.
        from rebar.learning.zone_preference import weak_distance
        weak_distance((1.0, 1, 1), mass, bars)
        reference = (mass, bars, row["reference_source"])
        if case in references and references[case] != reference:
            raise ValueError(f"повторный case_id {case!r} имеет разные инженерные метки")
        references[case] = reference
        relative = row["report_path"]
        if not isinstance(relative, str) or not relative.strip():
            raise ValueError("нужен report_path")
        report_path = (path.parent / relative).resolve()
        if report_path in seen_paths:
            raise ValueError("один report_path не должен повторяться в manifest")
        seen_paths.add(report_path)
        report_bytes = report_path.read_bytes()
        report = _json_bytes(report_bytes, str(report_path))
        front = validated_front(report)
        if len(front) < 2:
            raise ValueError(f"{case}: одноточечный фронт непригоден для калибровки")
        observations.append(ZoneObservation(case, relative, hashlib.sha256(report_bytes).hexdigest(), front,
                                            mass, bars, row["reference_source"], input_fingerprint(report)))
    return tuple(observations), hashlib.sha256(data).hexdigest()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path, required=True, help="новый JSON; существующий файл не перезаписывается")
    parser.add_argument("--bundle-output", type=Path, help="малый пакет модели для исследовательской подсказки")
    args = parser.parse_args(argv)
    if args.output.suffix.casefold() != ".json" or args.output.exists():
        parser.error("выход должен быть новым .json; существующие файлы не перезаписываются")
    if args.bundle_output is not None and (args.bundle_output.suffix.casefold() != ".json"
                                            or args.bundle_output.exists() or args.bundle_output == args.output):
        parser.error("bundle должен быть новым отдельным .json")
    try:
        observations, manifest_sha = load_manifest(args.manifest)
        result = evaluate(observations)
        result["manifest_sha256"] = manifest_sha
        result["reference_provenance"] = {case: source for case, source in sorted({
            o.case_id: o.reference_source for o in observations}.items())}
        bundle = build_bundle(observations, result) if args.bundle_output is not None else None
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, allow_nan=False, indent=2)
        if bundle is not None:
            with args.bundle_output.open("x", encoding="utf-8") as stream:
                json.dump(bundle, stream, ensure_ascii=False, allow_nan=False, indent=2)
    except (OSError, ValueError, TypeError, KeyError) as error:
        parser.error(str(error))
    print(f"LOPO: {result['independent_case_count']} independent cases, {result['report_count']} reports; {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
