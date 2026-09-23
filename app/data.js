// Seed data for the dummy spreadsheet app.
// Columns/fields intentionally mirror the brief's §3 spreadsheet structure.

const SEED_DATA = {
  Invoices: {
    columns: [
      { field: "invoiceId", label: "Invoice ID" },
      { field: "poNumber", label: "PO Number" },
      { field: "supplier", label: "Supplier" },
      { field: "invoiceQty", label: "Invoice Qty" },
      { field: "unitPrice", label: "Unit Price" },
      { field: "blockReason", label: "Block Reason" },
      { field: "status", label: "Status" },
      { field: "resolution", label: "Resolution" },
    ],
    rowKey: "invoiceId",
    editableFields: ["status", "resolution"],
    rows: [
      {
        invoiceId: "INV-93821",
        poNumber: "PO-4500123",
        supplier: "Acme Ltd",
        invoiceQty: 100,
        unitPrice: 101.5,
        blockReason: "PRICE_DIFFERENCE",
        status: "BLOCKED",
        resolution: "",
      },
      {
        invoiceId: "INV-94117",
        poNumber: "PO-4500198",
        supplier: "Global Parts",
        invoiceQty: 100,
        unitPrice: 50.0,
        blockReason: "QUANTITY_MISMATCH",
        status: "BLOCKED",
        resolution: "",
      },
      {
        invoiceId: "INV-95500",
        poNumber: "PO-4500300",
        supplier: "Beta Corp",
        invoiceQty: 100,
        unitPrice: 100.0,
        blockReason: "",
        status: "BLOCKED",
        resolution: "",
      },
      {
        invoiceId: "INV-95600",
        poNumber: "PO-4500301",
        supplier: "Gamma Inc",
        invoiceQty: 100,
        unitPrice: 110.0,
        blockReason: "PRICE_DIFFERENCE",
        status: "BLOCKED",
        resolution: "",
      },
      {
        invoiceId: "INV-96000",
        poNumber: "PO-4500400",
        supplier: "Epsilon Heavy",
        invoiceQty: 100,
        unitPrice: 3051.0,
        blockReason: "PRICE_DIFFERENCE",
        status: "BLOCKED",
        resolution: "",
      },
      {
        invoiceId: "INV-95700",
        poNumber: "PO-9999999",
        supplier: "Delta LLC",
        invoiceQty: 100,
        unitPrice: 100.0,
        blockReason: "MISSING_PO",
        status: "BLOCKED",
        resolution: "",
      },
    ],
  },

  "Purchase Orders": {
    columns: [
      { field: "poNumber", label: "PO Number" },
      { field: "orderedQty", label: "Ordered Qty" },
      { field: "unitPrice", label: "Unit Price" },
    ],
    rowKey: "poNumber",
    editableFields: [],
    rows: [
      { poNumber: "PO-4500123", orderedQty: 100, unitPrice: 100.0 },
      { poNumber: "PO-4500198", orderedQty: 100, unitPrice: 50.0 },
      { poNumber: "PO-4500300", orderedQty: 100, unitPrice: 100.0 },
      { poNumber: "PO-4500301", orderedQty: 100, unitPrice: 100.0 },
      { poNumber: "PO-4500400", orderedQty: 100, unitPrice: 3000.0 },
    ],
  },

  "Goods Receipts": {
    columns: [
      { field: "poNumber", label: "PO Number" },
      { field: "receivedQty", label: "Received Qty" },
    ],
    rowKey: "poNumber",
    editableFields: [],
    rows: [
      { poNumber: "PO-4500123", receivedQty: 100 },
      { poNumber: "PO-4500198", receivedQty: 80 },
      { poNumber: "PO-4500300", receivedQty: 100 },
      { poNumber: "PO-4500301", receivedQty: 100 },
      { poNumber: "PO-4500400", receivedQty: 100 },
    ],
  },

  "Processing Rules": {
    columns: [
      { field: "rule", label: "Rule" },
      { field: "value", label: "Value" },
    ],
    rowKey: "rule",
    editableFields: [],
    rows: [
      { rule: "Maximum Price Variance", value: "2%" },
      { rule: "Maximum Absolute Difference", value: "5000" },
      {
        rule: "Quantity Requirement",
        value: "Invoice quantity must not exceed received quantity",
      },
    ],
  },
};

const SHEET_NAMES = Object.keys(SEED_DATA);

// Deep snapshot of the original seed rows, so a "New Demo" can restore a
// clean environment even after edits/added rows from a previous session.
const ORIGINAL_ROWS = SHEET_NAMES.reduce((acc, name) => {
  acc[name] = JSON.parse(JSON.stringify(SEED_DATA[name].rows));
  return acc;
}, {});

function resetAllSheetData() {
  SHEET_NAMES.forEach((name) => {
    SEED_DATA[name].rows = JSON.parse(JSON.stringify(ORIGINAL_ROWS[name]));
  });
}
