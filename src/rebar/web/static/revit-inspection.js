"use strict";

(() => {
  const form = document.getElementById("snapshot-form");
  const button = document.getElementById("inspect-button");
  const progress = document.getElementById("inspection-progress");
  const error = document.getElementById("inspection-error");
  const result = document.getElementById("inspection-result");
  const labels = {
    not_compared: "Один снимок: сопоставление не выполнено",
    mismatch: "Снимки не совпадают по идентификаторам — не использовать вместе",
    insufficient_identity: "Недостаточно идентификаторов для сопоставления",
    host_matches_document_unverified: "UID плиты совпал; идентичность документа не подтверждена",
    recorded_identity_matches: "Записанные UID плиты и документа совпали — актуальность RVT не проверена",
  };
  function element(tag, text, className) {
    const node = document.createElement(tag);
    node.textContent = text;
    if (className) node.className = className;
    return node;
  }
  function renderSnapshot(kind, snapshot) {
    const card = element("article", "", "snapshot-card");
    card.append(element("h3", kind === "host" ? "Геометрия плиты" : "Существующая арматура"));
    card.append(element("p", `${snapshot.document.title} · ${snapshot.document.is_workshared ? "С рабочими наборами" : "Несовместная модель"}`));
    card.append(element("p", `Плита: ID ${snapshot.host.element_id} · ${snapshot.host.mark || "без марки"}`));
    card.append(element("code", `UID: ${snapshot.host.unique_id || "не прочитан"}`));
    card.append(element("p", `Статус снимка: ${snapshot.reported_status} · замечаний чтения: ${snapshot.read_issue_count}`));
    if (snapshot.inventory) {
      const inventory = snapshot.inventory;
      const metrics = element("dl", "", "snapshot-metrics");
      [
        ["Элементы Rebar / RebarInSystem", inventory.host_reinforcement_element_count_read],
        ["Прочитанные физические положения", inventory.read_existing_position_count],
        ["Из них с записями Line / Arc", inventory.exact_geometry_position_count],
        ["Исключённые положения", inventory.excluded_position_count],
        ["Непрочитанные положения известных наборов", inventory.unknown_position_count],
        ["Полное количество по отчёту", inventory.physical_bar_count_as_reported ?? "не установлено"],
      ].forEach(([label, value]) => { metrics.append(element("dt", label), element("dd", String(value))); });
      card.append(metrics);
      card.append(element("p", inventory.status === "complete_as_reported"
        ? "Сводка согласована с записями снимка. Это не независимая проверка существования стержней в RVT."
        : "Неполный снимок: количество не является полной инвентаризацией плиты.", "inspection-caution"));
      card.append(element("p", `Area/Path (не дополнительные стержни): ${inventory.parent_system_count}. Неподдержанные элементы host: ${inventory.unsupported_host_element_count}. Связанные модели: ${inventory.linked_model_count} — их арматура не прочитана.`));
    } else {
      card.append(element("p", `Записей граней Solid: ${snapshot.geometry.solid_face_record_count ?? "не прочитаны"}. Топология и привязка DXF здесь не проверяются.`));
    }
    const closed = snapshot.worksharing.closed_worksets;
    card.append(element("p", closed === null ? "Состояние закрытых рабочих наборов неизвестно."
      : closed.length ? `Закрытые рабочие наборы: ${closed.join(", ")}` : "По отчёту закрытых рабочих наборов нет."));
    if (snapshot.read_issues.length) {
      const details = document.createElement("details");
      details.append(element("summary", "Замечания чтения"));
      const list = document.createElement("ul");
      snapshot.read_issues.forEach((issue) => list.append(element("li", `${issue.section}: ${issue.message}`)));
      details.append(list);
      if (snapshot.read_issues_omitted_from_display) details.append(element("p", `Ещё ${snapshot.read_issues_omitted_from_display} замечаний — в исходном файле.`));
      card.append(details);
    }
    card.append(element("p", `Снимок: ${snapshot.created_utc} · Probe ${snapshot.probe_version}`, "metadata"));
    card.append(element("code", `SHA256 загруженных байтов: ${snapshot.sha256}`, "checksum"));
    return card;
  }
  function render(report) {
    if (report.schema_version !== "qmonitoring-revit-snapshot-inspection/v1" || report.placement_eligible !== false
        || report.engineering_approval !== false || report.live_model_checked !== false) {
      throw new Error("Неизвестный формат ответа инспектора. Обновите страницу.");
    }
    const binding = element("div", "", report.binding.status === "mismatch" ? "error" : "inspection-caution");
    binding.append(element("strong", labels[report.binding.status] || "Сопоставление неизвестно"));
    binding.append(element("p", report.binding.note));
    if (report.binding.document_path_hash_matches === false) {
      binding.append(element("p", "В файлах указаны разные пути документа. Это могут быть разные копии или версии модели."));
    }
    const cards = element("div", "", "snapshot-cards");
    ["host", "rebar"].forEach((kind) => { if (report.snapshots[kind]) cards.append(renderSnapshot(kind, report.snapshots[kind])); });
    const limits = element("p", "Не проверены: " + report.not_checked.join("; ") + ". Снимки не подключены к расчёту автоматически.", "inspection-caution");
    result.replaceChildren(binding, cards, limits);
    result.hidden = false;
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    error.hidden = true;
    result.hidden = true;
    button.disabled = true;
    progress.textContent = "Читаем и проверяем переданные JSON…";
    try {
      const data = new FormData();
      let total = 0;
      ["host", "rebar"].forEach((kind) => {
        const file = document.getElementById("snapshot-" + kind).files[0];
        if (file) { data.append(kind, file); total += file.size; }
      });
      if (!total) throw new Error("Выберите хотя бы один непустой JSON-отчёт.");
      if (total > 32 * 1024 * 1024) throw new Error("Суммарный размер JSON превышает 32 MiB.");
      const response = await fetch("/api/revit/inspect-snapshots", {method: "POST", body: data, cache: "no-store"});
      const report = await response.json();
      if (!response.ok) throw new Error(typeof report.detail === "string" ? report.detail : "Не удалось прочитать снимки.");
      render(report);
      progress.textContent = "Проверка переданных файлов завершена. RVT не читался и не изменялся.";
    } catch (failure) {
      error.textContent = failure.message;
      error.hidden = false;
      progress.textContent = "Снимки не приняты; результат расчёта не менялся.";
    } finally { button.disabled = false; }
  });
})();
