# Read-only rebuild experiment

`scripts/experiment_source_zone_rebuilding.py` is a bounded diagnostic over the
saved v0 source packet. It does not modify the web response, Revit packet, GA or
physical bars. The baseline rehydrates the original `DemandMap` and zones, keeps
the catalogue profile and evaluates every accepted candidate with the unchanged
`MONOTONE_SINGLE_STO_COVERAGE_POLICY`.

The sequential 30-second v0 diagnostic applied H0 plus transverse relocations, then
rechecked all four directions together. It accepted six relocations:
`bottom-X/source-recovery-6`, `bottom-Y/source-recovery-7`,
`top-X/genetic-7`, `top-X/genetic-47`, `top-Y/genetic-4` and
`top-Y/source-recovery-38`. Outside component frames changed `4/5/10/13 →
3/4/8/11` (32 → 26), with zero uncovered cells and valid geometry in every
direction. Whole inventory changed 139 zones / 1196 bars / 3105.939331 kg /
28 positions to 139 / 1195 / 3101.993729 / 28. Both one-second stock checks are
`not_checked` because diagnostic rehydration omitted the declared steel class. The
original report declares A500: baseline stock is PASS, while the discarded six-change
variant fails it.

Safer product relocation preserves every component's `(diameter, selected length,
bar count)` and phase. On K09 it reduced frames `32 → 28` with exactly unchanged
139 zones / 1196 bars / 3105.939330 kg / 28 positions; the declared A500 stock
check remains PASS. The earlier `32 → 26` variant was rejected: it substituted
minimum catalogue lengths and changed Ø10 batch length by −6400 mm. Other saved
variants observed `36 → 33` and `32 → 28`; the ten-second product budget can time
out and reports unvisited zones rather than claiming infeasibility.

This is evidence only for same-phase, minimum-width window relocation; it is not
host, body, 3D, native Revit or engineering approval. Recursive splits are off by
default and untested alternatives are not infeasible.

## Перестроение зон без изменения партии

Two source producers call the bounded rebuild before exact edge placement, source
SVG and downloaded/Revit JSON. Outer `zone_rebuild` metadata records bounds and
checks; v2 zone fields and physical/front metrics remain unchanged. Strict 40d is
separate (24 K09 violations), not replaced by the total-80d proxy.

H0 uses `contributing_zone_ids`: a zone uniquely covering positive FE is not even
offered for removal. H1 first moves the transverse selection window while retaining
the two-KE minimum and all positive intersections, then probes bounded two-to-four
rectangle partitions. Coverage/outer-frame acceptance requires no new outside
components and a strict reduction. Fixed phase, snapped axes and catalogue length
can still make a local feature unreachable; this experiment does not prove the
contrary.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src artifacts/ga_solver_env/bin/python \
  scripts/experiment_source_zone_rebuilding.py \
  artifacts/engineering_variants_2026_09_16/latest-v2/report.json --seconds 30 \
  --output /private/tmp/source-rebuild-sequential.json
```
