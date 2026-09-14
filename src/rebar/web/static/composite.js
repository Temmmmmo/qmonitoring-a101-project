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
const explanations = {
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
q("#direction-inputs").innerHTML = directions.map((d) => `<fieldset><legend>${d.title}</legend>
  <label>Изополе DXF<input name="dxf_${d.key}" type="file" accept=".dxf" required></label>
  <label>Соответствующая шкала<input name="shk_${d.key}" type="file" accept=".shk" required></label></fieldset>`).join("");
q("#placement-inputs").innerHTML = directions.map((d) => `<fieldset data-direction="${d.key}"><legend>${d.title}</legend>
  <label>Начало фона, мм<input data-param="background_origin_mm" type="number" step="any" required></label>
  <label>Смещение первой добавки @300, мм<input data-param="first_300_offset_mm" type="number" step="any" required></label>
  <label>Смещение второй добавки, мм<input data-param="second_offset_mm" type="number" step="any" required></label></fieldset>`).join("");
q("#direction-tabs").innerHTML = directions.map((d, i) => `<button type="button" data-index="${i}">${d.title}</button>`).join("");

function selectedPoint() { return result?.front[Number(q("#candidate").value)]; }
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
  q("#engineer-comparison").hidden = !point || !reference;
  if (!point || !reference) return;
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
  const selected = point ? result.directions.map((direction, i) => direction.candidates[point.direction_candidate_indexes[i]]) : [];
  const covered = selected.length === 4 && selected.every((candidate) => candidate?.coverage?.uncovered_cell_count === 0);
  const hostChecks = selected.map((candidate) => candidate?.host_preflight?.checks?.planar_host_and_openings);
  const hostStatus = hostChecks.length === 4 && hostChecks.every((status) => status === "pass") ? "pass"
    : hostChecks.some((status) => status === "fail") ? "fail" : "not_checked";
  const pairs = result.same_plane_conflicts?.body_intersection_count;
  const rows = [["Покрытие исходной потребности", covered ? "pass" : point ? "fail" : "not_checked",
    covered ? "Все исходные КЭ покрыты в принятой модели." : "Полное покрытие выбранного варианта не подтверждено."],
    ["Раскрой прутка 11,7 м", point?.stock_cutting?.status || "not_checked", "Проверка всей партии, смешанный рез и нулевой пропил."],
    ["Пересечения стержней одного направления", Number.isInteger(pairs) ? (pairs ? "fail" : "pass") : "not_checked",
      Number.isInteger(pairs) ? `${fmt(pairs, 0)} пар в плоской модели. Это не проверка фактических высот и 3D.` : "Проверка физических стержней не выполнена."],
    ["Границы и проёмы плиты", hostStatus, hostStatus === "not_checked" ? "Нужна независимая проверка фактической геометрии host." : "Проверено только в переданной плоской модели host."],
    ["Создание в Revit", "not_checked", "Этот web-расчёт не изменял и не читал обратно модель."]];
  q("#check-summary").innerHTML = rows.map(([title, status, note]) => `<div class="check-card" data-status="${esc(status)}"><span>${esc(statuses[status] || status)}</span><strong>${esc(title)}</strong><p>${esc(note)}</p></div>`).join("");
  q("#blocker-title").textContent = `Незакрытые условия: ${blockers.length}`;
}
function renderDirection() {
  const point = selectedPoint();
  const direction = result.directions[activeDirection];
  const candidate = point ? direction.candidates[point.direction_candidate_indexes[activeDirection]] : null;
  q("#drawing").innerHTML = candidate?.svg ?? direction.input_svg ?? ""; // Server's escaped geometry renderer only.
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
          (direction.telemetry?.host_rejected_candidates ? `${direction.telemetry.host_rejected_candidates} кандидатов отклонены проверкой границ/проёмов/конфликтов.` : ""));
  q("#direction-tabs").querySelectorAll("button").forEach((button, i) => button.setAttribute("aria-pressed", i === activeDirection));
}
function renderPoint() {
  const point = selectedPoint();
  q("#download-selected").disabled = !point;
  q("#metrics").innerHTML = point ? [[point.additional_mass_kg, "Масса добавки", "кг", "Установленная партия", 2],
    [point.physical_bar_count, "Физические стержни", "шт.", "Количество отдельных стержней", 0],
    [point.position_count, "Позиции спецификации", "поз.", `${fmt(point.zone_count, 0)} параметрических зон`, 0]]
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
  q("#stock-patterns").innerHTML = point ? point.stock_cutting.groups.map((group) => `<details><summary>
    ${esc(group.steel_class || "Класс не задан")} Ø${fmt(group.diameter_mm)} — ${esc(statuses[group.status])}</summary>
    <p>${esc(explanations[group.reason] || group.reason)}</p>${group.patterns.map((pattern) => `<p>${fmt(pattern.stock_bar_count, 0)} прутков: ` +
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
q("#candidate").addEventListener("change", renderPoint);
q("#drawing-layers").addEventListener("click", (event) => {
  const button = event.target.closest("button[data-layer]");
  if (!button) return;
  q("#drawing").dataset.layer = button.dataset.layer;
  q("#drawing-layers").querySelectorAll("button").forEach((item) => item.setAttribute("aria-pressed", item === button));
  q("#drawing-legend").textContent = button.dataset.layer === "layout"
    ? "Синие линии — оси дополнительных стержней. Светлая подложка — исходная сетка КЭ."
    : button.dataset.layer === "demand" ? "Цвета исходного DXF — требуемые уровни армирования. Раскладка скрыта только на схеме."
    : "Цветные поля — потребность КЭ; линии — оси добавок. Наведите на элемент для параметров.";
});
q("#zoom-in").addEventListener("click", () => setZoom(zoom + .5));
q("#zoom-out").addEventListener("click", () => setZoom(zoom - .5));
q("#zoom-reset").addEventListener("click", () => setZoom(1));
async function runAnalysis(url, options) {
  if (busy) return;
  busy = true;
  q("#error").hidden = true;
  q("#output").hidden = true;
  result = null;
  q("#run").disabled = true;
  q("#run-demo").disabled = true;
  q("#run-engineering-example").disabled = true;
  const startedAt = Date.now();
  const updateProgress = () => {
    const message = `Рассчитываем четыре направления · ${Math.floor((Date.now() - startedAt) / 1000)} с. Проверяем покрытие и всю партию. Не закрывайте страницу.`;
    q("#progress").textContent = message;
    if (url.startsWith("/api/engineering-examples/")) q("#example-status").textContent = message;
  };
  const progressTimer = setInterval(updateProgress, 1000);
  document.body.classList.add("is-calculating");
  q("#progress").textContent = "Запускаем расчёт четырёх направлений. Оптимизация и проверка физических стержней могут занять несколько минут.";
  try {
    const response = await fetch(url, options);
    const payload = await response.json();
    if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail));
    result = payload;
    if (result.schema_version !== "composite-plate-analysis/v1" || !Array.isArray(result.front) || result.directions?.length !== 4) throw new Error("Неподдержанный формат результата. Обновите страницу.");
    q("#demo-notice").hidden = !result.demo;
    q("#demo-notice").textContent = result.demo ? "ДЕМО, НЕ РЕАЛЬНЫЙ ПРОЕКТ. " + result.demo.description : "";
    q("#result-title").textContent = result.engineering_example?.title || result.case_id || "Раскладка всей плиты";
    q("#result-summary").textContent = `Вариантов всей плиты: ${result.front.length} · исходная потребность сохранена ${result.source_demand_preserved === true ? "полностью" : "— не подтверждено"}. ` +
      (result.output_kind === "normalized-physical-bars" ? "Показана физическая партия после обработки. Исходный вариант выбран по массе с ограничением количества стержней." : "Выбор учитывает массу и позиции спецификации.");
    q("#full-result-warning").textContent = result.warning;
    q("#candidate").innerHTML = result.front.map((point, i) => `<option value="${i}">Вариант ${i + 1}: ${fmt(point.additional_mass_kg)} кг / ` +
      `${point.position_count} позиций / ${point.physical_bar_count} стержней</option>`).join("");
    if (result.selected_index !== null) q("#candidate").value = String(result.selected_index);
    q("#candidate").disabled = !result.front.length;
    q("#output").hidden = false;
    activeDirection = 0;
    zoom = 1;
    renderPoint();
    q("#progress").textContent = result.front.length ? "Расчёт закончен. Проверки размещения показаны отдельно." : "Полного решения не найдено. Смотрите причины по направлениям.";
    q("#output").scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    q("#error").textContent = error.message;
    q("#error").hidden = false;
    q("#progress").textContent = "Расчёт не завершён; старый результат не выдан за новый.";
  } finally {
    clearInterval(progressTimer); document.body.classList.remove("is-calculating");
    busy = false; q("#run").disabled = false; q("#run-demo").disabled = false;
    q("#run-engineering-example").disabled = !(await window.engineeringExampleReady);
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
  await runAnalysis("/api/analyze-composite-plate", { method: "POST", body: data });
});
q("#run-demo").addEventListener("click", () => runAnalysis("/api/composite-demo", { method: "POST" }));
q("#run-engineering-example").addEventListener("click", async () => {
  const example = await window.engineeringExampleReady;
  if (example) runAnalysis(`/api/engineering-examples/${encodeURIComponent(example.id)}/analyze`, {method: "POST"});
});
q("#open-custom-inputs").addEventListener("click", () => { q("#custom-inputs").open = true; });
if (window.location.hash === "#custom-inputs") q("#custom-inputs").open = true;
function download(value, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
q("#download-report").addEventListener("click", () => { if (result) download(result, "composite-plate-report.json"); });
q("#download-selected").addEventListener("click", () => {
  const point = selectedPoint();
  if (!point) return;
  if (result.output_kind === "normalized-physical-bars") {
    download({schema_version: "physical-layout-web-review/v1", units: "mm", placement_eligible: false,
      warning: result.warning, engineering_example: result.engineering_example,
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
if (new URLSearchParams(window.location.search).get("demo") === "1") {
  runAnalysis("/api/composite-demo", { method: "POST" });
} else if (new URLSearchParams(window.location.search).get("run") === "1") {
  window.engineeringExampleReady.then((example) => {
    if (example) runAnalysis(`/api/engineering-examples/${encodeURIComponent(example.id)}/analyze`, {method: "POST"});
  });
}
