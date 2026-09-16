# Rebar Review MVP — первая настоящая партия Rebar

Дата: 16 сентября 2026. Пакет:
[`qmonitoring-rebar-review-mvp-0.1.1-code-only.zip`](downloads/qmonitoring-rebar-review-mvp-0.1.1-code-only.zip).

Это отдельный review-only путь для Revit 2024/pyRevit. Он принимает полную
прямую партию `graphic-bar-plan-draft/v1`, `graphic-bar-plan-pruned/v1` или `graphic-bar-plan-repaired/v1`,
создаёт **каждый стержень** как native `DB.Structure.Rebar`, выполняет Commit и
строгий post-Commit readback. Только после совпадения всей партии появляется
отдельный вопрос Keep. Отказ и любая ошибка откатывают всю TransactionGroup.

Короткий сценарий:

1. Распаковать ZIP в `C:\QMonitoring\RebarReview\`, содержащую `QMonitoringRebarReview.extension`.
   В pyRevit Settings → Custom Extension Directories добавить именно `C:\QMonitoring\RebarReview\`, не ZIP, не `script.py`, не саму `.extension`.
2. Открыть локальную/отсоединённую **копию** RVT 2024, выделить native Floor,
   соответствующую исходной плите **С1**, не произвольную тестовую плиту.
   Matching Floor в переданных RVT не подтверждена; при её отсутствии нужно
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
совпадающий внешний line-loop и толщина. BBox не подставляется. Тело должно
лежать во внешнем контуре и толщине, но отверстия, cover, перепады и фоновая
арматура исключены и остаются `not_checked`. Наклон/составная геометрия/кривые
блокируются. Нужны существующие прямые RebarShape; новых форм, загибов и муфт
команда не создаёт. Coverage/40d/stock `fail` не превращаются в pass.

Keep означает только диагностическую арматуру в копии: `placement_eligible=false`,
`engineering_approval=false`. Первый запуск целой партии в настоящем Revit ещё
не выполнен; после него нужны JSON-отчёт, traceback при ошибке и время выполнения.
