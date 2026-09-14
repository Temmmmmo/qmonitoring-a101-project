"use strict";

// Shared start card. Only backend-published metadata, never invented baseline values.
window.engineeringExampleReady = (async () => {
  const get = (id) => document.getElementById(id);
  const button = get("run-engineering-example");
  const status = get("example-status");
  const number = (value, suffix) => Number.isFinite(value)
    ? new Intl.NumberFormat("ru-RU", {maximumFractionDigits: 2}).format(value) + suffix : "не указан";
  try {
    const response = await fetch("/api/engineering-examples", {cache: "no-store"});
    const payload = await response.json();
    if (!response.ok) throw new Error(typeof payload.detail === "string" ? payload.detail : "Каталог примеров недоступен.");
    if (!Array.isArray(payload.examples)) throw new Error("Неверный формат каталога примеров.");
    const requested = new URLSearchParams(window.location.search).get("example");
    const example = requested ? payload.examples.find((item) => item.id === requested) : payload.examples[0];
    if (!example || !/^[a-zA-Z0-9_-]+$/.test(example.id)) throw new Error("Инженерный пример не найден в этой поставке.");
    get("example-title").textContent = example.title || "К09 · инженерный пример";
    get("example-description").textContent = example.description || "Настоящий комплект четырёх направлений армирования.";
    const facts = get("example-facts");
    const items = ["4 направления", "Реальный DXF-комплект", "A500", "Пруток 11,7 м"];
    facts.replaceChildren(...items.map((text) => { const span = document.createElement("span"); span.textContent = text; return span; }));
    const reference = example.reference || {};
    get("example-mass").textContent = number(reference.mass_kg, " кг");
    get("example-bars").textContent = number(reference.physical_bar_count, " шт.");
    get("example-positions").textContent = number(reference.position_count, " поз.");
    get("example-reference-note").textContent = [reference.scope, reference.note].filter(Boolean).join(" ") || "Эталон из инженерной выдачи. Сравнение не заменяет проверку безопасности.";
    get("example-profile-note").textContent = example.profile?.note || "Описание профиля не передано. Выбранные фазы и высоты не считаются инженерным согласованием.";
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
      throw new Error(example.unavailable_reason || (example.status !== "ready" ? example.status : null)
        || "Материалы этого примера не включены в текущую поставку.");
    }
    button.disabled = false;
    status.textContent = "Данные предзаполнены. Кнопка запускает новый расчёт, а не готовую картинку.";
    return example;
  } catch (failure) {
    status.textContent = failure.message + " Можно загрузить свой комплект ниже.";
    button.disabled = true;
    return null;
  }
})();
