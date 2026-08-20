const form = document.querySelector("#analysis-form");
const dxfInput = document.querySelector("#dxf-input");
const shkInput = document.querySelector("#shk-input");
const dropzone = document.querySelector("#dropzone");
const demoSource = document.querySelector("#demo-source");
const uploadSource = document.querySelector("#upload-source");
const demoSelect = document.querySelector("#demo-select");
const sourceModeInputs = [...document.querySelectorAll('input[name="source_mode"]')];
const mappingSelect = document.querySelector("#mapping-select");
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

let options = null;
let analysis = null;
let activeSolution = 0;
let showDetailing = false;

const OPTIONS_SCHEMA_VERSION = 1;

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
  const listKeys = ["algorithms", "mappings", "demo_cases", "cutting_profiles"];
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
  const demoMode = sourceMode() === "demo";
  demoSource.hidden = !demoMode;
  uploadSource.hidden = demoMode;
  demoSelect.disabled = !demoMode;
  mappingSelect.disabled = demoMode;
  shkInput.disabled = demoMode;
  dxfInput.disabled = demoMode;
  if (results.hidden && loading.hidden) {
    setWorkspaceState(demoMode ? "Демо готово" : "Ожидание DXF", demoMode ? "ready" : "");
  }
}

function renderOptions(payload) {
  options = normalizedOptions(payload);
  mappingSelect.innerHTML = options.mappings.map((item) => (
    `<option value="${escapeHtml(item.id)}">${escapeHtml(item.title)}</option>`
  )).join("");
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
  document.querySelector("#max-details").value = options.defaults.max_details;
  document.querySelector("#min-width").value = options.defaults.min_width_cells;
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
  return [
    `<span><b>${escapeHtml(layer)} · ось ${escapeHtml(source.direction.axis)}</b></span>`,
    `<span><b>${number(source.cell_count)}</b> КЭ</span>`,
    `<span><b>${number(source.level_count)}</b> уровней</span>`,
    `<span><b>${number(width)} × ${number(height)}</b> мм</span>`,
    `<span>источник: <b>${escapeHtml(mapping)}</b></span>`,
  ].join("");
}

function metricCard(value, label, danger = false) {
  return `<div class="metric-card${danger ? " danger" : ""}"><strong>${value}</strong><span>${label}</span></div>`;
}

function renderTrajectory(solution) {
  const panel = document.querySelector("#trajectory-panel");
  const chart = document.querySelector("#trajectory-chart");
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
  panel.hidden = false;
}

function renderActiveSolution() {
  const solution = analysis.solutions[activeSolution];
  const metrics = solution.metrics;
  const invalid = metrics.under_reinforced_cell_count > 0 || solution.status === "error";

  document.querySelectorAll(".solution-tab").forEach((tab, index) => {
    tab.setAttribute("aria-selected", String(index === activeSolution));
  });
  document.querySelector("#active-algorithm").textContent = algorithmTitle(solution.algorithm);
  const status = document.querySelector("#active-status");
  const statusLabels = {
    optimal: "оптимально",
    feasible: "допустимо",
    time_limit: "лимит времени",
    error: "ошибка",
  };
  status.textContent = invalid ? "требует внимания" : (statusLabels[solution.status] ?? solution.status);
  status.classList.toggle("bad", invalid);
  const drawing = document.querySelector("#drawing");
  drawing.innerHTML = solution.svg;
  drawing.classList.toggle("show-detailing", showDetailing);
  document.querySelector("#active-metrics").innerHTML = [
    metricCard(`${number(metrics.total_mass_kg, 1)} кг`, "масса дополнительной арматуры"),
    metricCard(number(metrics.detail_count), "прямоугольных зон"),
    metricCard(number(solution.physical_bar_count), "физических стержней"),
    metricCard(number(metrics.overcovered_cell_count), "КЭ с перерасходом"),
    metricCard(number(metrics.under_reinforced_cell_count), "недоармированных КЭ", invalid),
    metricCard(`${number(solution.runtime_ms, 1)} мс`, "время алгоритма"),
  ].join("");
  renderTrajectory(solution);

  const diagnostics = solution.diagnostics.length
    ? solution.diagnostics
    : ["Нарушения не обнаружены в текущей MVP-модели."];
  document.querySelector("#diagnostics-list").innerHTML = diagnostics
    .map((message) => `<li>${escapeHtml(message)}</li>`).join("");
  document.querySelector("#zones-body").innerHTML = solution.zones.map((zone) => `
    <tr>
      <td>${escapeHtml(zone.id)}</td><td>${number(zone.level_index)}</td>
      <td>⌀${number(zone.rebar.diameter)} / ${number(zone.rebar.step)}</td>
      <td>${number(zone.width_mm)} мм</td><td>${number(zone.anchored_length_mm)} мм</td>
      <td>${number(zone.installed_length_mm)} мм</td>
      <td>${number(zone.bar_count)}</td><td>${number(zone.mass_kg, 1)} кг</td>
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
  workbenchTitle.textContent = "Результат расчёта";
  document.querySelector("#result-filename").textContent = payload.source.filename;
  document.querySelector("#source-facts").innerHTML = sourceFacts(payload.source);
  document.querySelector("#solution-tabs").innerHTML = payload.solutions.map((solution, index) => `
    <button class="solution-tab" type="button" role="tab" data-index="${index}" aria-selected="${index === 0}">
      <strong>${escapeHtml(algorithmTitle(solution.algorithm))}</strong>
      <small>${number(solution.metrics.total_mass_kg, 1)} кг · ${number(solution.metrics.detail_count)} зон</small>
    </button>
  `).join("");
  document.querySelectorAll(".solution-tab").forEach((tab) => tab.addEventListener("click", () => {
    activeSolution = Number(tab.dataset.index);
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
  const demoMode = sourceMode() === "demo";
  if (!demoMode && !dxfInput.files[0]) {
    showError("Сначала выберите DXF.");
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
  } else {
    body.append("dxf", dxfInput.files[0]);
    if (shkInput.files[0]) body.append("shk", shkInput.files[0]);
    body.append("mapping_id", mappingSelect.value);
  }
  body.append("algorithms", selectedAlgorithms.join(","));
  body.append("max_details", document.querySelector("#max-details").value);
  body.append("min_width_cells", document.querySelector("#min-width").value);
  body.append("detail_penalty_kg", document.querySelector("#detail-penalty").value);
  body.append("cutting_profile", cuttingProfileSelect.value);

  submitButton.disabled = true;
  results.hidden = true;
  emptyState.hidden = true;
  loading.hidden = false;
  workbenchTitle.textContent = "Выполнение расчёта";
  setWorkspaceState("Расчёт выполняется", "busy");
  try {
    const endpoint = demoMode ? "/api/demo" : "/api/analyze";
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

loadOptions();
