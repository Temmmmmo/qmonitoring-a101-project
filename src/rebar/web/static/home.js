"use strict";
document.getElementById("run-engineering-example").addEventListener("click", async () => {
  const example = await window.engineeringExampleReady;
  if (example) window.location.assign("/composite?example=" + encodeURIComponent(example.id) + "&run=1");
});
if (new URLSearchParams(window.location.search).get("advanced") === "1" || window.location.hash === "advanced") {
  document.getElementById("advanced").open = true;
}
