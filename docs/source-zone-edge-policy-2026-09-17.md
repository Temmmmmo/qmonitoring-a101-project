# Краевой envelope policy

`source-edge-total-80d/research-v1` — явная MVP-гипотеза, а не замена strict
40d с каждого конца. Она сохраняет logical core, фазу, оси, длину, массу и число
стержней; проверяет containment core, суммарные `80d` и DXF exterior без отверстий.
Native Revit host, тело стержня, cover и 3D остаются `not_checked`.

Для v0 сохранённого K09: 139 компонентов, 25 outside envelopes перенесены внутрь,
32 остаются; все 32 имеют logical core envelope вне exterior (4/5/10/13 по
bottom-X/Y/top-X/top-Y). Старый strict checker для скорректированного пакета должен
оставаться отдельным: он находит 24 нарушения 40d с каждого конца.

Фундаментальные причины не сводятся к К09: прямоугольник пересекает вогнутость;
крайние snapped axes шире core; global median-based `2 КЭ` может не поместиться в
локальной особенности неравномерной сетки; следующая catalog length может быть
длиннее общего коридора. Точный сервис строит общий longitudinal corridor по
сечениям в vertex/midpoint events и финально вызывает `covers(rect)`.

Регрессия `test_partition_before_edge_fit_can_preserve_coverage_on_an_l_shape`
задаёт независимую от K09 L-плиту `6000×6000` с 27 КЭ по сетке 1000 мм, тремя
КЭ повышенного спроса и неизменной схемой добавки `@150`: интервалы 100/200 мм
относительно фона `@300`. Одна зона
`[1000,1000,5000,5100]` проходит исследовательское coverage, но её core-frame
не помещается в L-контур. Две зоны `[1000,1000,5000,3000]` и
`[1000,3000,2000,5100]` при catalogue lengths 4875/1950 мм также дают ноль
непокрытых КЭ и проходят edge-fit. Это показывает гипотезу «сначала разбить
зону, затем подгонять к контуру», но не решает автоматически 32 случая K09.

```bash
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src artifacts/ga_solver_env/bin/python \
  -c 'import json; from rebar.reporting.source_edge_placement import apply_source_edge_placement; r=json.load(open("artifacts/engineering_variants_2026_09_16/latest-v2/report.json")); p=r["source_graphics"]; _, c=apply_source_edge_placement(p); print(c)'
```

Команда применяет точный postprocess к уже сохранённому source packet; она не
запускает GA и не заменяет strict-сертификат 40d.

Постпроцесс подключён к `physical_web_report` и `flat_mvp_source_web_report`:
SVG, source JSON и его Revit-потребитель получают одинаковые изменённые
координаты; исходные кандидаты и физическая партия не переписываются.
Неразмещённые компоненты остаются в JSON с причиной отказа, а не скрываются.
Для нового расчёта на сервере применяется новая политика; уже скачанный старый
JSON сам не изменяется. Workflow 81 не требует переустановки ради новых координат.

Локальная проверка: **107 passed** (application, source/edge geometry,
frontend и Workflow 81), Ruff, `node --check`, `git diff --check` успешны.
Pure consumer Workflow 81 прочитал скорректированный К09 во всех направлениях:
20/13/50/56 компонентов. Native создание семейств в живом Revit этим прогоном
не проверено. Общий запас 80d и продольное сохранение core проходят для всех
139 компонентов; внешний DXF-контур не пройден у 32. Сравнение с инженером,
раскрой и реальные коллизии этим переносом не объявляются закрытыми.
