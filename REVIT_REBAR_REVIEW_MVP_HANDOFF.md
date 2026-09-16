# Rebar Review MVP — первая настоящая партия Rebar

Дата: 16 сентября 2026. Пакет:
[`qmonitoring-rebar-review-mvp-0.1.2-code-only.zip`](downloads/qmonitoring-rebar-review-mvp-0.1.2-code-only.zip).

Это отдельный review-only путь для Revit 2024/pyRevit. Он принимает полную
прямую партию `graphic-bar-plan-draft/v1`, `graphic-bar-plan-pruned/v1` или `graphic-bar-plan-repaired/v1`,
создаёт **каждый стержень** как native `DB.Structure.Rebar`, выполняет Commit и
строгий post-Commit readback. Только после совпадения всей партии появляется
отдельный вопрос Keep. Отказ и любая ошибка откатывают всю TransactionGroup.

Короткий сценарий:

1. Распаковать ZIP в `C:\QMonitoring\RebarReview\`, содержащую `QMonitoringRebarReview.extension`.
   В pyRevit Settings → Custom Extension Directories добавить именно `C:\QMonitoring\RebarReview\`, не ZIP, не `script.py`, не саму `.extension`.
2. Открыть локальную/отсоединённую **копию** RVT 2024, выделить native Floor,
   соответствующую выбранному JSON: **Пм-1 К09 для К09**, С1 только для JSON С1.
   Matching Floor С1 в переданных RVT не подтверждена; при её отсутствии нужно
   штатно подготовить соответствующую плоскую native Floor в отдельной копии.
   Плагин Floor не создаёт и автоматическую DXF↔RVT-привязку не выполняет.
3. Запустить `QMonitoringRebarReview → Review → Rebar Review`.
4. Выбрать полный graphic JSON, новый путь JSON-отчёта, явный XY и четыре
   глубины осей. Выбрать точные загруженные RebarBarType для каждого steel/D.
5. Подтвердить создание всей партии. После readback выбрать Keep или полный
   rollback. Команда не Save/Sync; после Keep модель сохраняет пользователь.

Readback проверяет все element ID, исходные bar ID в Comments, host, тип,
nominal/model D, количество, отсутствие hooks, единственную финальную прямую
ось XYZ, длину и массу из фактически прочитанных осей. Допуск — 0,01 мм.

Граница MVP намеренно узкая: одна planar top и bottom native-грань,
совпадающие внешние line-loop либо отдельный flat200 top-subset-bottom профиль К09.
Коллинеарное разбиение рёбер не меняет геометрию. BBox не подставляется. Тело должно
лежать в ОБОИХ native наружных контурах и толщине, но отверстия, cover, перепады и фоновая
арматура исключены и остаются `not_checked`. Наклон/составная геометрия/кривые
блокируются. Нужны существующие прямые RebarShape; новых форм, загибов и муфт
команда не создаёт. Coverage/40d/stock `fail` не превращаются в pass.

Keep означает только диагностическую арматуру в копии: `placement_eligible=false`,
`engineering_approval=false`. Первый запуск целой партии в настоящем Revit ещё
не выполнен; после него нужны JSON-отчёт, traceback при ошибке и время выполнения.

Для К09: выбери Пм-1 в копии (snapshot ID11020633,200мм,Z6910..7110), полныйJSON
К09, явныйXY и4глубины, существующие прямые RebarShape/точныеD/steel. Наtop есть
вырез300×180мм, отсутствующий наbottom; поэтому каждыйbar проверяется по обоим
контурам. НовыйID в копии допустим только при проверяемойгеометрии; промежуточный
Solid не сертифицируется. Это не liveRevit approval и не40dPASS.
Ранний blocked_setup теперь сохраняет UUID setupJSON в `%TEMP%`, показывает
причину/путь и выводит traceback; `Отчёт: None` больше не используется.
Исходные conditional3D относятся к D-dependent ResearchLayerProfile, не к четырём
фиксированным UI-глубинам. NativeZ перечитываются строго, но их collision-статус
остаётся not_checked; source_checks не являются actualcollisionpass.

Offline проверка текущей полной К09 dual-exterior партии: 918 стержней /
2864,768770746 кг / 106 позиций, XY0;0. Все 918 проходят оба native-контура
snapshot и толщину; readback doubles перечитывают все 918 и отвергают tamper.
Для текущего К09 условный диагностический ввод: **`50;70;50;70` мм**
(низ X; низ Y; верх X; верх Y), от native-грани до ОСИ, не cover; 0 блокируется.
Оси Z: 6960;6980;7060;7040 мм. Dmax: низ 12 / верх 16 мм. Минимальный вертикальный зазор
между ортогональными телами: низ 8 / верх 4 мм, верх-низ 46 мм. Все 918 проходят именно
этот двухконтурный preflight offline, не утверждённый инженерный Z-profile.
FE presence остаётся fail (10 КЭ), 40d fail (143 КЭ), условные исходные
коллизии 12 пар / stock fail; actual RVT collisions not_checked. Создание 918 стержней
настоящим API ещё не запускалось: после запуска нужен обязательный nativeJSON.
