document.addEventListener("DOMContentLoaded", () => {
  const sidebar = document.querySelector("#sidebar");
  const toggle = document.querySelector("[data-sidebar-toggle]");
  if (toggle && sidebar) {
    toggle.addEventListener("click", () => sidebar.classList.toggle("open"));
  }

  document.querySelectorAll("form[data-confirm]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      if (!window.confirm(form.dataset.confirm)) event.preventDefault();
    });
  });

  const workerLabel = document.querySelector("#worker-label");
  if (workerLabel) {
    const formatTime = (value) => {
      if (!value) return "—";
      const parsed = new Date(value);
      return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
    };
    window.setInterval(async () => {
      try {
        const response = await window.fetch("/api/status", {
          headers: { Accept: "application/json" },
          credentials: "same-origin",
        });
        if (!response.ok) return;
        const data = await response.json();
        workerLabel.textContent = data.worker_health.label;
        const statusDot = document.querySelector(".status-title .status-dot");
        if (statusDot) statusDot.className = `status-dot ${data.worker_health.status}`;
        const nextRun = document.querySelector("#next-run");
        const updated = document.querySelector("#updated-at");
        if (nextRun && data.state) nextRun.textContent = formatTime(data.state.next_run);
        if (updated && data.state) updated.textContent = formatTime(data.state.updated_at);
      } catch (_error) {
        // The server-rendered status remains visible when a refresh fails.
      }
    }, 15000);
  }
});
