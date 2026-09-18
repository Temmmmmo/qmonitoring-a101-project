/* The shared server history only contains small summaries, never report bodies. */
(() => {
  const list = document.querySelector("#job-history-list");
  const status = document.querySelector("#job-history-status");
  const refreshButton = document.querySelector("#refresh-job-history");
  let loading = false;

  async function refresh() {
    if (loading) return;
    loading = true;
    refreshButton.disabled = true;
    try {
      const response = await fetch("/api/analyze-composite-plate/jobs", {cache: "no-store"});
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const payload = await response.json();
      if (!Array.isArray(payload.jobs)) throw new Error("Неверный формат списка");
      const rows = payload.jobs.map((job) => {
        const row = document.createElement("li");
        const link = document.createElement("a");
        link.href = `/composite?job=${encodeURIComponent(job.job_id)}`;
        const heading = document.createElement("span");
        const title = document.createElement("strong");
        title.textContent = job.case_id || "Своя плита";
        const date = document.createElement("small");
        date.textContent = new Date(job.created_at * 1000).toLocaleString("ru-RU") + ` · ${job.job_id.slice(0, 8)}`;
        heading.append(title, date);
        const state = document.createElement("span");
        state.textContent = job.status === "running" ? `Выполняется · ${job.progress_percent}% · ${job.progress_stage}`
          : job.status === "complete" ? "Готово · открыть результат →" : "Ошибка · открыть пояснение →";
        link.append(heading, state);
        row.append(link);
        return row;
      });
      list.replaceChildren(...rows);
      status.textContent = rows.length ? "" : "Сохранённых расчётов пока нет.";
    } catch {
      status.textContent = "Не удалось обновить список расчётов. Нажмите «Обновить список».";
    } finally {
      loading = false;
      refreshButton.disabled = false;
    }
  }

  window.refreshCompositeHistory = refresh;
  refreshButton.addEventListener("click", refresh);
  refresh();
  setInterval(refresh, 15000);
})();
