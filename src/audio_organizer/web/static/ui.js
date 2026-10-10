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

  // Forms with data-busy (slow lookups that wait for an online answer): the button is
  // disabled and shows a spinner while the page loads, so it is not clicked twice.
  document.querySelectorAll("form[data-busy]").forEach((form) => {
    form.addEventListener("submit", () => {
      const button = form.querySelector("button");
      if (!button) return;
      button.disabled = true;
      button.classList.add("busy");
    });
  });
  // Coming back with the browser's back button restores the page as it was left
  window.addEventListener("pageshow", (event) => {
    if (!event.persisted) return;
    document.querySelectorAll("form[data-busy] button").forEach((button) => {
      button.disabled = false;
      button.classList.remove("busy");
    });
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
      boxes.forEach((b) => { b.checked = all.checked; });
      sync();
    });
    boxes.forEach((b) => b.addEventListener("change", sync));
    sync();
  });

  // Playlist file places (form.place): typing a folder or choosing formats updates the
  // listed paths at once, so that the buttons visibly act on the path shown. Without
  // JavaScript the "Update paths" button does the same with a page load.
  document.querySelectorAll("form.place").forEach((form) => {
    const list = form.querySelector("ul.paths");
    const folder = form.querySelector("input[name=folder]");
    if (!list || !folder) return;
    const names = JSON.parse(form.dataset.names || "[]");
    form.querySelectorAll(".no-js-only").forEach((el) => { el.hidden = true; });
    const render = () => {
      const dir = folder.value.trim().replace(/\\/g, "/").split("/")
        .filter((part) => part && part !== ".").join("/");
      const formats = [...form.querySelectorAll("input[name=formats]:checked")].map((b) => b.value);
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
