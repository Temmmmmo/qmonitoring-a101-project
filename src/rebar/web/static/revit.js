"use strict";

const toolsContainer = document.getElementById("tools");
const statusElement = document.getElementById("catalog-status");
const errorElement = document.getElementById("catalog-error");

function textElement(tag, text, className) {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function toolCard(tool) {
  const card = document.createElement("article");
  card.className = "tool-card";
  const modeLabel = tool.mode === "read_only" ? "Только чтение" :
    tool.mode === "view_family" ? "Семейства на виде, не Rebar" : "Графический вид, не Rebar";
  card.append(textElement("span", modeLabel, "tool-mode"));
  card.append(textElement("h3", tool.title));
  card.append(textElement("p", tool.description));
  card.append(textElement("p", `${tool.tab} → ${tool.panel || "Diagnostics"} → ${tool.command}`, "command-path"));
  card.append(textElement("p", `Runtime ${tool.runtime_version} · ${Math.ceil(tool.bytes / 1024)} КБ`, "metadata"));
  const link = textElement("a", "Скачать расширение ZIP", "download");
  const expectedUrl = `/api/revit/tools/${encodeURIComponent(tool.id)}/${encodeURIComponent(tool.version)}/download`;
  if (tool.download_url !== expectedUrl) throw new Error("Каталог содержит неверную ссылку скачивания.");
  link.href = expectedUrl;
  link.download = tool.filename;
  card.append(link);
  const details = document.createElement("details");
  details.append(textElement("summary", "Инструкция, ограничения и контрольная сумма"));
  const steps = document.createElement("ol");
  tool.steps.forEach((step) => steps.append(textElement("li", step)));
  details.append(steps);
  details.append(textElement("p", tool.limitations));
  details.append(textElement("p", tool.verification, "verification"));
  details.append(textElement("p", `Версия сборки: ${tool.version}`, "metadata"));
  details.append(textElement("code", `SHA256: ${tool.sha256}`, "checksum"));
  if (tool.accepted_input_schemas.length) {
    details.append(textElement("p", `Входные форматы: ${tool.accepted_input_schemas.join(", ")}`));
  }
  card.append(details);
  return card;
}

async function loadCatalog() {
  try {
    const response = await fetch("/api/revit/tools", {headers: {Accept: "application/json"}, cache: "no-store"});
    const catalog = await response.json();
    if (!response.ok) throw new Error(catalog.detail || "Не удалось получить пакеты Revit.");
    if (catalog.schema_version !== "qmonitoring-revit-tools/v1" || !Array.isArray(catalog.tools)) {
      throw new Error("Версия списка инструментов не поддерживается. Обновите страницу.");
    }
    const cards = catalog.tools.map(toolCard);
    toolsContainer.replaceChildren(...cards);
    statusElement.textContent = "Пакеты доступны. Версия и SHA256 указаны для каждого архива.";
  } catch (error) {
    statusElement.textContent = "Скачивание сейчас недоступно.";
    errorElement.textContent = error.message;
    errorElement.hidden = false;
  }
}

loadCatalog();
