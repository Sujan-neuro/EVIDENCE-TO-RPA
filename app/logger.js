// Evidence logger: captures interaction events + DOM/accessibility metadata
// in the exact shape described in the brief (§5-§8).
//
// Combined evidence document shape (per demonstration):
// {
//   demonstrationId: "DEMO-A",
//   narration: { narrationId: "NARRATION-A", text: "..." },
//   events: [
//     { eventId, timestamp, action, sheet?, value?, column?, element? }
//   ]
// }

const Logger = (() => {
  // The sheet shown before any tab is clicked. Every recording opens here, so
  // an event logged before the first SELECT_SHEET still reports truthfully.
  const DEFAULT_SHEET = "Invoices";

  let eventCounter = 0;
  let demoNumber = 1;
  let recordingEnabled = false;

  // The sheet every event happened on. Tracked here so that EVERY event can
  // state it, rather than only SELECT_SHEET. Without it a READ_CELL says which
  // column it touched but not which sheet, and "Unit Price" exists on both
  // Invoices and Purchase Orders -- so the consumer has to replay the whole
  // trace to work out which entity was read. Evidence should not need to be
  // inferred to be understood.
  let currentSheet = DEFAULT_SHEET;
  let events = [];

  function nextEventId() {
    eventCounter += 1;
    return `E${String(eventCounter).padStart(3, "0")}`;
  }

  function nowTimestamp() {
    const d = new Date();
    const hh = String(d.getHours()).padStart(2, "0");
    const mm = String(d.getMinutes()).padStart(2, "0");
    const ss = String(d.getSeconds()).padStart(2, "0");
    const ms = String(d.getMilliseconds()).padStart(3, "0");
    return `${hh}:${mm}:${ss}.${ms}`;
  }

  // Build a simple, deterministic CSS selector for an element using
  // semantic attributes first, falling back to nth-child.
  // A selector built from what an element MEANS, when the element says so.
  // Returns null when nothing stable is available, so the caller can fall back
  // to a positional path and the difference stays visible.
  function stableSelector(el) {
    if (!el || !el.dataset) return null;
    if (el.dataset.rowKey && el.dataset.field) {
      return `[data-row-key="${el.dataset.rowKey}"] [data-field="${el.dataset.field}"]`;
    }
    if (el.dataset.sheet) {
      return `[data-sheet="${el.dataset.sheet}"]`;
    }
    return null;
  }

  function cssSelector(el) {
    if (!el) return "";
    // Prefer meaning over position. `nav#tabs > button:nth-of-type(2)` names the
    // second tab, which stops being the Purchase Orders tab the moment a tab is
    // added or reordered; `[data-sheet="Purchase Orders"]` cannot. These
    // selectors are carried into the IR and end up as locators in generated
    // automation, so a positional one here becomes a brittle robot later.
    const stable = stableSelector(el);
    if (stable) return stable;
    const parts = [];
    let node = el;
    while (node && node.nodeType === 1 && parts.length < 5) {
      let part = node.tagName.toLowerCase();
      if (node.id) {
        part += `#${node.id}`;
        parts.unshift(part);
        break;
      }
      const parent = node.parentElement;
      if (parent) {
        const siblings = Array.from(parent.children).filter(
          (c) => c.tagName === node.tagName
        );
        if (siblings.length > 1) {
          part += `:nth-of-type(${siblings.indexOf(node) + 1})`;
        }
      }
      parts.unshift(part);
      node = parent;
    }
    return parts.join(" > ");
  }

  function xpathFor(el) {
    if (!el) return "";
    // Same preference as the CSS selector: an attribute predicate survives the
    // DOM being rearranged, an index path does not.
    if (el.dataset && el.dataset.sheet) {
      return `//${el.tagName.toLowerCase()}[@data-sheet="${el.dataset.sheet}"]`;
    }
    if (el.dataset && el.dataset.rowKey && el.dataset.field) {
      return `//tr[@data-row-key="${el.dataset.rowKey}"]` +
             `/td[@data-field="${el.dataset.field}"]`;
    }
    const segments = [];
    let node = el;
    while (node && node.nodeType === 1) {
      let index = 1;
      let sibling = node.previousElementSibling;
      while (sibling) {
        if (sibling.tagName === node.tagName) index += 1;
        sibling = sibling.previousElementSibling;
      }
      segments.unshift(`${node.tagName.toLowerCase()}[${index}]`);
      node = node.parentElement;
    }
    return "/" + segments.join("/");
  }

  function accessibilityPathFor(el) {
    if (!el) return "";
    const roles = [];
    let node = el;
    while (node && node.nodeType === 1) {
      const role = node.getAttribute("role");
      if (role) roles.unshift(role);
      node = node.parentElement;
    }
    return roles.join(" > ");
  }

  // Text of sibling cells in the same row, used to disambiguate a generic
  // column name like "Status" (mirrors §8's nearbyText example).
  function nearbyTextFor(el) {
    if (!el) return [];
    const row = el.closest("[data-row-key]");
    if (!row) return [];
    return Array.from(row.querySelectorAll("[data-field]"))
      .map((c) => c.textContent.trim())
      .filter(Boolean)
      .slice(0, 4);
  }

  function rowColumnIndex(el) {
    if (!el) return { row: null, column: null };
    const rowEl = el.closest("tr");
    const table = rowEl ? rowEl.closest("table") : null;
    let row = null;
    let column = null;
    if (table && rowEl) {
      row = Array.from(table.querySelectorAll("tr")).indexOf(rowEl);
    }
    const cellEl = el.closest("td,th");
    if (cellEl && rowEl) {
      column = Array.from(rowEl.children).indexOf(cellEl);
    }
    return { row, column };
  }

  // Captures the §8-style "element" metadata block for a given DOM node.
  function captureElementMetadata(el) {
    if (!el) return null;
    const { row, column } = rowColumnIndex(el);
    return {
      role: el.getAttribute("role") || null,
      name: el.dataset ? el.dataset.field || null : null,
      text: el.textContent ? el.textContent.trim() : "",
      row,
      column,
      css: cssSelector(el),
      xpath: xpathFor(el),
      accessibilityPath: accessibilityPathFor(el),
      nearbyText: nearbyTextFor(el),
    };
  }

  function logEvent(action, payload, el) {
    if (!recordingEnabled) return null;
    const event = {
      eventId: nextEventId(),
      timestamp: nowTimestamp(),
      action,
      sheet: currentSheet,
      ...payload,
      element: captureElementMetadata(el),
    };
    events.push(event);
    if (typeof window !== "undefined" && window.onEvidenceEvent) {
      window.onEvidenceEvent(event);
    }
    return event;
  }

  function setRecording(enabled) {
    recordingEnabled = !!enabled;
  }

  function isRecording() {
    return recordingEnabled;
  }

  function logSelectSheet(sheet, el) {
    // Set BEFORE logging, so the navigation event reports the sheet being
    // moved to rather than the one being left.
    currentSheet = sheet;
    return logEvent("SELECT_SHEET", { sheet }, el);
  }

  function logSearch(value, el) {
    return logEvent("SEARCH", { value }, el);
  }

  function logReadCell(column, value, el) {
    return logEvent("READ_CELL", { column, value }, el);
  }

  function logWriteCell(column, value, el) {
    return logEvent("WRITE_CELL", { column, value }, el);
  }

  // Starts a brand new demonstration: bumps the demo number and clears
  // the event log. Used by the "New Demo" button.
  function reset() {
    demoNumber += 1;
    events = [];
    currentSheet = DEFAULT_SHEET;
  }

  // Clears only the event log for the current demo (used right after an
  // export) without advancing the demo number or touching sheet data.
  function clearLog() {
    events = [];
  }

  function getEvents() {
    return events;
  }

  function getDemoId() {
    return `DEMO-${demoNumber}`;
  }

  function exportEvidence(narrationText) {
    return {
      demonstrationId: getDemoId(),
      narration: {
        narrationId: `NARRATION-${demoNumber}`,
        text: narrationText || "",
      },
      events,
    };
  }

  return {
    logSelectSheet,
    logSearch,
    logReadCell,
    logWriteCell,
    reset,
    clearLog,
    getEvents,
    getDemoId,
    exportEvidence,
    setRecording,
    isRecording,
  };
})();
