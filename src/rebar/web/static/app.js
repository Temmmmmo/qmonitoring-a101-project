const form = document.querySelector("#analysis-form");
const dxfInput = document.querySelector("#dxf-input");
const shkInput = document.querySelector("#shk-input");
const dropzone = document.querySelector("#dropzone");
const demoSource = document.querySelector("#demo-source");
const uploadSource = document.querySelector("#upload-source");
const plateSource = document.querySelector("#plate-source");
const demoSelect = document.querySelector("#demo-select");
const sourceModeInputs = [...document.querySelectorAll('input[name="source_mode"]')];
const mappingSelect = document.querySelector("#mapping-select");
const plateMappingSelect = document.querySelector("#plate-mapping-select");
const plateShkInput = document.querySelector("#plate-shk-input");
const plateShkField = document.querySelector("#plate-shk-field");
const plateReferenceSelect = document.querySelector("#plate-reference-select");
const plateInputs = [...document.querySelectorAll(".plate-file-row input[type='file']")];
const cuttingProfileSelect = document.querySelector("#cutting-profile");
const algorithmList = document.querySelector("#algorithm-list");
const submitButton = document.querySelector("#submit-button");
const formError = document.querySelector("#form-error");
const loading = document.querySelector("#loading");
const results = document.querySelector("#results");
const emptyState = document.querySelector("#empty-state");
const workspaceState = document.querySelector("#workspace-state");
const workbenchTitle = document.querySelector("#workbench-title");
const appStatus = document.querySelector("#app-status");
const scopeLabel = document.querySelector("#scope-label");
const panelCode = document.querySelector("#panel-code");
const downloadSolution = document.querySelector("#download-solution");

let options = null;
let analysis = null;
let activeSolution = 0;
let activeDirection = 0;
let showDetailing = false;

const OPTIONS_SCHEMA_VERSION = 2;

const number = (value, digits = 0) => new Intl.NumberFormat("ru-RU", {
  maximumFractionDigits: digits,
}).format(value ?? 0);

const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

function setWorkspaceState(label, tone = "") {
  workspaceState.textContent = label;
  workspaceState.className = `workspace-state${tone ? ` ${tone}` : ""}`;
  appStatus.textContent = label;
}

function showError(message) {
  formError.textContent = message;
  formError.hidden = false;
}

function clearError() {
  formError.textContent = "";
  formError.hidden = true;
}

function algorithmTitle(id) {
  if (id === "mixed") return "Комбинированный вариант";
  return options?.algorithms.find((item) => item.id === id)?.title ?? id;
}

function sourceMode() {
  return sourceModeInputs.find((input) => input.checked)?.value ?? "demo";
}

function normalizedOptions(payload) {
  const incompatibleMessage = "Интерфейс и сервер имеют разные версии. Обновите страницу с очисткой кеша.";
  if (!payload || typeof payload !== "object" || payload.schema_version !== OPTIONS_SCHEMA_VERSION) {
    throw new Error(incompatibleMessage);
  }
  const listKeys = ["algorithms", "mappings", "demo_cases", "cutting_profiles", "references"];
  if (listKeys.some((key) => !Array.isArray(payload[key])) || !payload.defaults) {
    throw new Error(incompatibleMessage);
  }
  if (!payload.demo_cases.length) {
    throw new Error("На сервере не настроена ни одна встроенная задача.");
  }
  return payload;
}

function updateDemoDescription() {
  const selected = options?.demo_cases.find((item) => item.id === demoSelect.value);
  document.querySelector("#demo-description").textContent = selected?.description
    ?? "Синтетический DXF проходит через настоящий парсер.";
}

function updateSourceMode() {
  const mode = sourceMode();
  const demoMode = mode === "demo";
  const uploadMode = mode === "upload";
  const plateMode = mode === "plate";
  demoSource.hidden = !demoMode;
  uploadSource.hidden = !uploadMode;
  plateSource.hidden = !plateMode;
  demoSelect.disabled = !demoMode;
  mappingSelect.disabled = !uploadMode;
  shkInput.disabled = !uploadMode;
  dxfInput.disabled = !uploadMode;
  plateMappingSelect.disabled = !plateMode;
  updatePlateMappingMode();
  plateReferenceSelect.disabled = !plateMode;
  plateInputs.forEach((input) => { input.disabled = !plateMode; });
  scopeLabel.textContent = plateMode ? "Плита · 4 направления" : "Одно направление";
  panelCode.textContent = plateMode ? "DXF · 04" : "DXF · 01";
  if (results.hidden && loading.hidden) {
    const label = demoMode ? "Демо готово" : (plateMode ? "Ожидание комплекта" : "Ожидание DXF");
    setWorkspaceState(label, demoMode ? "ready" : "");
  }
}

function renderOptions(payload) {
  options = normalizedOptions(payload);
  mappingSelect.innerHTML = options.mappings.map((item) => (
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`
  )).join("");
  plateMappingSelect.innerHTML = options.mappings
    .map((item) => `<option value="${escapeHtml(item.id)}">${escapeHtml(
      item.id === "auto" ? "Загрузить общий .shk" : item.title,
    )}</option>`)
    .join("");
  plateMappingSelect.value = options.mappings.find((item) => item.id !== "auto")?.id
    ?? "auto";
  plateReferenceSelect.innerHTML = [
    '<option value="">Без инженерного эталона</option>',
    ...options.references.map((item) => (
      `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`
    )),
  ].join("");
  demoSelect.innerHTML = options.demo_cases.map((item) => (
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`
  )).join("");
  cuttingProfileSelect.innerHTML = options.cutting_profiles.map((item) => (
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`
  )).join("");
  algorithmList.innerHTML = options.algorithms.map((item) => `
    <label class="algorithm-option">
      <input type="checkbox" name="algorithm" value="${escapeHtml(item.id)}" ${item.default ? "checked" : ""}>
      <span><strong>${escapeHtml(item.title)}</strong><small>${escapeHtml(item.description)}</small></span>
    </label>
  `).join("");
  document.querySelector("#max-details").value = options.defaults.max_details ?? "";
  document.querySelector("#min-width").value = options.defaults.min_width_cells;
  document.querySelector("#genetic-population").value = options.defaults.genetic_population_size;
  document.querySelector("#genetic-generations").value = options.defaults.genetic_generations;
  document.querySelector("#genetic-seed").value = options.defaults.genetic_seed;
  document.querySelector("#genetic-operator-policy").value = options.defaults.genetic_operator_policy;
  document.querySelector("#genetic-ucb-exploration").value = options.defaults.genetic_ucb_exploration;
  cuttingProfileSelect.value = options.defaults.cutting_profile;
  demoSelect.value = options.defaults.demo_id;
  if (!demoSelect.value) demoSelect.selectedIndex = 0;
  const defaultMode = options.defaults.source_mode ?? "demo";
  sourceModeInputs.forEach((input) => { input.checked = input.value === defaultMode; });
  updateDemoDescription();
  updateSourceMode();
}

async function loadOptions() {
  try {
    const response = await fetch("/api/options", { cache: "no-store" });
    if (!response.ok) throw new Error("Не удалось получить настройки приложения.");
    renderOptions(await response.json());
    setWorkspaceState(sourceMode() === "demo" ? "Демо готово" : "Система готова", "ready");
  } catch (error) {
    showError(error.message);
    setWorkspaceState("Ошибка конфигурации", "bad");
  }
}

function updateDxfLabel() {
  const file = dxfInput.files[0];
  document.querySelector("#dxf-title").textContent = file ? file.name : "Выберите файл мозаики";
  document.querySelector("#dxf-meta").textContent = file
    ? `${number(file.size / 1024 / 1024, 2)} МБ · готов к обработке`
    : "DXF, не более 30 МБ";
  dropzone.classList.toggle("has-file", Boolean(file));
  if (file && sourceMode() === "upload" && results.hidden && loading.hidden) {
    setWorkspaceState("Файл выбран", "ready");
  }
}

dxfInput.addEventListener("change", updateDxfLabel);
sourceModeInputs.forEach((input) => input.addEventListener("change", () => {
  clearError();
  updateSourceMode();
}));
demoSelect.addEventListener("change", () => {
  updateDemoDescription();
  if (results.hidden && loading.hidden) setWorkspaceState("Демо готово", "ready");
});
shkInput.addEventListener("change", () => {
  document.querySelector("#shk-label").textContent = shkInput.files[0]?.name ?? "Добавить .shk";
  if (shkInput.files[0]) mappingSelect.value = "auto";
});
mappingSelect.addEventListener("change", () => {
  if (mappingSelect.value !== "auto" && shkInput.files.length) {
    shkInput.value = "";
    document.querySelector("#shk-label").textContent = "Добавить .shk";
  }
});

function updatePlateMappingMode() {
  const usesShk = plateMappingSelect.value === "auto";
  const plateMode = sourceMode() === "plate";
  plateShkField.hidden = !usesShk;
  plateShkInput.disabled = !plateMode || !usesShk;
  if (!usesShk && plateShkInput.files.length) {
    plateShkInput.value = "";
    document.querySelector("#plate-shk-label").textContent = "Выбрать .shk";
  }
}

plateMappingSelect.addEventListener("change", () => {
  clearError();
  updatePlateMappingMode();
});
plateShkInput.addEventListener("change", () => {
  document.querySelector("#plate-shk-label").textContent = (
    plateShkInput.files[0]?.name ?? "Выбрать .shk"
  );
  if (plateShkInput.files[0]) plateMappingSelect.value = "auto";
  updatePlateMappingMode();
});

function updatePlateFileLabel(input) {
  const row = input.closest(".plate-file-row");
  const file = input.files[0];
  row.querySelector("[data-file-title]").textContent = file?.name ?? "Выбрать DXF";
  row.querySelector("[data-file-meta]").textContent = file
    ? `${number(file.size / 1024 / 1024, 2)} МБ · готов`
    : "не выбран";
  row.classList.toggle("has-file", Boolean(file));
  if (sourceMode() === "plate" && plateInputs.every((item) => item.files[0])) {
    setWorkspaceState("Комплект выбран", "ready");
  }
}

plateInputs.forEach((input) => input.addEventListener("change", () => {
  clearError();
  updatePlateFileLabel(input);
}));

["dragenter", "dragover"].forEach((eventName) => dropzone.addEventListener(eventName, (event) => {
  event.preventDefault();
  dropzone.classList.add("dragging");
}));
["dragleave", "drop"].forEach((eventName) => dropzone.addEventListener(eventName, (event) => {
  event.preventDefault();
  dropzone.classList.remove("dragging");
}));
dropzone.addEventListener("drop", (event) => {
  const file = [...event.dataTransfer.files].find((item) => item.name.toLowerCase().endsWith(".dxf"));
  if (!file) {
    showError("Перетащите файл с расширением .dxf.");
    return;
  }
  const transfer = new DataTransfer();
  transfer.items.add(file);
  dxfInput.files = transfer.files;
  clearError();
  updateDxfLabel();
});

function sourceFacts(source) {
  const layer = source.direction.layer === "bottom" ? "нижнее" : "верхнее";
  const mapping = source.mapping_id || ".shk";
  const width = source.bbox[2] - source.bbox[0];
  const height = source.bbox[3] - source.bbox[1];
  const maximumZones = source.zone_count_bounds?.maximum ?? source.cell_count;
  const loweredCells = source.single_cell_preprocessing?.changed_count ?? 0;
  return [
    `<span><b>${escapeHtml(layer)} · ось ${escapeHtml(source.direction.axis)}</b></span>`,
    `<span><b>${number(source.cell_count)}</b> КЭ</span>`,
    `<span><b>${number(source.level_count)}</b> уровней</span>`,
    `<span>диапазон зон: <b>${maximumZones ? `1–${number(maximumZones)}` : "0"}</b></span>`,
    `<span>одиночных КЭ понижено: <b>${number(loweredCells)}</b></span>`,
    `<span><b>${number(width)} × ${number(height)}</b> мм</span>`,
    `<span>источник: <b>${escapeHtml(mapping)}</b></span>`,
  ].join("");
}

function directionLabel(direction) {
  const layer = direction.layer === "bottom" ? "Нижнее" : "Верхнее";
  return `${layer} · ось ${direction.axis}`;
}

function plateSourceFacts(sources) {
  const totalCells = sources.reduce((sum, source) => sum + Number(source.cell_count ?? 0), 0);
  const loweredCells = sources.reduce((sum, source) => (
    sum + Number(source.single_cell_preprocessing?.changed_count ?? 0)
  ), 0);
  return [
    `<span><b>4 направления</b></span>`,
    `<span><b>${number(totalCells)}</b> КЭ суммарно</span>`,
    `<span><b>${number(loweredCells)}</b> одиночных КЭ понижено</span>`,
    ...sources.map((source) => (
      `<span><b>${escapeHtml(directionLabel(source.direction))}</b> · `
      + `${escapeHtml(source.filename)} · зоны 1–${number(source.zone_count_bounds?.maximum)}</span>`
    )),
  ].join("");
}

function nonUsableStatus(status) {
  return ["error", "infeasible", "time_limit"].includes(status);
}

function metricCard(value, label, danger = false) {
  return `<div class="metric-card${danger ? " danger" : ""}"><strong>${value}</strong><span>${label}</span></div>`;
}

function signedNumber(value, digits = 1) {
  if (value === null || value === undefined) return "—";
  const prefix = Number(value) > 0 ? "+" : "";
  return `${prefix}${number(value, digits)}`;
}

function gateStatusLabel(status) {
  return {
    pass: "соблюдено",
    fail: "нарушено",
    warning: "требует решения",
    not_checked: "не проверено",
  }[status] ?? status;
}

function renderGateAssessment(assessment) {
  const panel = document.querySelector("#gate-panel");
  if (!assessment?.items) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const summary = assessment.summary ?? {};
  const checked = Number(summary.pass ?? 0) + Number(summary.fail ?? 0) + Number(summary.warning ?? 0);
  const reference = assessment.reference_title
    ? ` · эталон: ${assessment.reference_title}`
    : "";
  document.querySelector("#gate-summary").textContent = (
    `${number(summary.pass)} из ${number(checked)} измеримых соблюдено${reference}`
  );
  document.querySelector("#gate-list").innerHTML = assessment.items.map((item) => {
    const actual = item.actual === null ? "—" : `${number(item.actual, 2)} ${item.unit}`.trim();
    const target = item.target === null ? "—" : `${number(item.target, 2)} ${item.unit}`.trim();
    const absolute = item.absolute_deviation === null
      ? "абс. —"
      : `абс. ${signedNumber(item.absolute_deviation, 2)} ${item.unit}`.trim();
    const relative = item.relative_deviation_pct === null
      ? "отн. —"
      : `отн. ${signedNumber(item.relative_deviation_pct, 1)}%`;
    return `<article class="gate-item ${escapeHtml(item.status)}">
      <div class="gate-item-head"><strong>${escapeHtml(item.title)}</strong>
        <span>${escapeHtml(gateStatusLabel(item.status))}</span></div>
      <div class="gate-values">
        <span>факт <b>${escapeHtml(actual)}</b></span>
        <span>норма <b>${escapeHtml(target)}</b></span>
        <span>${escapeHtml(absolute)}</span><span>${escapeHtml(relative)}</span>
      </div>
      <p>${escapeHtml(item.note)}</p>
    </article>`;
  }).join("");
}

function renderTrajectory(solution) {
  const panel = document.querySelector("#trajectory-panel");
  const chart = document.querySelector("#trajectory-chart");
  const title = document.querySelector("#trajectory-title");
  const note = document.querySelector("#trajectory-note");
  const front = analysis.pareto_front;
  const frontPoints = front?.points ?? [];
  if (frontPoints.length) {
    const points = frontPoints;
    const width = 320;
    const height = 150;
    const padding = 24;
    const complexities = points.map((point) => Number(point.complexity));
    const masses = points.map((point) => Number(point.total_mass_kg));
    const minComplexity = Math.min(...complexities);
    const maxComplexity = Math.max(...complexities);
    const minMass = Math.min(...masses);
    const maxMass = Math.max(...masses);
    const x = (complexity) => padding + ((complexity - minComplexity)
      / Math.max(maxComplexity - minComplexity, 1)) * (width - 2 * padding);
    const y = (mass) => height - padding - ((mass - minMass)
      / Math.max(maxMass - minMass, 1)) * (height - 2 * padding);
    const polyline = [...points]
      .sort((first, second) => first.complexity - second.complexity)
      .map((point) => `${x(point.complexity)},${y(point.total_mass_kg)}`)
      .join(" ");
    const circles = points.map((point) => {
      const active = Number(point.solution_index) === activeSolution;
      return `<circle class="trajectory-point pareto${active ? " active" : ""}"
        data-solution-index="${Number(point.solution_index)}"
        cx="${x(point.complexity)}" cy="${y(point.total_mass_kg)}" r="${active ? 5 : 3.5}">
        <title>${number(point.complexity)} сложности · ${number(point.total_mass_kg, 1)} кг</title>
      </circle>`;
    }).join("");
    const axisLabel = front.complexity_axis === "physical_bar_count"
      ? "стержней"
      : "зон";
    const scopeTitle = analysis.kind === "plate" ? "плиты" : "направления";
    chart.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Парето-фронт ${scopeTitle}">
      <line class="trajectory-axis" x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}"></line>
      <line class="trajectory-axis" x1="${padding}" y1="${padding}" x2="${padding}" y2="${height - padding}"></line>
      <polyline class="trajectory-line" points="${polyline}"></polyline>${circles}
      <text class="trajectory-label" x="${padding}" y="${height - 7}">${number(minComplexity)} ${axisLabel}</text>
      <text class="trajectory-label" x="${width - padding}" y="${height - 7}" text-anchor="end">${number(maxComplexity)} ${axisLabel}</text>
      <text class="trajectory-label" x="${padding + 3}" y="${padding - 7}">${number(maxMass, 1)} кг</text>
      <text class="trajectory-label" x="${padding + 3}" y="${height - padding - 5}">${number(minMass, 1)} кг</text>
    </svg>`;
    chart.querySelectorAll("[data-solution-index]").forEach((point) => {
      point.addEventListener("click", () => {
        activeSolution = Number(point.dataset.solutionIndex);
        activeDirection = 0;
        renderActiveSolution();
      });
    });
    title.textContent = `Парето-фронт ${scopeTitle}`;
    const checkedCount = analysis.kind === "plate"
      ? `${number(front.combination_count)} комбинаций`
      : `${number(front.source_candidate_count)} кандидатов`;
    note.textContent = `${checkedCount} проверено; показаны только недоминируемые и прошедшие hard-валидацию варианты.`;
    panel.hidden = false;
    return;
  }
  const trajectory = solution.meta?.merge_trajectory ?? [];
  if (trajectory.length < 2) {
    panel.hidden = true;
    chart.innerHTML = "";
    return;
  }

  const width = 320;
  const height = 150;
  const padding = 24;
  const points = [...trajectory].sort((first, second) => first.zone_count - second.zone_count);
  const counts = points.map((point) => Number(point.zone_count));
  const masses = points.map((point) => Number(point.total_mass_kg));
  const minCount = Math.min(...counts);
  const maxCount = Math.max(...counts);
  const minMass = Math.min(...masses);
  const maxMass = Math.max(...masses);
  const x = (count) => padding + ((count - minCount) / Math.max(maxCount - minCount, 1)) * (width - 2 * padding);
  const y = (mass) => height - padding - ((mass - minMass) / Math.max(maxMass - minMass, 1)) * (height - 2 * padding);
  const pareto = new Set((solution.meta?.trajectory_pareto_front ?? []).map(
    (point) => `${point.zone_count}:${Number(point.total_mass_kg).toFixed(6)}`,
  ));
  const polyline = points.map((point) => `${x(point.zone_count)},${y(point.total_mass_kg)}`).join(" ");
  const circles = points.map((point) => {
    const key = `${point.zone_count}:${Number(point.total_mass_kg).toFixed(6)}`;
    const isPareto = pareto.has(key);
    return `<circle class="trajectory-point${isPareto ? " pareto" : ""}" cx="${x(point.zone_count)}" cy="${y(point.total_mass_kg)}" r="${isPareto ? 3.5 : 2}"><title>${number(point.zone_count)} зон · ${number(point.total_mass_kg, 1)} кг</title></circle>`;
  }).join("");
  chart.innerHTML = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="Траектория массы и числа зон">
    <line class="trajectory-axis" x1="${padding}" y1="${height - padding}" x2="${width - padding}" y2="${height - padding}"></line>
    <line class="trajectory-axis" x1="${padding}" y1="${padding}" x2="${padding}" y2="${height - padding}"></line>
    <polyline class="trajectory-line" points="${polyline}"></polyline>${circles}
    <text class="trajectory-label" x="${padding}" y="${height - 7}">${number(minCount)} зон</text>
    <text class="trajectory-label" x="${width - padding}" y="${height - 7}" text-anchor="end">${number(maxCount)} зон</text>
    <text class="trajectory-label" x="${padding + 3}" y="${padding - 7}">${number(maxMass, 1)} кг</text>
    <text class="trajectory-label" x="${padding + 3}" y="${height - padding - 5}">${number(minMass, 1)} кг</text>
  </svg>`;
  title.textContent = "Траектория вариантов";
  note.textContent = "Зелёным отмечены недоминируемые точки внутри текущей жадной траектории.";
  panel.hidden = false;
}

function renderDirectionTabs(plateSolution) {
  const tabs = document.querySelector("#direction-tabs");
  if (analysis.kind !== "plate") {
    tabs.hidden = true;
    tabs.innerHTML = "";
    return;
  }
  tabs.innerHTML = plateSolution.direction_solutions.map((item, index) => `
    <button class="direction-tab" type="button" role="tab" data-index="${index}"
      aria-selected="${index === activeDirection}">
      <strong>${escapeHtml(directionLabel(item.direction))}</strong>
      <small>${number(item.solution.metrics.total_mass_kg, 1)} кг · ${number(item.solution.metrics.detail_count)} зон</small>
    </button>
  `).join("");
  tabs.hidden = false;
  tabs.querySelectorAll(".direction-tab").forEach((tab) => tab.addEventListener("click", () => {
    activeDirection = Number(tab.dataset.index);
    renderActiveSolution();
  }));
}

function renderActiveSolution() {
  const selected = analysis.solutions[activeSolution];
  const plateMode = analysis.kind === "plate";
  const directionItem = plateMode ? selected.direction_solutions[activeDirection] : null;
  const layoutSolution = plateMode ? directionItem.solution : selected;
  const metrics = plateMode ? selected.metrics : layoutSolution.metrics;
  const invalid = metrics.under_reinforced_cell_count > 0 || nonUsableStatus(selected.status);

  document.querySelectorAll(".solution-tab").forEach((tab, index) => {
    tab.setAttribute("aria-selected", String(index === activeSolution));
  });
  renderDirectionTabs(selected);
  const activeTitle = plateMode
    ? `${selected.title ?? algorithmTitle(selected.algorithm)} · ${directionLabel(directionItem.direction)}`
    : algorithmTitle(layoutSolution.algorithm);
  document.querySelector("#active-algorithm").textContent = activeTitle;
  const status = document.querySelector("#active-status");
  const statusLabels = {
    optimal: "оптимально",
    feasible: "допустимо",
    infeasible: "нет решения",
    time_limit: "лимит времени",
    error: "ошибка",
  };
  status.textContent = invalid ? "требует внимания" : (statusLabels[selected.status] ?? selected.status);
  status.classList.toggle("bad", invalid);
  const revitExport = plateMode ? selected.revit_export : null;
  downloadSolution.hidden = !revitExport;
  if (revitExport) {
    const eligible = Boolean(revitExport.checks?.export_eligible);
    downloadSolution.textContent = eligible ? "Скачать JSON для Revit" : "Скачать черновик JSON";
    const blockers = revitExport.checks?.blocking_check_ids ?? [];
    downloadSolution.title = eligible
      ? "Проверки экспортного профиля пройдены"
      : `Перед применением устраните: ${blockers.join(", ")}`;
  }
  const drawing = document.querySelector("#drawing");
  drawing.innerHTML = layoutSolution.svg;
  drawing.classList.toggle("show-detailing", showDetailing);
  document.querySelector("#active-metrics").innerHTML = plateMode ? [
    metricCard(`${number(metrics.total_mass_kg, 1)} кг`, "общая масса четырёх направлений"),
    metricCard(number(metrics.zone_count), "прямоугольных зон суммарно"),
    metricCard(number(metrics.physical_bar_count), "физических стержней суммарно"),
    metricCard(number(metrics.direction_count), "направлений в комплекте"),
    metricCard(number(metrics.under_reinforced_cell_count), "недоармированных КЭ", invalid),
    metricCard(`${number(selected.runtime_ms, 1)} мс`, "сумма времени алгоритмов"),
    metricCard(number(selected.constructability?.unique_layout_signature_count), "уникальных сигнатур раскладки"),
    metricCard(number(selected.constructability?.warning_count), "предупреждений детализации"),
  ].join("") : [
    metricCard(`${number(metrics.total_mass_kg, 1)} кг`, "масса дополнительной арматуры"),
    metricCard(number(metrics.detail_count), "прямоугольных зон"),
    metricCard(number(layoutSolution.physical_bar_count), "физических стержней"),
    metricCard(number(metrics.overcovered_cell_count), "КЭ с перерасходом"),
    metricCard(number(metrics.under_reinforced_cell_count), "недоармированных КЭ", invalid),
    metricCard(`${number(layoutSolution.runtime_ms, 1)} мс`, "время алгоритма"),
  ].join("");
  renderGateAssessment(plateMode ? selected.gate_assessment : layoutSolution.gate_assessment);
  renderTrajectory(layoutSolution);

  const allDiagnostics = selected.diagnostics.length
    ? selected.diagnostics
    : ["Нарушения не обнаружены в текущей MVP-модели."];
  const diagnostics = allDiagnostics.slice(0, 40);
  if (allDiagnostics.length > diagnostics.length) {
    diagnostics.push(`… ещё ${number(allDiagnostics.length - diagnostics.length)} сообщений`);
  }
  document.querySelector("#diagnostics-list").innerHTML = diagnostics
    .map((message) => `<li>${escapeHtml(message)}</li>`).join("");
  document.querySelector("#zones-body").innerHTML = layoutSolution.zone_schedule.map((zone) => `
    <tr>
      <td title="${escapeHtml(zone.zone_id)}">${escapeHtml(zone.mark)}</td>
      <td>${escapeHtml(zone.callout)}</td><td>${number(zone.level_index)}</td>
      <td>${number(zone.width_mm)} мм</td><td>${number(zone.anchored_length_mm)} мм</td>
      <td>${number(zone.installed_length_mm)} мм</td>
      <td>${number(zone.mass_kg, 1)} кг</td>
    </tr>
  `).join("");
}

document.querySelector("#detailing-toggle").addEventListener("click", (event) => {
  showDetailing = !showDetailing;
  event.currentTarget.setAttribute("aria-pressed", String(showDetailing));
  event.currentTarget.textContent = `Детализация: ${showDetailing ? "вкл" : "выкл"}`;
  document.querySelector("#drawing").classList.toggle("show-detailing", showDetailing);
});

function renderResults(payload) {
  analysis = payload;
  activeSolution = 0;
  activeDirection = 0;
  const plateMode = payload.kind === "plate";
  workbenchTitle.textContent = "Результат расчёта";
  document.querySelector("#result-kicker").textContent = plateMode ? "Активная плита" : "Активный файл";
  document.querySelector("#result-filename").textContent = plateMode
    ? (payload.plate.case_id || "Комплект четырёх направлений")
    : payload.source.filename;
  document.querySelector("#source-facts").innerHTML = plateMode
    ? plateSourceFacts(payload.sources)
    : sourceFacts(payload.source);
  document.querySelector("#solution-tabs").innerHTML = payload.solutions.map((solution, index) => `
    <button class="solution-tab" type="button" role="tab" data-index="${index}" aria-selected="${index === 0}">
      <strong>${escapeHtml(solution.title ?? algorithmTitle(solution.algorithm))}</strong>
      <small>${number(solution.metrics.total_mass_kg, 1)} кг · ${number(plateMode ? solution.metrics.zone_count : solution.metrics.detail_count)} зон</small>
    </button>
  `).join("");
  document.querySelectorAll(".solution-tab").forEach((tab) => tab.addEventListener("click", () => {
    activeSolution = Number(tab.dataset.index);
    activeDirection = 0;
    renderActiveSolution();
  }));
  renderActiveSolution();
  results.hidden = false;
  setWorkspaceState("Расчёт завершён", "ready");
  results.scrollIntoView({ behavior: "smooth", block: "start" });
}

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearError();
  const mode = sourceMode();
  const demoMode = mode === "demo";
  const plateMode = mode === "plate";
  if (mode === "upload" && !dxfInput.files[0]) {
    showError("Сначала выберите DXF.");
    return;
  }
  if (plateMode && !plateInputs.every((input) => input.files[0])) {
    showError("Для плиты выберите все четыре DXF: низ/верх вдоль X и Y.");
    return;
  }
  if (plateMode && !plateMappingSelect.value) {
    showError("Выберите встроенную таблицу или общий .shk.");
    return;
  }
  if (plateMode && plateMappingSelect.value === "auto" && !plateShkInput.files[0]) {
    showError("Выберите общий файл .shk для четырёх направлений.");
    return;
  }
  const selectedAlgorithms = [...form.querySelectorAll('input[name="algorithm"]:checked')]
    .map((input) => input.value);
  if (!selectedAlgorithms.length) {
    showError("Выберите хотя бы один алгоритм.");
    return;
  }

  const body = new FormData();
  if (demoMode) {
    body.append("demo_id", demoSelect.value);
  } else if (plateMode) {
    plateInputs.forEach((input) => body.append(input.name, input.files[0]));
    if (plateShkInput.files[0]) body.append("shk", plateShkInput.files[0]);
    body.append("mapping_id", plateMappingSelect.value);
    body.append("case_id", document.querySelector("#plate-case-id").value);
    body.append("reference_id", plateReferenceSelect.value);
  } else {
    body.append("dxf", dxfInput.files[0]);
    if (shkInput.files[0]) body.append("shk", shkInput.files[0]);
    body.append("mapping_id", mappingSelect.value);
  }
  body.append("algorithms", selectedAlgorithms.join(","));
  const maxDetails = document.querySelector("#max-details").value;
  if (maxDetails) body.append("max_details", maxDetails);
  body.append("min_width_cells", document.querySelector("#min-width").value);
  body.append("detail_penalty_kg", document.querySelector("#detail-penalty").value);
  body.append("cutting_profile", cuttingProfileSelect.value);
  body.append("genetic_population_size", document.querySelector("#genetic-population").value);
  body.append("genetic_generations", document.querySelector("#genetic-generations").value);
  body.append("genetic_seed", document.querySelector("#genetic-seed").value);
  body.append("genetic_operator_policy", document.querySelector("#genetic-operator-policy").value);
  body.append("genetic_ucb_exploration", document.querySelector("#genetic-ucb-exploration").value);

  submitButton.disabled = true;
  results.hidden = true;
  emptyState.hidden = true;
  loading.hidden = false;
  workbenchTitle.textContent = "Выполнение расчёта";
  setWorkspaceState("Расчёт выполняется", "busy");
  try {
    const endpoint = demoMode
      ? "/api/demo"
      : (plateMode ? "/api/analyze-plate" : "/api/analyze");
    const response = await fetch(endpoint, { method: "POST", body });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.detail || "Не удалось выполнить расчёт.");
    renderResults(payload);
  } catch (error) {
    showError(error.message);
    emptyState.hidden = false;
    workbenchTitle.textContent = "Просмотр раскладки";
    setWorkspaceState("Ошибка расчёта", "bad");
    formError.scrollIntoView({ behavior: "smooth", block: "center" });
  } finally {
    loading.hidden = true;
    submitButton.disabled = false;
  }
});

document.querySelector("#new-analysis").addEventListener("click", () => {
  results.hidden = true;
  emptyState.hidden = false;
  workbenchTitle.textContent = "Просмотр раскладки";
  setWorkspaceState("Параметры готовы", "ready");
  form.scrollIntoView({ behavior: "smooth", block: "start" });
});

downloadSolution.addEventListener("click", () => {
  const selected = analysis?.kind === "plate" ? analysis.solutions[activeSolution] : null;
  if (!selected?.revit_export) return;
  const blob = new Blob(
    [JSON.stringify(selected.revit_export, null, 2)],
    { type: "application/json;charset=utf-8" },
  );
  const link = document.createElement("a");
  const caseId = analysis.plate.case_id || "plate-solution";
  link.href = URL.createObjectURL(blob);
  link.download = `${caseId.replaceAll(/[^a-zA-Zа-яА-ЯёЁ0-9_-]+/g, "-")}.revit.json`;
  link.click();
  URL.revokeObjectURL(link.href);
});

loadOptions();
