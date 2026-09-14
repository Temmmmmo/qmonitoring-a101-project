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
const statuses = { pass: "Проверка пройдена в указанной модели", fail: "Не выполнено", not_checked: "Не проверено" };
const explanations = {
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
q("#direction-inputs").innerHTML = directions.map((d) => `<fieldset><legend>${d.title}</legend>
  <label>Изополе DXF<input name="dxf_${d.key}" type="file" accept=".dxf" required></label>
  <label>Соответствующая шкала<input name="shk_${d.key}" type="file" accept=".shk" required></label></fieldset>`).join("");
q("#placement-inputs").innerHTML = directions.map((d) => `<fieldset data-direction="${d.key}"><legend>${d.title}</legend>
  <label>Начало фона, мм<input data-param="background_origin_mm" type="number" step="any" required></label>
  <label>Смещение первой добавки @300, мм<input data-param="first_300_offset_mm" type="number" step="any" required></label>
  <label>Смещение второй добавки, мм<input data-param="second_offset_mm" type="number" step="any" required></label></fieldset>`).join("");
q("#direction-tabs").innerHTML = directions.map((d, i) => `<button type="button" data-index="${i}">${d.title}</button>`).join("");

function selectedPoint() { return result?.front[Number(q("#candidate").value)]; }
function renderDirection() {
  const point = selectedPoint();
  const direction = result.directions[activeDirection];
  const candidate = point ? direction.candidates[point.direction_candidate_indexes[activeDirection]] : null;
  q("#drawing").innerHTML = candidate?.svg ?? direction.input_svg ?? ""; // Server's escaped geometry renderer only.
  q("#installation-notes").innerHTML = [...new Set((candidate?.installation_notes || []).map((item) => item.note))]
    .map((note) => `<li>${esc(note)} Высоты осей ещё не назначены; фактическое касание не подтверждено.</li>`).join("");
  q("#direction-status").textContent = candidate
    ? `${directions[activeDirection].title}: ${fmt(candidate.coverage.uncovered_cell_count, 0)} непокрытых КЭ из исходного спроса; ` +
      (candidate.host_preflight ? `границы: ${statuses[candidate.host_preflight.checks.planar_host_and_openings]}` : "host не проверен")
    : `${directions[activeDirection].title}: ${direction.candidates.length} кандидатов. ` +
      (direction.telemetry.host_demand_feasibility?.requires_engineering_decision
        ? `Несовместимы с текущей анкеровкой у края: ${direction.telemetry.host_demand_feasibility.cell_count} КЭ, выделены красным. Спрос не удалён.`
        : "Полного решения в заданном конечном поиске не найдено; показан исходный спрос, лимиты не ослаблены. " +
          (direction.telemetry.host_rejected_candidates ? `${direction.telemetry.host_rejected_candidates} кандидатов отклонены проверкой границ/проёмов/конфликтов.` : ""));
  q("#direction-tabs").querySelectorAll("button").forEach((button, i) => button.setAttribute("aria-pressed", i === activeDirection));
}
function renderPoint() {
  const point = selectedPoint();
  q("#download-selected").disabled = !point;
  q("#metrics").innerHTML = point ? [[point.additional_mass_kg, "кг добавки"], [point.position_count, "позиций"],
    [point.zone_count, "зон"], [point.physical_bar_count, "физических стержней"]]
    .map(([n, label]) => `<div><strong>${fmt(n)}</strong>${label}</div>`).join("") : "";
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
          explanations[attempt.telemetry.reason] || attempt.telemetry.reason)).join(". ") +
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
  renderDirection();
}
q("#direction-tabs").addEventListener("click", (event) => {
  const button = event.target.closest("button");
  if (button) { activeDirection = Number(button.dataset.index); renderDirection(); }
});
q("#candidate").addEventListener("change", renderPoint);
async function runAnalysis(url, options) {
  if (busy) return;
  busy = true;
  q("#error").hidden = true;
  q("#output").hidden = true;
  result = null;
  q("#run").disabled = true;
  q("#run-demo").disabled = true;
  q("#progress").textContent = "Расчёт четырёх направлений и проверка всей партии. Генерация геометрии занимает время сверх бюджета MILP.";
  try {
    const response = await fetch(url, options);
    const payload = await response.json();
    if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : JSON.stringify(payload.detail));
    result = payload;
    q("#demo-notice").hidden = !result.demo;
    q("#demo-notice").textContent = result.demo ? "ДЕМО, НЕ РЕАЛЬНЫЙ ПРОЕКТ. " + result.demo.description : "";
    q("#result-summary").textContent = `${result.front.length} общеплитных вариантов. ${result.warning}`;
    q("#candidate").innerHTML = result.front.map((point, i) => `<option value="${i}">Вариант ${i + 1}: ${fmt(point.additional_mass_kg)} кг / ` +
      `${point.position_count} позиций / ${point.physical_bar_count} стержней</option>`).join("");
    if (result.selected_index !== null) q("#candidate").value = String(result.selected_index);
    q("#candidate").disabled = !result.front.length;
    q("#output").hidden = false;
    activeDirection = 0;
    renderPoint();
    q("#progress").textContent = result.front.length ? "Расчёт закончен. Проверки размещения показаны отдельно." : "Полного решения не найдено. Смотрите причины по направлениям.";
    q("#output").scrollIntoView({ behavior: "smooth" });
  } catch (error) {
    q("#error").textContent = error.message;
    q("#error").hidden = false;
    q("#progress").textContent = "Расчёт не завершён; старый результат не выдан за новый.";
  } finally { busy = false; q("#run").disabled = false; q("#run-demo").disabled = false; }
}
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
function download(value, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
  const link = document.createElement("a"); link.href = url; link.download = filename; link.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
q("#download-report").addEventListener("click", () => { if (result) download(result, "composite-plate-report.json"); });
q("#download-selected").addEventListener("click", () => {
  const point = selectedPoint();
  if (!point) return;
  download({ schema_version: "composite-plate-selection-draft/v1", units: "mm", placement_eligible: false,
    case_id: result.case_id, warning: result.warning, constraints: result.constraints, demo: result.demo ?? null,
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
}
