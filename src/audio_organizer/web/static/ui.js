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
})();
