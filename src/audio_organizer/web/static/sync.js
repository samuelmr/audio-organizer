// Live progress of a sync: polls the job's JSON and updates the page in place. Without
// this script the page shows the state when it was loaded, with a Refresh link. The gnudb
// lookups page has the same fields and JSON, so this shows its progress too.
(() => {
  const root = document.querySelector("[data-sync-job]");
  if (!root) return;
  const url = root.dataset.syncJob;
  const field = (name) => root.querySelector(`[data-field="${name}"]`);
  const refresh = field("refresh");
  if (refresh) refresh.hidden = true;
  let wasActive = Boolean(field("stop"));

  const show = (job) => {
    const label = field("label");
    label.textContent = job.label;
    label.className = `status ${job.status}`;
    field("message").textContent = job.message || "";
    field("counts").textContent = job.counts;
    field("current").textContent = job.current || "";
    const progress = field("progress");
    progress.hidden = job.status === "planning" && !job.total;
    progress.max = job.total || 1;
    progress.value = job.done;
    const stop = field("stop");
    if (stop && job.stop_requested) {
      const button = stop.querySelector("button");
      button.disabled = true;
      button.textContent = "Stopping…";
    }
    const errors = field("errors");
    if (job.errors.length) {
      errors.hidden = false;
      const body = errors.querySelector("tbody");
      body.replaceChildren(...job.errors.map(([action, error]) => {
        const row = document.createElement("tr");
        for (const [text, cls] of [[action, "path"], [error, "error"]]) {
          const cell = row.insertCell();
          cell.className = cls;
          cell.textContent = text;
        }
        return row;
      }));
    }
  };

  const poll = async () => {
    let job;
    try {
      const response = await fetch(url, { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(response.statusText);
      job = await response.json();
    } catch {
      setTimeout(poll, 5000); // the server may be restarting; try again a bit later
      return;
    }
    show(job);
    if (job.active) {
      setTimeout(poll, 1000);
    } else if (wasActive) {
      wasActive = false;
      location.reload(); // the finished page has its own links and messages
    }
  };

  const stop = field("stop");
  if (stop) {
    stop.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = stop.querySelector("button");
      button.disabled = true;
      button.textContent = "Stopping…";
      await fetch(stop.action, { method: "POST" }).catch(() => {});
    });
  }

  if (wasActive) poll();
})();

// The plan page while the plan is made in the background: shows its progress, then loads
// the page again to show the plan.
(() => {
  const root = document.querySelector("[data-sync-plan]");
  if (!root) return;
  const field = (name) => root.querySelector(`[data-field="${name}"]`);
  const refresh = field("refresh");
  if (refresh) refresh.hidden = true;

  const poll = async () => {
    let state;
    try {
      const response = await fetch(root.dataset.syncPlan, { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(response.statusText);
      state = await response.json();
    } catch {
      setTimeout(poll, 5000);
      return;
    }
    if (!state.active) {
      location.reload();
      return;
    }
    field("counts").textContent = state.counts;
    const progress = field("progress");
    progress.hidden = !state.total;
    progress.max = state.total || 1;
    progress.value = state.done;
    setTimeout(poll, 1000);
  };
  poll();
})();
