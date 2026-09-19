"use strict";

// Shared start card. Only backend-published metadata, never invented baseline values.
window.engineeringExampleReady = (async () => {
  const get = (id) => document.getElementById(id);
  const button = get("run-engineering-example");
  const compare = get("compare-engineering-example");
  const status = get("example-status");
  const number = (value, suffix) => Number.isFinite(value)
    ? new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 2}).format(value) + suffix : "не указан";
  try {
    const response = await fetch("/api/engineering-examples", {cache: "no-store"});
    let payload = null;
    try {
      if (typeof response.text === "function") {
        const raw = await response.text();
        payload = raw ? JSON.parse(raw) : null;
      } else if (typeof response.json === "function") {
        payload = await response.json();
      }
    } catch { payload = null; }
    if (!payload) throw new Error(`Сервер не вернул каталог примеров (HTTP ${response.status}). Повторите загрузку страницы.`);
    if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "Каталог примеров недоступен.");
    if (!Array.isArray(payload.examples)) throw new Error("Неверный формат каталога примеров.");
    const requested = new URLSearchParams(window.location.search).get("example");
    const chosen = requested || payload.default_example_id || payload.examples[0]?.id;
    const example = payload.examples.find((item) => item.id === chosen);
    if (!example || !/^[a-zA-Z0-9_-]+$/.test(example.id)) throw new Error("Инженерный пример не найден в этой поставке.");
    get("example-title").textContent = example.title || "Инженерный пример";
    const selector = get("example-selector");
    if (selector) {
      selector.replaceChildren(...payload.examples.map((item) => {
        const option = document.createElement("option"); option.value = item.id;
        option.textContent = item.title; option.selected = item.id === example.id; return option;
      }));
      selector.addEventListener("change", () => {
        const url = new URL(window.location.href); url.searchParams.set("example", selector.value);
        url.searchParams.delete("run"); window.location.assign(url);
      });
    }
    button.textContent = window.location.pathname === "/partitions" ? "Рассчитать и сравнить варианты" : "Рассчитать плиту";
    if (compare) {
      compare.href = `/partitions?example=${encodeURIComponent(example.id)}&run=1`;
      compare.hidden = false;
    }
    const hostOptions = document.querySelector(".boundary-trim-options");
    if (hostOptions) hostOptions.hidden = example.supports_working_host_trim === false;
    const baseDescription = example.description || "Настоящий комплект четырёх направлений армирования.";
    get("example-description").textContent = window.location.pathname === "/partitions"
      ? `${baseDescription} Для сравнения применяются стандартные длины до 11 700 мм; раскрой проверяется отдельно для каждого варианта и не считается пройденным заранее.`
      : baseDescription;
    const facts = get("example-facts");
    const items = ["4 направления", "Оригинальные DXF", "Новый расчёт по выбранному комплекту"];
    facts.replaceChildren(...items.map((text) => { const span = document.createElement("span"); span.textContent = text; return span; }));
    const reference = example.reference || {};
    get("example-mass").textContent = number(reference.mass_kg, " кг");
    get("example-bars").textContent = number(reference.physical_bar_count, " шт.");
    get("example-positions").textContent = number(reference.position_count, " поз.");
    get("example-reference-note").textContent = [reference.scope, reference.note].filter(Boolean).join(" ") ||
      "Для этого комплекта нет отдельного сопоставимого эталона. Проверяйте исходную потребность и гейты результата.";
    get("example-profile-note").textContent = window.location.pathname === "/partitions"
      ? "Режим сравнения: фазы 0/100/0 мм для готовых примеров, контрольные 40d, каталожные длины и активная область исходных КЭ. Высоты стержней не назначены; раскрой партии проверяется отдельно для каждого варианта."
      : example.profile?.note || "Описание профиля не передано. Выбранные фазы и высоты не считаются инженерным согласованием.";
    const files = get("example-files");
    if (files && Array.isArray(example.sources)) {
      files.replaceChildren(...example.sources.map((source) => {
        const row = document.createElement("div"); row.className = "example-file";
        const label = document.createElement("strong");
        label.textContent = `${source.direction?.layer === "top" ? "Верх" : "Низ"} · ${source.direction?.axis || "?"}`;
        const name = document.createElement("span"); name.textContent = source.dxf_filename || "DXF не указан";
        const scale = document.createElement("small"); scale.textContent = source.shk_filename || source.mapping_label || "Шкала не подтверждена";
        row.append(label, name, scale); return row;
      }));
    }
    if (example.is_available !== true || example.source_kind !== "real_engineering_files") {
      const alternative = payload.examples.find((item) => item.id !== example.id && item.is_available === true &&
        item.source_kind === "real_engineering_files");
      throw new Error((example.unavailable_reason || (example.status !== "ready" ? example.status : null)
        || "Материалы этого примера не включены в текущую поставку.") +
        (alternative ? ` Доступен ${alternative.title}: выберите его в списке.` : ""));
    }
    button.disabled = false;
    status.textContent = "Четыре оригинальных DXF проверены. Кнопка запускает новый расчёт.";
    return example;
  } catch (failure) {
    status.textContent = failure.message + " Расчёт выбранной плиты недоступен.";
    button.disabled = true;
    if (compare) compare.hidden = true;
    return null;
  }
})();
