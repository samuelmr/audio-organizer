// Small conveniences on top of plain forms; every page works without them.
(() => {
  // Rows with data-select-row: a click anywhere in the row, except on its links, buttons
  // and inputs, selects its radio button or toggles its checkbox.
  document.addEventListener("click", (event) => {
    const row = event.target.closest("tr[data-select-row]");
    if (!row || event.target.closest("a, button, input, label, select, textarea")) return;
    const input = row.querySelector("input[type=radio], input[type=checkbox]");
    if (input) input.click();
  });

  // Covers that are missing leave their empty square instead of a broken image
  document.querySelectorAll(".thumb img").forEach((img) => {
    if (img.complete && !img.naturalWidth) img.remove();
    else img.addEventListener("error", () => img.remove());
  });

  // Forms with data-busy (long-running actions: lookups, proposals, applying and undoing
  // changes): while one is submitted the page loads, so its button shows a spinner and
  // every other data-busy button on the page is disabled, so that no second run is
  // started alongside it. The server refuses a parallel run too (409) for other tabs.
  const busyButtons = () => [
    ...document.querySelectorAll("form[data-busy] button, button[form][type=submit]"),
  ].filter((button) => button.form?.hasAttribute("data-busy"));
  const setBusy = (active, submitter) => {
    busyButtons().forEach((button) => {
      button.disabled = active;
      button.classList.toggle("busy", active && button === submitter);
    });
  };
  document.querySelectorAll("form[data-busy]").forEach((form) => {
    form.addEventListener("submit", (event) => {
      // After the event, so that the clicked button's own name and value are still sent,
      // and not when a confirm() or another handler cancelled the submit
      setTimeout(() => {
        if (!event.defaultPrevented) setBusy(true, event.submitter);
      }, 0);
    });
  });
  // Coming back with the browser's back button restores the page as it was left
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) setBusy(false);
  });

  // A checkbox with data-select-all="name" selects or deselects all checkboxes of that
  // name, and shows a partial selection.
  document.querySelectorAll("input[data-select-all]").forEach((all) => {
    const boxes = [...document.querySelectorAll(`input[name="${all.dataset.selectAll}"]`)];
    if (!boxes.length) return;
    all.hidden = false;
    const sync = () => {
      const checked = boxes.filter((b) => b.checked).length;
      all.checked = checked === boxes.length;
      all.indeterminate = checked > 0 && checked < boxes.length;
    };
    all.addEventListener("change", () => {
      // Dispatching change lets the group checkboxes below follow
      boxes.forEach((b) => {
        b.checked = all.checked;
        b.dispatchEvent(new Event("change"));
      });
      sync();
    });
    boxes.forEach((b) => b.addEventListener("change", sync));
    sync();
  });

  // A checkbox with data-select-group="n" (an album's header row) selects or deselects the
  // file_ids checkboxes in the rows with data-group="n", and shows a partial selection.
  document.querySelectorAll("input[data-select-group]").forEach((all) => {
    const boxes = [...document.querySelectorAll(
      `tr[data-group="${all.dataset.selectGroup}"] input[name="file_ids"]`)];
    if (!boxes.length) return;
    all.hidden = false;
    const sync = () => {
      const checked = boxes.filter((b) => b.checked).length;
      all.checked = checked === boxes.length;
      all.indeterminate = checked > 0 && checked < boxes.length;
    };
    all.addEventListener("change", () => {
      boxes.forEach((b) => {
        b.checked = all.checked;
        b.dispatchEvent(new Event("change"));
      });
    });
    boxes.forEach((b) => b.addEventListener("change", sync));
    sync();
  });

  // Playlist file places (form.place): typing a folder or choosing formats updates the
  // listed paths at once, so that the buttons visibly act on the path shown. Without
  // JavaScript the "Update paths" button does the same with a page load.
  document.querySelectorAll(".place").forEach((form) => {
    const list = form.querySelector("ul.paths");
    const folder = form.querySelector("input[name=folder]");
    if (!list || !folder) return;
    const names = JSON.parse(form.dataset.names || "[]");
    form.querySelectorAll(".no-js-only").forEach((el) => { el.hidden = true; });
    const render = () => {
      const dir = folder.value.trim().replace(/\\/g, "/").split("/")
        .filter((part) => part && part !== ".").join("/");
      const formats = [...form.querySelectorAll("input[name=formats]:checked")].map((b) => b.value.split(":").pop());
      const base = form.dataset.root.replace(/\/+$/, "") + (dir ? "/" + dir : "");
      list.replaceChildren(...names.flatMap((name) => formats.map((fmt) => {
        const item = document.createElement("li");
        const code = document.createElement("code");
        code.textContent = `${base}/${name}.${fmt}`;
        item.append(code);
        return item;
      })));
    };
    form.querySelectorAll("[data-live]").forEach((input) => {
      input.addEventListener("input", render);
      input.addEventListener("change", render);
    });
  });
})();
