// Rendering + interaction wiring for the dummy spreadsheet app.

let activeSheet = SHEET_NAMES[0];
let highlightedRowKey = null;
let searchFilter = "";
let addRowFormOpen = false;

function $(sel) {
  return document.querySelector(sel);
}

function renderTabs() {
  const tabsEl = $("#tabs");
  tabsEl.innerHTML = "";
  SHEET_NAMES.forEach((name) => {
    const btn = document.createElement("button");
    btn.textContent = name;
    btn.className = "tab" + (name === activeSheet ? " active" : "");
    btn.dataset.sheet = name;
    btn.addEventListener("click", () => onTabClick(name, btn));
    tabsEl.appendChild(btn);
  });
}

function onTabClick(name, el) {
  activeSheet = name;
  highlightedRowKey = null;
  searchFilter = "";
  addRowFormOpen = false;
  $("#search-box").value = "";
  Logger.logSelectSheet(name, el);
  renderTabs();
  renderTable();
  renderLog();
}

function rowMatchesFilter(sheet, row) {
  if (!searchFilter) return true;
  const needle = searchFilter.toLowerCase();
  return sheet.columns.some((col) =>
    String(row[col.field] ?? "").toLowerCase().includes(needle)
  );
}

function renderTable() {
  const sheet = SEED_DATA[activeSheet];
  const container = $("#table-container");
  container.innerHTML = "";

  const table = document.createElement("table");
  table.setAttribute("role", "grid");
  table.dataset.sheet = activeSheet;

  const thead = document.createElement("thead");
  const headRow = document.createElement("tr");
  sheet.columns.forEach((col) => {
    const th = document.createElement("th");
    th.textContent = col.label;
    headRow.appendChild(th);
  });
  thead.appendChild(headRow);
  table.appendChild(thead);

  const tbody = document.createElement("tbody");
  const visibleRows = sheet.rows.filter((row) => rowMatchesFilter(sheet, row));

  visibleRows.forEach((row) => {
    const rowKey = row[sheet.rowKey];
    const tr = document.createElement("tr");
    tr.dataset.rowKey = rowKey;
    if (rowKey === highlightedRowKey) tr.classList.add("highlight");

    sheet.columns.forEach((col) => {
      const td = document.createElement("td");
      td.setAttribute("role", "gridcell");
      td.dataset.field = col.field;
      // The column header, so a cell can be addressed the way a person
      // names it. Evidence records headers, never internal field names,
      // so anything derived from evidence asks for "Invoice Qty".
      td.dataset.label = col.label;
      td.dataset.rowKey = rowKey;
      td.textContent = row[col.field];

      // Every cell is clickable. Whether the resulting event is a READ or
      // a WRITE is decided at commit time based on whether the value
      // actually changed (see beginEdit's commit()).
      td.classList.add("editable");
      td.title = "Click to view / edit";
      // Single click SELECTS; Ctrl/Cmd+C captures the read; double click edits.
      // Reading a value is what an operator does constantly, so it needs a
      // gesture that is explicit but cheap, and that cannot turn into an
      // accidental write. Click-to-edit-then-leave-unchanged was neither.
      td.addEventListener("click", () => selectCell(td, sheet, row, col, rowKey));
      td.addEventListener("dblclick", () => beginEdit(td, sheet, row, col, rowKey));
      if (selectedCell && selectedCell.rowKey === rowKey &&
          selectedCell.col.field === col.field) {
        td.classList.add("selected");
        selectedCell.td = td;   // survive the re-render
      }
      tr.appendChild(td);
    });
    tbody.appendChild(tr);
  });
  table.appendChild(tbody);
  container.appendChild(table);

  if (searchFilter && visibleRows.length === 0) {
    const notice = document.createElement("div");
    notice.className = "no-results";
    notice.textContent = `No rows match "${searchFilter}" in ${activeSheet}.`;
    container.appendChild(notice);
  }

  renderAddRowForm(sheet, container);
}

function renderAddRowForm(sheet, container) {
  const bar = document.createElement("div");
  bar.className = "add-row-bar";

  const toggleBtn = document.createElement("button");
  toggleBtn.textContent = addRowFormOpen ? "Cancel" : "+ Add Row";
  toggleBtn.addEventListener("click", () => {
    addRowFormOpen = !addRowFormOpen;
    renderTable();
  });
  bar.appendChild(toggleBtn);
  container.appendChild(bar);

  if (!addRowFormOpen) return;

  const form = document.createElement("form");
  form.className = "add-row-form";

  const inputs = {};
  sheet.columns.forEach((col) => {
    const wrap = document.createElement("label");
    wrap.textContent = col.label;
    const input = document.createElement("input");
    input.type = "text";
    input.name = col.field;
    inputs[col.field] = input;
    wrap.appendChild(input);
    form.appendChild(wrap);
  });

  const submitBtn = document.createElement("button");
  submitBtn.type = "submit";
  submitBtn.textContent = "Save Row";
  form.appendChild(submitBtn);

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    const newRow = {};
    sheet.columns.forEach((col) => {
      newRow[col.field] = inputs[col.field].value;
    });
    const key = newRow[sheet.rowKey];
    if (!key) {
      alert(`${sheet.rowKey} is required.`);
      return;
    }
    if (sheet.rows.some((r) => r[sheet.rowKey] === key)) {
      alert(`A row with ${sheet.rowKey} = "${key}" already exists.`);
      return;
    }
    sheet.editableFields.forEach((f) => {
      if (!(f in newRow) || newRow[f] === "") newRow[f] = "";
    });
    // Row creation is environment/data setup, not a recorded business
    // action, so it is intentionally NOT logged as evidence.
    sheet.rows.push(newRow);
    addRowFormOpen = false;
    renderTable();
  });

  container.appendChild(form);
}

// The cell the operator is currently looking at. Held across re-renders by
// (rowKey, field) so a search or a tab switch does not silently drop it.
let selectedCell = null;

function selectCell(td, sheet, row, col, rowKey) {
  document
    .querySelectorAll("td.selected")
    .forEach((e) => e.classList.remove("selected"));
  td.classList.add("selected");
  selectedCell = { td, sheet, row, col, rowKey };
}

// Ctrl/Cmd+C on the selected cell records a READ_CELL and copies the value.
// "I am taking this value" is exactly what a READ means in the evidence, so
// the capture gesture and the business meaning line up.
function captureSelectedRead() {
  if (!selectedCell) return false;
  const { row, col, td } = selectedCell;
  const value = row[col.field];
  Logger.logReadCell(col.label, value, td);
  if (navigator.clipboard) {
    navigator.clipboard.writeText(String(value == null ? "" : value)).catch(() => {});
  }
  td.classList.add("just-read");
  setTimeout(() => td.classList.remove("just-read"), 400);
  renderLog();
  return true;
}

function onCopyKey(e) {
  const isCopy = (e.ctrlKey || e.metaKey) && (e.key === "c" || e.key === "C");
  if (!isCopy) return;
  // Never hijack an ordinary text copy, and never fire while a cell is open
  // for editing -- that path logs its own READ or WRITE on commit.
  if (document.querySelector("td input")) return;
  const selection = window.getSelection();
  if (selection && String(selection).length > 0) return;
  if (captureSelectedRead()) e.preventDefault();
}

function beginEdit(td, sheet, row, col, rowKey) {
  const currentValue = row[col.field];
  const input = document.createElement("input");
  input.type = "text";
  input.value = currentValue;
  td.textContent = "";
  td.appendChild(input);
  input.focus();
  input.select();

  let committed = false;
  // Whether this click becomes a READ or a WRITE is decided here: if the
  // value is unchanged it's a read (inspecting the cell); if it changed
  // it's a write (an actual edit).
  function commit() {
    if (committed) return;
    committed = true;
    const newValue = input.value;
    const changed = String(newValue) !== String(currentValue);
    td.textContent = newValue;
    if (changed) {
      row[col.field] = newValue;
      Logger.logWriteCell(col.label, newValue, td);
    } else {
      Logger.logReadCell(col.label, currentValue, td);
    }
    renderLog();
  }

  let cancelled = false;
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      commit();
    } else if (e.key === "Escape") {
      cancelled = true;
      committed = true; // suppress the blur-triggered commit below
      td.textContent = currentValue;
    }
  });
  input.addEventListener("blur", () => {
    if (!cancelled) commit();
  });
}

function onSearch() {
  const input = $("#search-box");
  const value = input.value.trim();
  const sheet = SEED_DATA[activeSheet];

  if (!value) {
    searchFilter = "";
    highlightedRowKey = null;
    renderTable();
    renderLog();
    return;
  }

  searchFilter = value;
  const exactMatch = sheet.rows.find(
    (r) => String(r[sheet.rowKey]).toLowerCase() === value.toLowerCase()
  );
  highlightedRowKey = exactMatch ? exactMatch[sheet.rowKey] : null;

  Logger.logSearch(value, input);
  renderTable();
  renderLog();

  const rowEl = document.querySelector(`tr[data-row-key="${highlightedRowKey}"]`);
  if (rowEl) rowEl.scrollIntoView({ behavior: "smooth", block: "center" });
}

function onClearSearch() {
  $("#search-box").value = "";
  searchFilter = "";
  highlightedRowKey = null;
  renderTable();
}

function renderLog() {
  const logEl = $("#event-log");
  const events = Logger.getEvents();
  logEl.textContent = JSON.stringify(events, null, 2);
  logEl.scrollTop = logEl.scrollHeight;
  $("#event-count").textContent = events.length;
}

function renderDemoId() {
  $("#demo-id").textContent = Logger.getDemoId();
}

// "New Demo": full fresh start — new demo id, clean sheet data, clean log.
function onNewDemo() {
  resetAllSheetData();
  Logger.reset();
  $("#narration").value = "";
  $("#search-box").value = "";
  activeSheet = SHEET_NAMES[0];
  highlightedRowKey = null;
  searchFilter = "";
  addRowFormOpen = false;
  renderTabs();
  renderTable();
  renderLog();
  renderDemoId();
}

// Export: save the current demo's evidence, then clear just the log/
// narration so the next recording starts clean. Sheet data and demo id
// are left untouched.
function onExport() {
  const narration = $("#narration").value;
  const evidence = Logger.exportEvidence(narration);
  const blob = new Blob([JSON.stringify(evidence, null, 2)], {
    type: "application/json",
  });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${evidence.demonstrationId}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);

  Logger.clearLog();
  $("#narration").value = "";
  renderLog();
}

// Record toggle: ON shows New Demo/SME Narration/Export/Event Log and
// enables event logging. OFF hides all of that and the sheet is just a
// plain viewer/editor — no evidence is captured.
function onToggleRecord(enabled) {
  Logger.setRecording(enabled);
  document.querySelectorAll(".record-only").forEach((el) => {
    el.classList.toggle("hidden", !enabled);
  });
}

function init() {
  renderTabs();
  renderTable();
  renderLog();
  renderDemoId();
  onToggleRecord(false);

  $("#search-btn").addEventListener("click", onSearch);
  $("#search-box").addEventListener("keydown", (e) => {
    if (e.key === "Enter") onSearch();
  });
  $("#clear-search-btn").addEventListener("click", onClearSearch);
  $("#export-btn").addEventListener("click", onExport);
  $("#new-demo-btn").addEventListener("click", onNewDemo);
  document.addEventListener("keydown", onCopyKey);
  $("#record-toggle").addEventListener("change", (e) =>
    onToggleRecord(e.target.checked)
  );
}

document.addEventListener("DOMContentLoaded", init);
