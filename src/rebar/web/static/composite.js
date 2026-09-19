"use strict";
const directions = [
  { layer: "bottom", axis: "X", key: "bottom_x", title: "Низ · X" },
  { layer: "bottom", axis: "Y", key: "bottom_y", title: "Низ · Y" },
  { layer: "top", axis: "X", key: "top_x", title: "Верх · X" },
  { layer: "top", axis: "Y", key: "top_y", title: "Верх · Y" },
];
const q = (selector) => document.querySelector(selector);
const esc = (value) => String(value ?? "").replaceAll("&", "&amp;").replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;").replaceAll('"', "&quot;").replaceAll("'", "&#039;");
const fmt = (value, digits = 2) => value === null || value === undefined ? "—" :
  new Intl.NumberFormat("ru-RU", { maximumFractionDigits: digits }).format(value);
const statuses = { pass: "Пройдено в расчётной модели", fail: "Не выполнено", not_checked: "Не проверено" };
const shortStatuses = {pass: "Да · расчётно", fail: "Не выполнено", not_checked: "Не проверено"};
const explanations = {
  "monotone-component-substitution-engineering-approval": "Эксперимент зон: замена каждой добавки не более слабой требует инженерного согласования.",
  "boundary-trim-engineering-review": "Обрезка у края требует решения анкеровки; это не разрешение размещения",
  "external_boundary": "Есть стержни, тело которых не помещается во внешний контур",
  "slab_material_boundary": "Есть стержни, тело которых не помещается в материал плиты с учётом отверстий",
  "original_FE_geometric_presence": "Не везде сохранено геометрическое наличие стали по исходным КЭ",
  "original_FE_with_control_40d": "Часть исходных КЭ не покрыта с прежним контрольным запасом 40d",
  "additional_3D_collisions": "Пересечения тел добавок при явно назначенных исследовательских высотах",
  "additional_3D_separation_unproven": "Для части пар разделение тел в 3D не доказано",
  "11700_zero_waste_cutting": "Новые длины после обрезки не подтверждены безотходным раскроем 11700 мм",
  "same-plane-additional-bar-intersections": "Пересечения тел дополнительных стержней: требуется проверка узлов",
  "monotone-diameter-substitution-engineering-approval": "Подтверждение замены одиночной добавки на более сильную",
  "permanent-placement-not-authorized": "Постоянное размещение арматуры не разрешено",
  "actual-3d-placement-and-layer-order": "Фактические высоты и совместная 3D-раскладка",
  "source-demand-and-40d-engineering-approval": "Инженерное подтверждение потребности и анкеровки 40d",
  "permanent-placement": "Проверка перед постоянным размещением в рабочем Revit",
  "manual-joints-unresolved": "Оставшиеся стыки требуют инженерного решения",
  "composite-coverage-engineering-acceptance": "Инженерная применимость модели покрытия составными зонами",
  "composite-minimum-width": "Минимальная ширина по фактическим наборам стержней",
  "project-axis-origins-approval": "Проектное подтверждение выбранных фаз",
  "xy-layer-order": "Порядок и высоты X/Y сверху и снизу",
  "background-and-additions-3d-collisions": "Совместная 3D-проверка фона и добавок четырёх направлений",
  "a101-composite-positions": "Применимость нормативного каталога к составным комбинациям",
  "postprocessing-conflicts": "Устранение физических конфликтов между зонами",
  "host-boundary-cover-openings": "Грани, защитный слой и все проёмы фактической плиты",
  "live-host-geometry": "Актуальность и полнота геометрии в открытом Revit",
  "top-bottom-cover": "Защитные слои после назначения высот стержней",
  "coplanar-background-contact-and-depths": "Контакт добавки @100 с фоном с учётом фактических высот",
  "revit-readback": "Полное создание и обратное чтение в Revit",
  "stock-cutting-zero-waste": "Безотходность фактической партии 11700 мм",
  "stock-cutting-manufacturing-assumptions": "Применимость смешанного раскроя с нулевым пропилом к производству",
  "full-plate-solution-not-found": "Полная раскладка в заданных ограничениях не найдена",
  "batch_total_not_multiple_of_stock": "Суммарная длина этого диаметра и класса не кратна 11700 мм",
  "no_exact_cut_pattern": "Из этих длин нельзя составить пруток 11700 мм без остатка",
  "steel_class_not_declared": "Класс стали не задан, объединять материалы нельзя",
  "pattern_search_limit": "Достигнут лимит поиска схем — допустимость не установлена",
  "every_installed_bar_assigned_once": "Каждый установленный стержень учтён ровно один раз",
  "installed_bar_exceeds_stock_or_length_resolution": "Стержень длиннее прутка или длина некорректна",
  "batch_pattern_counts_infeasible": "Схемы существуют, но их количества не складываются в нужную партию",
  "no_balanced_lengths_within_limits": "В проверенном каталоге нет сочетания длин в пределах заданного прироста массы и числа позиций",
  "independent_cutting_not_passed": "Независимая проверка раскроя не подтвердила выбранные длины",
  "time_limit_before_solver": "Исчерпан бюджет поиска; невозможность решения не доказана",
};
let result = null;
let activeDirection = 0;
let busy = false;
let zoom = 1;
let drawingView = "source";
let layoutVariants = [];
let tradeoffZoomed = false;
function setLayoutVariants(payload) {
  result = payload;
  layoutVariants = [];
  if (!Array.isArray(payload.layout_variants)) return;
  const {layout_variants, ...base} = payload;
  layoutVariants = layout_variants.map((variant, i) => ({...variant, report: i === 0 ? base : variant.report}));
  if (layoutVariants.some((variant) => variant.report?.schema_version !== "composite-plate-analysis/v1" ||
      variant.report.directions?.length !== 4 || !variant.report.front?.length)) throw new Error("Неподдержанный формат вариантов раскладки.");
  result = base;
}
q("#direction-inputs").innerHTML = directions.map((d) => `<fieldset><legend>${d.title}</legend>
  <label>Изополе DXF<input name="dxf_${d.key}" type="file" accept=".dxf" required></label>
  <label>Шкала армирования · .shk или PNG<input name="shk_${d.key}" type="file" accept=".shk,.png" required></label></fieldset>`).join("");
q("#placement-inputs").innerHTML = directions.map((d) => `<fieldset data-direction="${d.key}"><legend>${d.title}</legend>
  <label>Координата фоновой оси, мм<input data-param="background_origin_mm" type="number" step="any" placeholder="Из проекта" required></label>
  <label>Сдвиг первой добавки @300 от фона, мм<input data-param="first_300_offset_mm" type="number" step="any" required></label>
  <label>Сдвиг второго набора от фона, мм<input data-param="second_offset_mm" type="number" step="any" required></label></fieldset>`).join("");
q("#direction-tabs").innerHTML = directions.map((d, i) => `<button type="button" data-index="${i}">${d.title}</button>`).join("");

function selectedPoint() { return result?.front[layoutVariants.length ? result.selected_index : Number(q("#candidate").value)]; }
function isTrimmedResult() { return ["boundary-trimmed-physical-bars", "pruned-trimmed-physical-bars", "repaired-trimmed-physical-bars"].includes(result?.output_kind); }
function isPhysicalResult() { return result?.output_kind === "normalized-physical-bars" || isTrimmedResult(); }
function trimDisplayChecks() {
  const trim = result.boundary_trim, cleanup = result.trimmed_cleanup;
  if (result.output_kind !== "pruned-trimmed-physical-bars") return trim;
  // Display current independent checks, NEVER mutate the preceding exact-trim history.
  return {...trim, geometric_presence: cleanup.coverage_after.geometric_presence,
    coverage_with_control_40d: cleanup.coverage_after.control_40d,
    stock_cutting: cleanup.stock_cutting, collisions: cleanup.collisions,
    external_boundary_failures_after: cleanup.material_boundary_failures_after,
    material_boundary_failures_after: cleanup.material_boundary_failures_after,
    opening_intersections_after: cleanup.material_boundary_failures_after,
    actual_Revit_host_informational_failures: undefined};
}
function graphicDownload() {
  if (result?.output_kind === "repaired-trimmed-physical-bars") {
    const packet = result.graphic_bar_plan_repaired;
    return packet?.schema_version === "graphic-bar-plan-repaired/v1" && packet.placement_eligible === false ? packet : null;
  }
  const pruned = result?.output_kind === "pruned-trimmed-physical-bars";
  const packet = pruned ? result.graphic_bar_plan_pruned : result?.graphic_bar_plan_draft;
  const schema = pruned ? "graphic-bar-plan-pruned/v1" : "graphic-bar-plan-draft/v1";
  return packet?.schema_version === schema && packet.placement_eligible === false ? packet : null;
}
function selectedSourceGraphics() {
  const packet = result?.source_graphics;
  if (!packet) return null;
  if (result.source_graphics_mode === "direction-candidates" && !result.output_kind) {
    const point = selectedPoint();
    if (!point || packet.directions?.length !== 4 || point.direction_candidate_indexes?.length !== 4) return null;
    const rows = [];
    for (let i = 0; i < 4; i++) {
      const source = packet.directions[i];
      const candidate = result.directions[i].candidates[point.direction_candidate_indexes[i]];
      if (!candidate || !Array.isArray(candidate.zone_drafts) || candidate.zone_drafts.length !== candidate.metrics?.zone_count ||
          candidate.direction?.layer !== source.direction.layer || candidate.direction?.axis !== source.direction.axis) return null;
      rows.push({...source, zone_drafts: candidate.zone_drafts});
    }
    return {...packet, directions: rows};
  }
  return result.source_graphics_candidate_index === undefined ||
    result.source_graphics_candidate_index === (layoutVariants.length ? result.selected_index : Number(q("#candidate").value)) ? packet : null;
}
function hasSelectedSourceGraphics() { return Boolean(selectedSourceGraphics()); }
function setZoom(value) {
  zoom = Math.min(3, Math.max(1, value));
  const svg = q("#drawing svg");
  if (svg) { svg.style.width = `${zoom * 100}%`; svg.style.maxWidth = "none"; }
  q("#zoom-level").textContent = `${Math.round(zoom * 100)}%`;
  q("#zoom-out").disabled = zoom <= 1;
  q("#zoom-in").disabled = zoom >= 3;
}
function renderComparison(point) {
  const reference = result.engineering_example?.reference;
  const comparable = reference && [reference.mass_kg, reference.physical_bar_count, reference.position_count]
    .some((value) => Number.isFinite(value) && value > 0);
  q("#engineer-comparison").hidden = !point || !comparable;
  q("#comparison-detail").hidden = !point || !comparable;
  if (!point || !comparable) return;
  const rows = [["Масса дополнительной арматуры", point.additional_mass_kg, reference.mass_kg, "кг", 2],
    ["Физические стержни", point.physical_bar_count, reference.physical_bar_count, "шт.", 0],
    ["Позиции спецификации", point.position_count, reference.position_count, "поз.", 0]];
  q("#comparison-rows").innerHTML = rows.map(([label, actual, baseline, unit, digits]) => {
    const valid = Number.isFinite(actual) && Number.isFinite(baseline) && baseline > 0;
    const delta = valid ? actual - baseline : null;
    const deltaText = valid ? `${delta > 0 ? "+" : ""}${fmt(delta, digits)} ${unit} (${delta > 0 ? "+" : ""}${fmt(delta / baseline * 100, 1)}%)` : "Нет сопоставимого значения";
    return `<tr><td>${esc(label)}</td><td>${fmt(actual, digits)} ${unit}</td><td>${fmt(baseline, digits)} ${unit}</td><td>${esc(deltaText)}</td></tr>`;
  }).join("");
  q("#comparison-scope").textContent = [reference.scope, reference.note,
    "Показана разница состава, а не прохождение всех гейтов. Прямые и гнутые стержни, границы и совместная укладка должны проверяться в одинаковом объёме."].filter(Boolean).join(" ");
}
function renderChecks(point, blockers) {
  const sourceCheck = hasSelectedSourceGraphics() ? result.source_zone_checks : null;
  const edgeCheck = hasSelectedSourceGraphics() ? result.source_zone_edge_checks : null;
  const rebuildCheck = hasSelectedSourceGraphics() ? result.source_zone_rebuild_checks : null;
  q("#source-zone-check-status").textContent = sourceCheck?.status === "pass"
    ? `Огибающая компонентов: 40d проверено; ${fmt(sourceCheck.component_count, 0)} компонентов, 0 нарушений. Этот контроль оценивает только 40d; фактическая плита Revit не проверена.`
    : sourceCheck?.status === "fail"
      ? `Огибающая компонентов: есть нарушения 40d; ${fmt(sourceCheck.component_count, 0)} компонентов, ${fmt(sourceCheck.violation_count, 0)} нарушений. Этот контроль оценивает только 40d; фактическая плита Revit не проверена.`
      : "Огибающая 40d исходного пакета не сертифицирована для выбранного варианта; фактическая плита Revit не проверена.";
  if (edgeCheck) {
    const edgeStatus = (value) => value === "pass" ? "сохранён" : value === "fail" ? "нарушен" : "не проверен";
    q("#source-zone-check-status").textContent += ` Краевой запас 80d: ${edgeStatus(edgeCheck.total_extension_status)}; внутри контура ${fmt(edgeCheck.placed_count, 0)}, исправлено ${fmt(edgeCheck.fixed_count, 0)}, за контуром ${fmt(edgeCheck.after_outside_count, 0)}, непроверенных направлений ${fmt(edgeCheck.unknown_count, 0)}. Сохранение логического ядра: ${edgeStatus(edgeCheck.longitudinal_core_containment_status)}. 40d с каждого конца — отдельный контроль; DXF-контур, не модель Revit.`;
  }
  if (rebuildCheck) q("#source-zone-check-status").textContent += rebuildCheck.status === "pass"
    ? ` Перестроение зон: ${fmt(rebuildCheck.before_outside_count, 0)}→${fmt(rebuildCheck.after_outside_count, 0)}; полное исходное покрытие подтверждено в принятой модели. Исходные зоны: ${fmt(rebuildCheck.after_metrics?.zone_count, 0)}, ${fmt(rebuildCheck.after_metrics?.bar_count, 0)} стержней, ${fmt(rebuildCheck.after_metrics?.mass_kg, 3)} кг, ${fmt(rebuildCheck.after_metrics?.position_count, 0)} позиций — это не физическая партия.`
    : " Перестроение зон исходного пакета не выполнено.";
  const selected = point ? result.directions.map((direction, i) => direction.candidates[point.direction_candidate_indexes[i]]) : [];
  const covered = selected.length === 4 && selected.every((candidate) => candidate?.coverage?.uncovered_cell_count === 0);
  const hostChecks = selected.map((candidate) => candidate?.host_preflight?.checks?.planar_host_and_openings);
  const hostStatus = hostChecks.length === 4 && hostChecks.every((status) => status === "pass") ? "pass"
    : hostChecks.some((status) => status === "fail") ? "fail" : "not_checked";
  const pairs = result.same_plane_conflicts?.body_intersection_count;
  let rows = [["Покрытие исходной потребности", covered ? "pass" : point ? "fail" : "not_checked",
    covered ? "Все исходные КЭ покрыты в принятой модели." : "Полное покрытие выбранного варианта не подтверждено."],
    ["Раскрой прутка 11,7 м", point?.stock_cutting?.status || "not_checked", "Проверка всей партии, смешанный рез и нулевой пропил."],
    ["Пересечения стержней одного направления", Number.isInteger(pairs) ? (pairs ? "fail" : "pass") : "not_checked",
      Number.isInteger(pairs) ? `${fmt(pairs, 0)} пар в плоской модели. Это не проверка фактических высот и 3D.` : "Проверка физических стержней не выполнена."],
    ["Границы и проёмы плиты", hostStatus, hostStatus === "not_checked" ? "Нужна независимая проверка фактической геометрии host." : "Проверено только в переданной плоской модели host."],
    ["Создание в Revit", "not_checked", "Этот web-расчёт не изменял и не читал обратно модель."]];
  if (result.mvp_checks && isTrimmedResult()) {
    const checks = result.mvp_checks, trim = trimDisplayChecks();
    const asStatus = (key) => ["pass", "fail", "not_checked"].includes(checks[key]) ? checks[key] : "not_checked";
    rows = [["Внешний контур плиты", asStatus("outer_boundary"),
      `${fmt(trim.external_boundary_failures_after, 0)} стержней вне контура.`],
      ["Наличие стали на исходных КЭ", asStatus("original_demand_presence"),
        `${fmt(trim.geometric_presence.uncovered_cell_count, 0)} КЭ с неполным наличием. Исходный спрос сохранён.`],
      ["Контроль 40d", asStatus("control_40d"),
        `${fmt(trim.coverage_with_control_40d.uncovered_cell_count, 0)} КЭ с неполным покрытием.`],
      ["Раскрой 11,7 м", asStatus("stock_11700"),
        "Вся партия; нулевой пропил и смешанный рез — допущения."],
      ["3D-пары · условные высоты", trim.collisions.proven_collision_pair_count ||
        trim.collisions.uncertain_pair_count ? "fail" : "pass",
        `${fmt(trim.collisions.proven_collision_pair_count, 0)} пересечений, ${fmt(trim.collisions.uncertain_pair_count, 0)} неопределённых. Не факт Revit.`]];
  }
  else if (isTrimmedResult()) {
    const trim = trimDisplayChecks();
    rows = [["Наличие стали на исходных КЭ — не анкеровка", trim.geometric_presence.status,
      `${fmt(trim.geometric_presence.uncovered_cell_count, 0)} КЭ с непокрытой частью. Исходные полигоны не изменены; достаточные зоны объединены без суммы слабых As.`],
      ["Исходная потребность с контрольными 40d", trim.coverage_with_control_40d.status,
        `${fmt(trim.coverage_with_control_40d.uncovered_cell_count, 0)} КЭ с непокрытой частью после обрезки. Наличие стали само по себе не подтверждает анкеровку.`],
      ["Внешний контур рабочего снимка", trim.external_boundary_failures_after ? "fail" : "pass",
        `До: ${fmt(trim.external_boundary_failures_before, 0)}, после: ${fmt(trim.external_boundary_failures_after, 0)} стержней вне контура. ` +
        `${fmt(trim.removed_input_bars?.length ?? trim.removed_wholly_external_bars?.length ?? 0, 0)} исходных стержней без пересечения с материалом — не оставлено ни отрезка; исходные КЭ проверены. Защитный слой не добавлен.`],
      ["Обрезка у отверстий", trim.respect_openings ? (trim.material_boundary_failures_after ? "fail" : "pass") : "not_checked",
        trim.respect_openings ? `Отверстия затрагивали ${fmt(trim.opening_affected_input_bar_count, 0)} исходных стержней; ` +
          `после разрезания пересечений с отверстиями: ${fmt(trim.opening_intersections_after, 0)}. ` +
          "Каждый оставшийся отрезок учтён в массе и ведомости; потребность КЭ не вырезается." : "В этом отчёте отверстия исключены из обрезки."],
      ["3D-пересечения добавок", trim.collisions.proven_collision_pair_count || trim.collisions.uncertain_pair_count ? "fail" : "pass",
        `${fmt(trim.collisions.proven_collision_pair_count, 0)} пересечений, ${fmt(trim.collisions.uncertain_pair_count, 0)} неопределённых пар. Высоты исследовательские, не измеренная арматура; существующий фон не проверен.`],
      ["Новый раскрой 11,7 м", trim.stock_cutting.status,
        "Вся изменённая партия проверена заново. Старый сертификат раскроя не используется."],
      ["Фактический host с проёмами и cover", trim.actual_Revit_host_informational_failures === undefined ? "not_checked"
        : trim.actual_Revit_host_informational_failures ? "fail" : "pass",
        trim.actual_Revit_host_informational_failures === undefined ? "После удаления проверено тело в материале без cover. Старый счётчик защитного слоя не выдан за новую проверку."
          : `${fmt(trim.actual_Revit_host_informational_failures, 0)} геометрических отказов. Информационно: защитный слой в этой операции не учтён; это не подтверждение размещения в Revit.`],
      ["Инженерная анкеровка и Revit", "not_checked", "Геометрический рисунок не является расчётом узла анкеровки или размещённой арматурой."]];
    if (result.output_kind === "pruned-trimmed-physical-bars") {
      const cleanup = result.trimmed_cleanup;
      rows.unshift(["Удаление резервных отрезков — отдельный этап", cleanup.accepted_nonregression ? "pass" : "fail",
        `${fmt(cleanup.physical_metrics_before.physical_bar_count, 0)} → ${fmt(cleanup.physical_metrics.physical_bar_count, 0)} стержней; ` +
        `удалено ${fmt(cleanup.removed_bar_count, 0)}. Ранее покрытые части исходных КЭ сохранены для наличия стали и 40d. ` +
        "Существовавшие пробелы не исправлены. Длины не округлялись; полный состав до удаления сохранён в экспорте."]);
    }
  }
  q("#check-summary").innerHTML = rows.map(([title, status, note]) => `<div class="check-card" data-status="${esc(status)}"><span>${esc(shortStatuses[status] || status)}</span><strong>${esc(title)}</strong><p>${esc(note)}</p></div>`).join("");
  const failed = rows.filter(([, status]) => status === "fail").length;
  q("#physical-check-warning").textContent = failed ? `· ${failed} не пройдено` : "· не завершены";
  q("#gate-status").dataset.status = failed ? "fail" : "incomplete";
  q("#gate-status").textContent = failed
    ? `Нет полной инженерной проверки: ${failed} расчётных условий не выполнено. Исправьте их до выпуска раскладки.`
    : "Полная инженерная проверка не завершена. Непроверенные условия и Revit требуют отдельного решения.";
  q("#blocker-title").textContent = `Все незакрытые условия · ${blockers.length}`;
}
function renderDirection() {
  const point = selectedPoint();
  const direction = result.directions[activeDirection];
  const candidate = point ? direction.candidates[point.direction_candidate_indexes[activeDirection]] : null;
  const sourceMatches = hasSelectedSourceGraphics();
  const source = sourceMatches ? selectedSourceGraphics().directions?.[activeDirection] : null;
  const sourceZones = (sourceMatches ? direction.source_zone_drafts : null) ?? candidate?.zone_drafts ?? [];
  const physical = isPhysicalResult();
  // Never derive source rectangles from normalized bars or crop bars to FE/host bounds.
  q("#drawing").innerHTML = drawingView === "source"
    ? (sourceMatches ? direction.source_svg : null) ?? candidate?.svg ?? direction.input_svg ?? ""
    : drawingView === "combined" ? candidate?.overlay_svg ?? candidate?.svg ?? direction.input_svg ?? ""
    : candidate?.svg ?? direction.input_svg ?? ""; // Server's escaped geometry renderer only.
  q("#drawing").dataset.view = drawingView;
  q("#drawing").dataset.envelopes = q("#source-envelopes").checked ? "shown" : "hidden";
  q("#source-envelope-control").hidden = drawingView === "physical";
  q("#drawing-layers").hidden = drawingView === "source";
  q("#source-zone-details").hidden = drawingView === "physical";
  q("#source-legend").hidden = drawingView === "physical";
  q("#drawing-views").querySelectorAll("button").forEach((button) => button.setAttribute("aria-pressed", button.dataset.view === drawingView));
  q("#drawing-view-note").textContent = drawingView === "source"
    ? (result.zone_boundary_policy === "zone_footprints_clipped_to_union_of_source_kleenka_cells"
      ? "Показана действующая область зон по цветным КЭ; белые участки исключены. Пунктирные огибающие осей можно включить отдельно. Геометрия физических стержней требует своей проверки."
      : "Исходные изополя и параметрические зоны до физической обработки. Это не физическая ведомость и не размещённая арматура. Прямоугольники не заменены контуром нормализованных стержней.")
    : (isTrimmedResult() ? (result.output_kind === "repaired-trimmed-physical-bars"
        ? "Партия после обрезки и локального repair: добавленные стержни и сдвиги осей явно записаны в JSON. Внешний контур"
        : "Новая физическая партия: отрезки реально укорочены/разделены по внешнему контуру") +
        (result.boundary_trim.respect_openings ? " и отверстиям" : "") +
        (result.mvp_domain ? " плоской модели MVP по DXF. Это не обрезка картинки. " : " рабочего снимка. Это не обрезка картинки. ")
      : physical ? "Физическая партия после обработки; схема и ведомость относятся к одним стержням. "
      : "Оси стержней параметрического кандидата; физическая нормализация для этого расчёта не выполнена. ") +
      (isTrimmedResult()
        ? "Показаны новые физические концы; дополнительных масок и скрытия оставшихся нарушений нет. Проверки приведены ниже."
        : "Выходы за контур и пересечения не обрезаются и не скрываются. Их проверки приведены ниже; отсутствие видимой ошибки не заменяет проверку.");
  if (drawingView === "combined") q("#drawing-view-note").textContent +=
    " Прямоугольники Z — исходные зоны потребности, линии — стержни выбранной партии. " +
    "После обработки их границы могут различаться: прямоугольник не является новым контуром стали.";
  q("#source-zone-summary").textContent = `${directions[activeDirection].title}: ${fmt(sourceZones.length, 0)} исходных зон; ` +
    `${fmt(sourceZones.reduce((sum, zone) => sum + zone.components.reduce((n, c) => n + c.bar_count, 0), 0), 0)} стержней до физической обработки. Это не количество в итоговой физической партии.`;
  q("#source-zone-rows").innerHTML = sourceZones.flatMap((zone, zi) => zone.components.map((component) => {
    const box = zone.demand_bbox_mm, x = zone.direction.axis === "X";
    const length = x ? box[2] - box[0] : box[3] - box[1];
    const width = x ? box[3] - box[1] : box[2] - box[0];
    const gaps = [...new Set(component.axis_coordinates_mm.slice(1).map((value, i) =>
      Number((value - component.axis_coordinates_mm[i]).toFixed(6))))].sort((a, b) => a-b);
    return `<tr><td><strong>Z${zi + 1} / ${component.component_index + 1}</strong><br>${esc(zone.source_zone_id)}</td>` +
      `<td>${fmt(length, 3)} × ${fmt(width, 3)}</td><td>${fmt(component.installed_length_mm, 3)} × ${fmt(component.axis_window_mm[1] - component.axis_window_mm[0], 3)}</td>` +
      `<td>${fmt(component.diameter_mm)}</td><td>${fmt(component.nominal_step_mm)}</td><td>${fmt(component.bar_count, 0)}</td>` +
      `<td>${gaps.length ? gaps.map((gap) => fmt(gap, 3)).join(" / ") : "Одна ось"}</td></tr>`;
  })).join("");
  q("#source-legend").innerHTML = (source?.legend || []).map((level) => {
    const validColor = Array.isArray(level.rgb) && level.rgb.length === 3 && level.rgb.every((n) => Number.isInteger(n) && n >= 0 && n <= 255);
    const color = validColor ? `rgb(${level.rgb.join(",")})` : "#d9e4ec";
    return `<span><i style="background:${color}"></i>${esc(level.label || `Уровень ${level.level_index}`)}</span>`;
  }).join("");
  renderDrawingLegend();
  setZoom(zoom);
  q("#installation-notes").innerHTML = [...new Set((candidate?.installation_notes || []).map((item) => item.note))]
    .map((note) => `<li>${esc(note)} Высоты осей ещё не назначены; фактическое касание не подтверждено.</li>`).join("");
  q("#direction-status").textContent = candidate
    ? `${directions[activeDirection].title}: ${fmt(candidate.coverage.uncovered_cell_count, 0)} непокрытых КЭ из исходного спроса; ` +
      (candidate.host_preflight ? `границы: ${statuses[candidate.host_preflight.checks.planar_host_and_openings]}` : "host не проверен")
    : `${directions[activeDirection].title}: ${direction.candidates.length} кандидатов. ` +
      (direction.telemetry?.host_demand_feasibility?.requires_engineering_decision
        ? `Несовместимы с текущей анкеровкой у края: ${direction.telemetry.host_demand_feasibility.cell_count} КЭ, выделены красным. Спрос не удалён.`
        : "Полного решения в заданном конечном поиске не найдено; показан исходный спрос, лимиты не ослаблены. " +
          (direction.telemetry?.unmeshed_zone_rejected_candidates ? `${direction.telemetry.unmeshed_zone_rejected_candidates} прямоугольников отклонены: они заходят в белые области без КЭ. ` : "") +
          (direction.telemetry?.host_rejected_candidates ? `${direction.telemetry.host_rejected_candidates} кандидатов отклонены проверкой границ/проёмов/конфликтов.` : ""));
  if (candidate && isTrimmedResult()) {
    q("#direction-status").textContent = `${directions[activeDirection].title}: геометрическое наличие — ` +
      `${fmt(candidate.geometric_presence.uncovered_cell_count, 0)} непокрытых КЭ; с прежними 40d — ` +
      `${fmt(candidate.coverage.uncovered_cell_count, 0)}. Эти проверки не взаимозаменяемы; чёрным показан ` +
      (result.mvp_domain ? "условный внешний контур плоской DXF-модели." : "внешний контур сечений настоящего host.");
  }
  q("#direction-tabs").querySelectorAll("button").forEach((button, i) => button.setAttribute("aria-pressed", i === activeDirection));
}
function validEngineeringPreference(front) {
  const preference = result?.engineering_preference;
  const validIndex = index => Number.isInteger(index) && index >= 0 && index < front.length;
  return preference?.status === "available" && validIndex(preference.recommended_index) &&
    Array.isArray(preference.top_indexes) && preference.top_indexes.length > 0 &&
    preference.top_indexes.every(validIndex) ? preference : null;
}
function renderZoneTradeoff() {
  const enabled = result?.complexity_axis === "zone_count" && !layoutVariants.length;
  q("#zone-tradeoff").hidden = !enabled;
  q("#engineering-preference").hidden = true;
  if (!enabled) return;
  const front = result.front || [];
  renderEngineeringPreference(front);
  const preference = validEngineeringPreference(front);
  const preferenceIndexes = preference ? [...new Set(preference.top_indexes)].slice(0, 3) : [];
  const knee = result.zone_tradeoff?.knee;
  const chosenValue = Number(q("#candidate").value);
  const chosen = Number.isInteger(chosenValue) && chosenValue >= 0 && chosenValue < front.length ? chosenValue : 0;
  const hasKnee = knee?.status === "candidate" && Number.isInteger(knee.index) &&
    knee.index >= 0 && knee.index < front.length;
  q("#select-zone-knee").hidden = !hasKnee;
  const markersCoincide = hasKnee && preferenceIndexes.includes(knee.index);
  q("#zone-tradeoff-legend").textContent =
    `${hasKnee ? "● Жёлтая точка — геометрический перегиб. " : ""}` +
    `${preferenceIndexes.length ? `● ${preferenceIndexes.length} ${preferenceIndexes.length === 1 ? "зелёная точка" : "оттенка зелёного"} — варианты по инженерным примерам, без утверждённого победителя. ` : ""}` +
    `${markersCoincide ? "Зелёная точка с жёлтым кольцом совпадает с геометрическим перегибом. " : ""}` +
    "Тёмная обводка — выбранный вариант.";
  q("#zone-tradeoff-scope").textContent = "Масса дополнительного армирования после 40d и выбранного раскроя. " +
    "Зоны, стержни и позиции считаются отдельно. Показаны только найденные проверенные варианты, не глобальный оптимум. " +
    (result.zone_tradeoff?.timed_out ? "Лимит времени достигнут: показаны только полностью проверенные варианты. " : "") +
    (result.diagnostic_front_before_cutting?.length ? "Кривая ограничена вариантами с успешным подбором безотходного раскроя." : "");
  q("#zone-knee-note").textContent = hasKnee
    ? `Геометрический перегиб: вариант ${knee.index + 1}, ${fmt(front[knee.index].zone_count, 0)} зон, ` +
      `${fmt(front[knee.index].additional_mass_kg)} кг. Это подсказка по форме кривой; инженерное предпочтение она не подтверждает.`
    : front.length ? "Выраженный геометрический перегиб не найден: вариантов недостаточно или кривая почти прямая. При отсутствии другой подсказки резервный выбор — минимум массы."
      : "Полного разбиения в заданных ограничениях не найдено. Исходный спрос не сокращён.";
  if (!front.length) {
    q("#zone-tradeoff-chart").innerHTML = "";
    q("#zone-selected-title").textContent = "Нет проверенных вариантов";
    ["#zone-selected-zones", "#zone-selected-mass", "#zone-selected-bars", "#zone-selected-positions", "#zone-selected-counter"]
      .forEach(selector => { q(selector).textContent = "—"; });
    ["#select-zone-min-zones", "#select-zone-previous", "#select-zone-next", "#select-zone-min-mass"]
      .forEach(selector => { q(selector).disabled = true; });
    tradeoffZoomed = false;
    q("#zone-toggle-zoom").disabled = true;
    q("#zone-toggle-zoom").setAttribute("aria-pressed", "false");
    q("#zone-toggle-zoom").textContent = "Увеличить выбранный участок";
    q("#zone-visible-range").textContent = "—";
    return;
  }
  const selected = front[chosen] || front[0];
  q("#zone-selected-title").textContent = `Вариант ${chosen + 1} из ${front.length}`;
  q("#zone-selected-zones").textContent = fmt(selected.zone_count, 0);
  q("#zone-selected-mass").textContent = `${fmt(selected.additional_mass_kg)} кг`;
  q("#zone-selected-bars").textContent = fmt(selected.physical_bar_count, 0);
  q("#zone-selected-positions").textContent = fmt(selected.position_count, 0);
  q("#zone-selected-counter").textContent = `${chosen + 1} / ${front.length}`;
  q("#select-zone-previous").disabled = chosen <= 0;
  q("#select-zone-next").disabled = chosen >= front.length - 1;
  q("#select-zone-min-zones").disabled = false;
  q("#select-zone-min-mass").disabled = false;
  if (front.length <= 21) tradeoffZoomed = false;
  const windowSize = Math.min(21, front.length);
  const visibleStart = tradeoffZoomed ? Math.max(0, Math.min(chosen - 10, front.length - windowSize)) : 0;
  const visibleEnd = tradeoffZoomed ? visibleStart + windowSize : front.length;
  const visibleEntries = front.slice(visibleStart, visibleEnd).map((point, offset) => ({point, index: visibleStart + offset}));
  const visiblePoints = visibleEntries.map(entry => entry.point);
  q("#zone-toggle-zoom").disabled = front.length <= 21;
  q("#zone-toggle-zoom").setAttribute("aria-pressed", String(tradeoffZoomed));
  q("#zone-toggle-zoom").textContent = tradeoffZoomed ? "Весь график" : "Увеличить выбранный участок";
  q("#zone-visible-range").textContent = `Варианты ${visibleStart + 1}–${visibleEnd} из ${front.length}`;
  const counts = visiblePoints.map(p => p.zone_count), masses = visiblePoints.map(p => p.additional_mass_kg);
  const n0 = Math.min(...counts), n1 = Math.max(...counts), m0 = Math.min(...masses), m1 = Math.max(...masses);
  const chartWidth = 900, plotStart = 92, plotEnd = 870, plotTop = 42, plotBottom = 260;
  const x = n => n0 === n1 ? (plotStart + plotEnd) / 2 : plotStart + (plotEnd - plotStart) * (n - n0) / (n1 - n0);
  const y = m => m0 === m1 ? (plotTop + plotBottom) / 2 : plotBottom - (plotBottom - plotTop) * (m - m0) / (m1 - m0);
  const tickValues = (min, max) => min === max ? [min] : Array.from({length: 5}, (_, i) => min + (max - min) * i / 4);
  const xTicks = tickValues(n0, n1), yTicks = tickValues(m0, m1);
  const labels = front.map((p, i) => `Вариант ${i + 1}: ${fmt(p.zone_count, 0)} зон, ${fmt(p.additional_mass_kg)} кг` +
    `${i === chosen ? ", выбран" : ""}${hasKnee && i === knee.index ? ", геометрический перегиб" : ""}` +
    `${preferenceIndexes.includes(i) ? `, подсказка ${preferenceIndexes.indexOf(i) + 1} по инженерным примерам` : ""}`);
  const markerEntries = [...visibleEntries].sort((a, b) => {
    const priority = entry => entry.index === chosen ? 3 : preferenceIndexes.includes(entry.index) ? 2 : hasKnee && entry.index === knee.index ? 1 : 0;
    return priority(a) - priority(b) || a.index - b.index;
  });
  q("#zone-tradeoff-chart").innerHTML = `<svg viewBox="0 0 ${chartWidth} 315" data-visible-start="${visibleStart}" data-visible-end="${visibleEnd - 1}" role="group" aria-label="График массы и числа зон найденных разбиений">
    <text x="${plotStart}" y="23">Масса добавки, кг</text>
    ${yTicks.map(value => `<path class="tradeoff-grid" d="M${plotStart} ${y(value)}H${plotEnd}"/><text x="82" y="${y(value) + 4}" text-anchor="end">${fmt(value, 0)}</text>`).join("")}
    ${xTicks.map(value => `<path class="tradeoff-grid" d="M${x(value)} ${plotTop}V${plotBottom}"/><text x="${x(value)}" y="280" text-anchor="middle">${fmt(value, 0)}</text>`).join("")}
    <path class="tradeoff-axis" d="M${plotStart} ${plotTop}V${plotBottom}H${plotEnd}"/>
    <text x="${(plotStart + plotEnd) / 2}" y="307" text-anchor="middle">Количество параметрических зон · вся плита</text>
    ${visiblePoints.length > 1 ? `<polyline class="tradeoff-line" points="${visiblePoints.map(p => `${x(p.zone_count)},${y(p.additional_mass_kg)}`).join(" ")}"/>` : ""}
    ${markersCoincide && knee.index >= visibleStart && knee.index < visibleEnd ? `<circle class="tradeoff-knee-halo" cx="${x(front[knee.index].zone_count)}" cy="${y(front[knee.index].additional_mass_kg)}" r="11"/>` : ""}
    ${markerEntries.map(({point: p, index: i}) => `<circle cx="${x(p.zone_count)}" cy="${y(p.additional_mass_kg)}" r="${i === chosen ? 8 : preferenceIndexes.includes(i) ? 7 : 4.5}"
      class="tradeoff-point ${i === chosen ? "selected" : ""} ${hasKnee && i === knee.index ? "knee" : ""} ${preferenceIndexes.includes(i) ? `preference-${preferenceIndexes.indexOf(i) + 1}` : ""}"
      data-candidate="${i}" role="button" tabindex="0" aria-pressed="${i === chosen}" aria-label="${esc(labels[i])}"><title>${esc(labels[i])}</title></circle>`).join("")}
    </svg>`;
}
function renderEngineeringPreference(front) {
  const preference = validEngineeringPreference(front);
  if (!preference) return;
  const top = [...new Set(preference.top_indexes)].slice(0, 3);
  const model = String(preference.model_id || "не указан");
  const training = Array.isArray(preference.training_case_ids) ? preference.training_case_ids.map(String) : [];
  const validation = preference.validation || {};
  q("#engineering-preference").hidden = false;
  q("#engineering-preference-note").textContent =
    `${top.length} ${top.length === 1 ? "подсказка" : top.length < 5 ? "подсказки" : "подсказок"} для сравнения. ` +
    `Окончательный вариант выбирает конструктор. Модель использует массу и число физических стержней; ` +
    `число зон в инженерских примерах не размечено.`;
  q("#engineering-preference-top").innerHTML = top.map((index, rank) =>
    `<button type="button" data-preference-index="${index}" aria-pressed="${index === Number(q("#candidate").value)}">` +
    `${rank + 1}. Вариант ${index + 1} · ${fmt(front[index].zone_count, 0)} зон · ${fmt(front[index].additional_mass_kg)} кг</button>`).join("");
  q("#engineering-preference-top").setAttribute("aria-label",
    `${top.length} ${top.length === 1 ? "вариант" : "варианта"} по инженерным примерам`);
  const independent = Number.isInteger(validation.independent_cases) ? validation.independent_cases : null;
  const regret = Number.isFinite(validation.mean_regret) ? fmt(validation.mean_regret, 3) : "—";
  const kneeRegret = Number.isFinite(validation.knee_regret) ? fmt(validation.knee_regret, 3) : "—";
  q("#engineering-preference-validation").textContent =
    `Модель ${model}; обучающих выдач: ${training.length}. Проверено на ${fmt(independent, 0)} отложенных инженерных выдачах. ` +
    `Средняя нормированная ошибка выбора: ${regret}; для геометрического перегиба: ${kneeRegret}. ` +
    `Выбор остаётся исследовательским.`;
}
function chooseTradeoffPoint(index) {
  if (!Number.isInteger(index) || index < 0 || index >= (result?.front.length || 0) || layoutVariants.length) return;
  q("#candidate").value = String(index);
  renderPoint();
}
q("#zone-tradeoff-chart").addEventListener("click", event => {
  const point = event.target.closest("[data-candidate]");
  if (point) chooseTradeoffPoint(Number(point.dataset.candidate));
});
q("#zone-tradeoff-chart").addEventListener("keydown", event => {
  const point = event.target.closest("[data-candidate]");
  if (point && (event.key === "Enter" || event.key === " ")) {
    event.preventDefault(); chooseTradeoffPoint(Number(point.dataset.candidate));
  }
});
q("#select-zone-knee").addEventListener("click", () => chooseTradeoffPoint(result?.zone_tradeoff?.knee?.index));
q("#select-engineering-preference").addEventListener("click", () => {
  const preference = validEngineeringPreference(result?.front || []);
  chooseTradeoffPoint(preference ? [...new Set(preference.top_indexes)][0] : null);
});
q("#select-zone-previous").addEventListener("click", () => chooseTradeoffPoint(Number(q("#candidate").value) - 1));
q("#select-zone-next").addEventListener("click", () => chooseTradeoffPoint(Number(q("#candidate").value) + 1));
q("#select-zone-min-zones").addEventListener("click", () => {
  const front = result?.front || [];
  if (front.length) chooseTradeoffPoint(front.reduce((best, point, index) =>
    point.zone_count < front[best].zone_count ||
    (point.zone_count === front[best].zone_count && point.additional_mass_kg < front[best].additional_mass_kg) ? index : best, 0));
});
q("#select-zone-min-mass").addEventListener("click", () => {
  const front = result?.front || [];
  if (front.length) chooseTradeoffPoint(front.reduce((best, point, index) =>
    point.additional_mass_kg < front[best].additional_mass_kg ||
    (point.additional_mass_kg === front[best].additional_mass_kg && point.zone_count < front[best].zone_count) ? index : best, 0));
});
q("#zone-toggle-zoom").addEventListener("click", () => {
  if ((result?.front.length || 0) <= 21 || layoutVariants.length) return;
  tradeoffZoomed = !tradeoffZoomed;
  renderZoneTradeoff();
});
q("#engineering-preference-top").addEventListener("click", event => {
  const button = event.target.closest("[data-preference-index]");
  if (button) chooseTradeoffPoint(Number(button.dataset.preferenceIndex));
});
function updateSearchMode() {
  const merge = q("#search-mode").value === "zone-merge";
  q("#pool-budget").hidden = merge;
  q('[name="maximum_candidates"]').disabled = merge;
  q('[name="maximum_positions"]').disabled = merge;
  q("#search-mode-note").textContent = merge
    ? "Отдельный эксперимент: строятся несколько разбиений, пересобираются предложенные зоны и проверяются ограниченные объединения близких зон вдоль стержней. Сохраняются варианты разной массы и числа зон. Каждая добавка может заменяться не более слабой. Исходный спрос сохраняется. Лимит позиций здесь не поддержан и отключён."
    : "Прежний поиск в конечном пуле прямоугольников; цель — масса и позиции спецификации. Лимит позиций доступен только здесь.";
}
q("#search-mode").addEventListener("change", updateSearchMode);
if (window.location.pathname === "/partitions") {
  document.title = "QMonitoring · Разбиения и выбор варианта";
  q('.start-eyebrow').textContent = '1 · Исследовательский режим';
  q('.engineering-start h1').textContent = 'Разбиения и выбор варианта';
  q('.start-description').textContent = 'Отдельный эксперимент по числу зон и массе добавочной арматуры. ' +
    'После расчёта выберите точку на графике и проверьте её четыре направления и JSON.';
  q('nav a[href="/partitions"]').setAttribute('aria-current', 'page');
  q("#search-mode").value = "zone-merge";
  q('[name="cutting_profile"]').value = "plate-11700";
  q('[name="maximum_zones"]').value = "128";
  q('[name="solver_time_limit_s"]').value = "20";
  updateSearchMode();
} else {
  q('nav a[href="/composite"]').setAttribute('aria-current', 'page');
}
updateSearchMode();
function renderPoint() {
  const point = selectedPoint();
  renderZoneTradeoff();
  q("#full-result-warning").textContent = result.warning;
  if (isTrimmedResult()) {
    const trim = trimDisplayChecks();
    q("#result-summary").textContent = `Исходный спрос сохранён; после обработки ${fmt(trim.geometric_presence.uncovered_cell_count, 0)} КЭ без полного наличия стали, ` +
      `${fmt(trim.coverage_with_control_40d.uncovered_cell_count, 0)} КЭ без контрольных 40d. Подробности — в гейтах.`;
  }
  const graphic = isTrimmedResult() ? graphicDownload() : null;
  q("#download-selected").disabled = !point || !graphic;
  const sourceReady = hasSelectedSourceGraphics();
  q("#download-source").disabled = !sourceReady;
  q("#handoff-note").textContent = !sourceReady
    ? "Для выбранного варианта пакет зон не подготовлен. Не подменяйте его JSON другого варианта."
    : graphic
      ? "Скачайте JSON и откройте в Revit через SourceWorkflow. Созданы зоны и аннотации на плане или 4 отдельных вида. Физическая раскладка требует проверки фактических XY, глубин и типов; это не инженерное разрешение."
      : "Скачайте JSON зон для SourceWorkflow. Для этого результата совместимый JSON физической раскладки Rebar не сформирован.";
  const physical = isPhysicalResult();
  q("#mvp-scope").hidden = !result.mvp_checks;
  q("#mvp-scope").textContent = result.mvp_checks ?
    "Плоский MVP: отверстия, перепады и cover вне расчёта; фактический Revit не проверен. Это не разрешение монтажа." : "";
  q("#metrics-scope").textContent = physical
    ? "Метрики и общая ведомость выше/ниже — физическая партия после обработки. На исходной схеме показаны другие, исходные параметры зон; их количество стержней не подменяет итоговое."
    : "Метрики относятся к выбранному параметрическому кандидату. Отдельная физическая нормализация не выполнена; исходные зоны и их расчётные оси доступны раздельно.";
  q("#schedule-title").textContent = physical ? "Общая ведомость физических стержней после обработки" : "Расчётная ведомость стержней параметрического кандидата";
  q("#metrics").innerHTML = point ? [[point.additional_mass_kg, "Масса добавки", "кг", "Установленная партия", 2],
    [point.physical_bar_count, "Физические стержни", "шт.", "Количество отдельных стержней", 0],
    [point.zone_count, "Параметрические зоны", "зон", "Количество участков раскладки", 0],
    [point.position_count, "Позиции спецификации", "поз.", "Типоразмеры стержней", 0]]
    .map(([n, label, unit, note, digits]) => `<div><span>${label}</span><strong>${fmt(n, digits)} <small>${unit}</small></strong><p>${note}</p></div>`).join("") : "";
  renderComparison(point);
  q("#schedule").innerHTML = point ? point.bar_schedule.map((p) => `<tr><td>${esc(p.mark)}</td><td>${esc(p.steel_class || "не задан")}</td>
    <td>${fmt(p.diameter_mm)}</td><td>${fmt(p.length_mm, 3)}</td><td>${fmt(p.physical_bar_count, 0)}</td><td>${fmt(p.total_mass_kg)}</td></tr>`).join("") : "";
  q("#stock-status").textContent = point ? `Безотходный раскрой 11700: ${statuses[point.stock_cutting.status]}. ` +
    "Модель: смешанные прямые отрезки, нулевой пропил. " + (point.mass_increase_pct !== undefined
      ? `Длины подобраны заново: +${fmt(point.mass_increase_pct)}% массы к исходному кандидату; количество стержней прежнее.`
      : "Только проверка заданных длин.") : "";
  const attempts = result.length_balance_attempts || [];
  q("#balance-status").textContent = attempts.length
    ? `Подбор длин проверен для ${attempts.length} исходных вариантов (центрального и самого лёгкого, если различаются). ` +
      attempts.map((attempt) => `Вариант ${attempt.original_candidate_index + 1}: ` +
        (attempt.status === "balanced" ? "подобран и повторно проверен" : attempt.geometry_error ||
          explanations[attempt.telemetry?.reason] || attempt.telemetry?.reason || "Причина не передана")).join(". ") +
      ". Это не доказательство результата для всех возможных разбиений плиты."
    : "";
  q("#stock-patterns").innerHTML = point ? (point.stock_cutting.groups || []).map((group) => `<details><summary>
    ${esc(group.steel_class || "Класс не задан")} Ø${fmt(group.diameter_mm)} — ${esc(statuses[group.status])}</summary>
    <p>${esc(explanations[group.reason] || group.reason)}</p>${(group.patterns || []).map((pattern) => `<p>${fmt(pattern.stock_bar_count, 0)} прутков: ` +
      pattern.cuts.map((cut) => `${fmt(cut.pieces_per_stock_bar, 0)} × ${fmt(cut.length_mm, 3)} мм (${esc(cut.mark)})`).join(" + ") +
      " = 11700 мм, остаток 0.</p>").join("")}</details>`).join("") : "";
  const blockers = result.blocking_check_ids.filter((id) => id !== "stock-cutting-zero-waste");
  if (point && point.stock_cutting.status !== "pass") blockers.push("stock-cutting-zero-waste");
  q("#blockers").innerHTML = blockers.map((id) => `<li>${esc(explanations[id] || id)}</li>`).join("");
  renderChecks(point, blockers);
  renderDirection();
}
q("#direction-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (button) { activeDirection = Number(button.dataset.index); renderDirection(); }
});
q("#candidate").addEventListener("change", () => {
  if (layoutVariants.length) result = layoutVariants[Number(q("#candidate").value)].report;
  renderPoint();
});
q("#source-envelopes").addEventListener("change", renderDirection);
q("#drawing-views").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-view]");
  if (!button || !["source", "physical", "combined"].includes(button.dataset.view)) return;
  drawingView = button.dataset.view;
  renderDirection();
});
function renderDrawingLegend() {
  if (drawingView === "source") {
    q("#drawing-legend").textContent = "Цвета исходного DXF — потребность; Z1, Z2… — исходные прямоугольники demand_bbox. " +
      (q("#source-envelopes").checked ? "Пунктир — огибающие исходных осей после 40d/раскроя, не тела стали и не AreaBoundary. " : "") +
      "Наведите на КЭ или зону. Параметры компонентов — в таблице ниже.";
    return;
  }
  const layer = q("#drawing").dataset.layer || "layout";
  q("#drawing-legend").textContent = layer === "layout"
    ? "Синие линии — оси дополнительных стержней. Светлая подложка — исходная сетка КЭ. " +
      (isTrimmedResult() ? "Показаны новые физические концы после обрезки; дополнительных масок нет. Чёрным — " +
          (result.mvp_domain ? "условный внешний контур DXF." : "внешний контур host.")
        : "Границы линий не обрезаны.")
    : layer === "demand" ? "Цвета исходного DXF — требуемые уровни армирования. Раскладка скрыта только на схеме; нарушения и проверки не меняются."
    : "Цветные поля — потребность КЭ; линии — оси добавок. Наведите на элемент для параметров.";
  if (drawingView === "combined") q("#drawing-legend").textContent +=
    " Охристые прямоугольники Z1, Z2… — исходные зоны. Размеры, диаметр и шаг — при наведении и в таблице ниже.";
}
q("#drawing-layers").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-layer]");
  if (!button) return;
  q("#drawing").dataset.layer = button.dataset.layer;
  q("#drawing-layers").querySelectorAll("button").forEach((item) => item.setAttribute("aria-pressed", item === button));
  renderDrawingLegend();
});
q("#zoom-in").addEventListener("click", () => setZoom(zoom + .5));
q("#zoom-out").addEventListener("click", () => setZoom(zoom - .5));
q("#zoom-reset").addEventListener("click", () => setZoom(1));
function readableCalculationError(error) {
  const reason = String(error.message || "");
  if (error.emptyResponse) return `Сервер прервал расчёт и не вернул результат${error.httpStatus ? ` (HTTP ${error.httpStatus})` : ""}. Повторите запуск; если ошибка повторится, передайте разработчику название плиты и время запуска.`;
  if (error.httpStatus === 409 && error.activeJobId) return "Другой расчёт уже выполняется. Откройте его по ссылке ниже, чтобы посмотреть прогресс и результат.";
  if (error.httpStatus === 409) return "Другой расчёт уже выполняется. Дождитесь его завершения и повторите запуск.";
  if (error.httpStatus === 404) return "Расчёт недоступен: срок хранения мог истечь или сервер был перезапущен. Проверьте раздел «Недавние расчёты».";
  if (error.httpStatus === 503) return "Исходные файлы выбранной плиты недоступны на сервере. Выберите другую доступную плиту или сообщите об этом разработчику.";
  if (/fetch|network|связь/i.test(reason)) return "Не удалось связаться с сервером. Проверьте соединение и повторите запуск.";
  if (/шкал|legend|mapping|SHK|PNG/i.test(reason)) return "Не удалось применить шкалу армирования: " + reason;
  if (/origin_mm|фаз|поперечн|привязк.*ос|phase.source|placement.settings/i.test(reason)) return "Не удалось определить положение добавок относительно фона. Для своего проекта проверьте координаты в разделе «Привязка к фоновой сетке» и источник этих значений.";
  if (/контур|границ|native|exterior|host|polygon|200mm/i.test(reason)) return "Не удалось проверить границы плиты в выбранном режиме. Для К09 нужен отчёт Working Host той же плиты 200 мм и подтверждённое совпадение XY. Подробная причина указана ниже.";
  if (/coverage|покры|исходн.*потреб|source limits|complete.*variant/i.test(reason)) return "Не удалось подготовить полную раскладку в текущих ограничениях. Исходная потребность не уменьшена. Проверьте схему добавок и лимиты поиска; точная причина указана ниже.";
  if (/формат|schema|верси/i.test(reason)) return "Формат данных не подходит этой версии приложения. Обновите страницу и выберите файл для соответствующей команды.";
  if (error.httpStatus === 422) return "Исходные файлы или параметры не прошли проверку: " + reason;
  return "Расчёт не завершён. Техническая причина указана ниже — её можно передать разработчику вместе с названием плиты.";
}
async function readAnalysisResponse(response) {
  let payload = null;
  try {
    if (typeof response.text === "function") {
      const raw = await response.text();
      payload = raw ? JSON.parse(raw) : null;
    } else if (typeof response.json === "function") {
      payload = await response.json();
    }
  } catch { payload = null; }
  if (payload == null) {
    const failure = new Error(`Пустой или повреждённый ответ сервера; HTTP ${response.status}`);
    failure.httpStatus = response.status;
    failure.emptyResponse = true;
    throw failure;
  }
  return payload;
}
async function runAnalysis(url, options) {
  if (busy) return;
  busy = true;
  q("#error").hidden = true;
  q("#error-details").hidden = true;
  q("#running-job-link").hidden = true;
  q("#output").hidden = true;
  result = null;
  q("#run").disabled = true;
  q("#run-demo").disabled = true;
  q("#run-engineering-example").disabled = true;
  q("#run-boundary-trim").disabled = true;
  let startedAt = Date.now();
  const reopeningJob = url.startsWith("/api/analyze-composite-plate/jobs/");
  const hasStages = reopeningJob || url === "/api/analyze-composite-plate" && options?.body?.get?.("async_job") === "true";
  const workspacePath = window.location.pathname === "/partitions" ? "/partitions" : "/composite";
  if (!reopeningJob) window.history?.replaceState(null, "", workspacePath);
  q("#progress-track").hidden = !hasStages;
  q("#progress-bar").value = 0;
  q("#progress-percent").textContent = "0%";
  let currentStage = "Подготовка входных файлов";
  const showStage = (payload) => {
    if (Number.isFinite(payload.created_at) && payload.created_at > 0) startedAt = payload.created_at * 1000;
    const percent = payload.progress_percent;
    if (!Number.isInteger(percent) || percent < 0 || percent > 100 || typeof payload.progress_stage !== "string") return;
    q("#progress-bar").value = percent;
    q("#progress-percent").textContent = `${percent}%`;
    currentStage = payload.progress_stage;
    updateProgress();
  };
  const updateProgress = () => {
    const message = hasStages
      ? `${currentStage} · ${Math.max(0, Math.floor((Date.now() - startedAt) / 1000))} с. Можно вернуться через «Недавние расчёты».`
      : `Рассчитываем четыре направления · ${Math.floor((Date.now() - startedAt) / 1000)} с. Проверяем покрытие и всю партию. Не закрывайте страницу.`;
    q("#progress").textContent = message;
    if (url.startsWith("/api/engineering-examples/")) q("#example-status").textContent = message;
  };
  const progressTimer = setInterval(updateProgress, 1000);
  document.body.classList.add("is-calculating");
  q("#progress").textContent = reopeningJob ? "Открываем сохранённый расчёт…"
    : "Запускаем расчёт четырёх направлений. Оптимизация и проверка физических стержней могут занять несколько минут.";
  try {
    let response = await fetch(url, options);
    let payload = await readAnalysisResponse(response);
    if (response.status === 202 && payload.job_id) {
      const jobId = payload.job_id;
      window.history?.replaceState(null, "", `${workspacePath}?job=${encodeURIComponent(jobId)}`);
      window.refreshCompositeHistory?.();
      showStage(payload);
      do {
        await new Promise((resolve) => setTimeout(resolve, 3000));
        response = await fetch(`/api/analyze-composite-plate/jobs/${encodeURIComponent(jobId)}`, {cache: "no-store"});
        payload = await readAnalysisResponse(response);
        if (response.status === 202) showStage(payload);
      } while (response.status === 202 && payload.status === "running");
    }
    if (!response.ok) {
      const failure = new Error(typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail));
      failure.httpStatus = response.status;
      if (response.status === 409 && typeof payload.active_job_id === "string") {
        failure.activeJobId = payload.active_job_id;
        q("#open-running-job").href = `/composite?job=${encodeURIComponent(payload.active_job_id)}`;
        q("#running-job-link").hidden = false;
      }
      throw failure;
    }
    result = payload;
    if (hasStages) {
      q("#progress-bar").value = 100;
      q("#progress-percent").textContent = "100%";
    }
    if (result.schema_version !== "composite-plate-analysis/v1" || !Array.isArray(result.front) || result.directions?.length !== 4) throw new Error("Неподдержанный формат результата. Обновите страницу.");
    setLayoutVariants(payload);
    q("#demo-notice").hidden = !result.demo;
    q("#demo-notice").textContent = result.demo ? "ДЕМО, НЕ РЕАЛЬНЫЙ ПРОЕКТ. " + result.demo.description : "";
    q("#result-title").textContent = result.engineering_example?.title || result.case_id || "Раскладка всей плиты";
    q("#result-summary").textContent = result.source_demand_preserved === true
      ? "Полный исходный спрос сохранён. Схема и проверки относятся к новому расчёту." :
        "Сохранение полного исходного спроса не подтверждено — не используйте эту выдачу.";
    if (isTrimmedResult()) {
      const trim = trimDisplayChecks();
      q("#result-summary").textContent = `Исходный спрос сохранён; после обработки ${fmt(trim.geometric_presence.uncovered_cell_count, 0)} КЭ без полного наличия стали, ` +
        `${fmt(trim.coverage_with_control_40d.uncovered_cell_count, 0)} КЭ без контрольных 40d. Подробности — в гейтах.`;
    }
    q("#full-result-warning").textContent = result.warning;
    const choices = layoutVariants.length ? layoutVariants.map((variant) => ({...variant.metrics, label: variant.label})) : result.front;
    q("#candidate").innerHTML = choices.map((point, i) => `<option value="${i}">Вариант ${i + 1}${point.label ? " · " + esc(point.label) : ""}: ${fmt(point.additional_mass_kg)} кг / ` +
      `${fmt(point.zone_count, 0)} зон / ${fmt(point.position_count, 0)} позиций / ${fmt(point.physical_bar_count, 0)} стержней</option>`).join("");
    q("#candidate").value = String(layoutVariants.length ? 0 :
      Number.isInteger(result.selected_index) && result.selected_index >= 0 && result.selected_index < choices.length
        ? result.selected_index : 0);
    q("#candidate").disabled = !choices.length;
    q(".candidate-control").hidden = choices.length <= 1;
    q("#output").hidden = false;
    activeDirection = 0;
    drawingView = "source";
    tradeoffZoomed = false;
    q("#source-envelopes").checked = result.zone_boundary_policy !== "zone_footprints_clipped_to_union_of_source_kleenka_cells";
    zoom = 1;
    renderPoint();
    q("#progress").textContent = result.front.length ? "Расчёт закончен. Проверки размещения показаны отдельно." : "Полного решения не найдено. Смотрите причины по направлениям.";
    q("#output").scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    q("#error").textContent = readableCalculationError(error);
    q("#error").hidden = false;
    q("#error-technical").textContent = String(error.message || error);
    q("#error-details").hidden = false;
    q("#progress").textContent = "Расчёт остановлен. Проверьте пояснение выше и повторите запуск.";
  } finally {
    window.refreshCompositeHistory?.();
    clearInterval(progressTimer); document.body.classList.remove("is-calculating");
    busy = false; q("#run").disabled = false; q("#run-demo").disabled = false;
    q("#run-engineering-example").disabled = !(await window.engineeringExampleReady);
    q("#run-boundary-trim").disabled = !(await window.engineeringExampleReady);
    if (url.startsWith("/api/engineering-examples/")) q("#example-status").textContent = q("#progress").textContent;
  }
}
q("#composite-form").addEventListener("invalid", (event) => {
  let parent = event.target.parentElement;
  while (parent) { if (parent.tagName === "DETAILS") parent.open = true; parent = parent.parentElement; }
}, true);
q("#composite-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(event.currentTarget);
  if (!data.get("maximum_positions")) data.delete("maximum_positions");
  if (!data.get("host_reference")?.size) data.delete("host_reference");
  const settings = directions.map((d) => {
    const row = { layer: d.layer, axis: d.axis, steel_class: q("#steel-class").value.trim(), source: q("#phase-source").value.trim(), contact_side: q("#contact-side").value };
    q(`[data-direction="${d.key}"]`).querySelectorAll("[data-param]").forEach((input) => { row[input.dataset.param] = Number(input.value); });
    return row;
  });
  data.set("placement_settings", JSON.stringify({ directions: settings }));
  data.set("async_job", "true");
  await runAnalysis("/api/analyze-composite-plate", { method: "POST", body: data });
});
q("#run-demo").addEventListener("click", () => runAnalysis("/api/composite-demo", { method: "POST" }));
function readyExampleAnalysisUrl(example) {
  const base = `/api/engineering-examples/${encodeURIComponent(example.id)}/analyze`;
  return window.location.pathname === "/partitions" ? `${base}?search_mode=zone-merge` : base;
}
q("#run-engineering-example").addEventListener("click", async () => {
  const example = await window.engineeringExampleReady;
  if (example) runAnalysis(readyExampleAnalysisUrl(example), {method: "POST"});
});
q("#boundary-trim-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const example = await window.engineeringExampleReady;
  if (!example) return;
  const data = new FormData(event.currentTarget);
  if (!data.get("working_host")?.size || data.get("host_xy_confirmed") !== "true") return;
  await runAnalysis(`/api/engineering-examples/${encodeURIComponent(example.id)}/boundary-trim`, {method: "POST", body: data});
});
window.engineeringExampleReady.then((example) => { q("#run-boundary-trim").disabled = !example; });
q("#open-custom-inputs").addEventListener("click", () => { q("#custom-inputs").open = true; });
if (window.location.hash === "#custom-inputs") q("#custom-inputs").open = true;
function download(value, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
q("#download-report").addEventListener("click", () => { if (result) download(result, "composite-plate-report.json"); });
q("#download-source").addEventListener("click", () => {
  const packet = selectedSourceGraphics();
  if (packet) download(packet, "source-isofields-zones.json");
});
q("#download-selected").addEventListener("click", () => {
  const point = selectedPoint();
  if (!point) return;
  if (isTrimmedResult()) {
    const packet = graphicDownload();
    if (packet) download(packet, result.output_kind === "repaired-trimmed-physical-bars"
      ? "graphic-bar-plan-repaired.json" : result.output_kind === "pruned-trimmed-physical-bars"
      ? "graphic-bar-plan-pruned.json" : "graphic-bar-plan-draft.json");
    return;
  }
  if (result.output_kind === "normalized-physical-bars") {
    download({schema_version: "physical-layout-web-review/v1", units: "mm", placement_eligible: false,
      warning: result.warning, engineering_example: result.engineering_example,
      source_graphics: result.source_graphics, original_source_zones: result.original_source_zones,
      physical_review: result.physical_review, diagnostic_rollback_packet: result.physical_trial_packet,
      directions: result.directions.map((direction) => ({direction: direction.direction,
        physical_bars: direction.candidates[0].physical_bars}))}, "physical-layout-REVIEW.json");
    return;
  }
  download({ schema_version: "composite-plate-selection-draft/v1", units: "mm", placement_eligible: false,
    case_id: result.case_id, warning: result.warning, constraints: result.constraints, demo: result.demo ?? null,
    engineering_example: result.engineering_example ?? null,
    source_demand_preserved: result.source_demand_preserved, averaging: result.averaging,
    blocking_check_ids: [...result.blocking_check_ids.filter((id) => id !== "stock-cutting-zero-waste"),
      ...(point.stock_cutting.status !== "pass" ? ["stock-cutting-zero-waste"] : [])],
    host_envelope: result.host_envelope, host_coordinate_policy: result.host_coordinate_policy,
    front_scope: result.front_scope, maximum_cutting_overhead_pct: result.maximum_cutting_overhead_pct,
    selected_point: point, directions: result.directions.map((direction, i) => ({ direction: direction.direction,
      source: direction.source, settings: direction.settings, ...direction.candidates[point.direction_candidate_indexes[i]], svg: undefined }))
  }, "composite-plate-selection-DRAFT.json");
});
const savedJobId = new URLSearchParams(window.location.search).get("job");
if (savedJobId) {
  runAnalysis(`/api/analyze-composite-plate/jobs/${encodeURIComponent(savedJobId)}`, {cache: "no-store"});
} else if (new URLSearchParams(window.location.search).get("demo") === "1") {
  runAnalysis("/api/composite-demo", { method: "POST" });
} else if (new URLSearchParams(window.location.search).get("run") === "1") {
  window.engineeringExampleReady.then((example) => {
    if (example) runAnalysis(readyExampleAnalysisUrl(example), {method: "POST"});
  });
}
