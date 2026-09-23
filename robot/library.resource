*** Settings ***
Documentation    Layer 2 keyword library: hand-written, deterministic wrappers
...              around Browser (Playwright) primitives, targeting the sheet
...              app's semantic markup (data-row-key / data-field, and
...              data-label for the header a person reads). The LLM
...              never sees this file's internals — it only calls these
...              keyword names with business-level arguments (e.g. an
...              Invoice ID), never a selector, xpath, or coordinate.
Library    Browser
Library    String


*** Variables ***
${BASE_URL}         http://localhost:8811
${DEFAULT_TIMEOUT}  5s
# Override on the command line to watch the run:
#   --variable HEADLESS:False --variable SLOWMO:0.4s
${HEADLESS}         ${True}
${SLOWMO}           0s


*** Keywords ***
# ------------------------------------------------------------------
# Application lifecycle
# ------------------------------------------------------------------

Open Sheet Application
    [Documentation]    Launches a fresh browser + page against the sheet app.
    ...                Recording must stay OFF for automation runs — the
    ...                app's evidence logger is only for human demonstrations.
    ...
    ...                Headless by default. To watch the automation drive a
    ...                real browser window:
    ...                    robot --variable HEADLESS:False --variable SLOWMO:0.4s ...
    New Browser    chromium    headless=${HEADLESS}    slowMo=${SLOWMO}
    New Page    ${BASE_URL}

Close Sheet Application
    [Documentation]    Tears down the browser session.
    Close Browser    ALL


# ------------------------------------------------------------------
# Navigation / locate primitives
# ------------------------------------------------------------------

Select Sheet
    [Documentation]    Switches the active tab to the named sheet.
    [Arguments]    ${sheet_name}
    Click    css=.tab[data-sheet="${sheet_name}"]

Search For
    [Documentation]    Types a business key into the search box and submits it.
    [Arguments]    ${value}
    Fill Text    css=#search-box    ${value}
    Click    css=#search-btn

Row Exists
    [Documentation]    True if a row with this business key is present in
    ...                the DOM (after a search has been performed on the
    ...                right sheet). Used to detect a missing PO / invoice
    ...                without raising a hard error.
    ...                Scoped to `tr` specifically: every cell in a row also
    ...                carries data-row-key (needed for the cell-level
    ...                selector `[data-row-key][data-field]`), so an
    ...                unscoped `[data-row-key="X"]` matches the row AND all
    ...                of its cells and trips Playwright's strict mode.
    [Arguments]    ${row_key}
    ${count}=    Get Element Count    css=tr[data-row-key="${row_key}"]
    RETURN    ${{$count > 0}}

Locate Row By Id
    [Documentation]    Selects the given sheet, searches for the business
    ...                key, and waits for the matching row to render.
    ...                Fails loudly if the row never appears — callers that
    ...                expect a possibly-missing row should call
    ...                `Row Exists` first instead.
    [Arguments]    ${sheet_name}    ${row_key}
    Select Sheet    ${sheet_name}
    Search For    ${row_key}
    Wait For Elements State    css=tr[data-row-key="${row_key}"]    visible    timeout=${DEFAULT_TIMEOUT}


# ------------------------------------------------------------------
# Business-entity locate wrappers
# ------------------------------------------------------------------

Locate Invoice By ID
    [Arguments]    ${invoice_id}
    Locate Row By Id    Invoices    ${invoice_id}

Locate Purchase Order By Number
    [Arguments]    ${po_number}
    Locate Row By Id    Purchase Orders    ${po_number}

Locate Goods Receipt By PO Number
    [Arguments]    ${po_number}
    Locate Row By Id    Goods Receipts    ${po_number}

Purchase Order Exists
    [Documentation]    Non-throwing existence check, for the missing-PO
    ...                (MANUAL_REVIEW) scenario.
    [Arguments]    ${po_number}
    Select Sheet    Purchase Orders
    Search For    ${po_number}
    ${exists}=    Row Exists    ${po_number}
    RETURN    ${exists}


# ------------------------------------------------------------------
# Read primitives
# ------------------------------------------------------------------

Read Cell Value
    [Documentation]    Reads a cell's current text directly from the DOM.
    ...                Deliberately does NOT click the cell — clicking opens
    ...                the edit input (see Write Cell Value) and is only
    ...                needed for writes.
    [Arguments]    ${row_key}    ${field}
    ${value}=    Get Text    css=[data-row-key="${row_key}"] [data-field="${field}"], [data-row-key="${row_key}"] [data-label="${field}"]
    RETURN    ${value}

Read Invoice Field
    [Arguments]    ${invoice_id}    ${field}
    Locate Invoice By ID    ${invoice_id}
    ${value}=    Read Cell Value    ${invoice_id}    ${field}
    RETURN    ${value}

Read Purchase Order Field
    [Arguments]    ${po_number}    ${field}
    Locate Purchase Order By Number    ${po_number}
    ${value}=    Read Cell Value    ${po_number}    ${field}
    RETURN    ${value}

Read Goods Receipt Field
    [Arguments]    ${po_number}    ${field}
    Locate Goods Receipt By PO Number    ${po_number}
    ${value}=    Read Cell Value    ${po_number}    ${field}
    RETURN    ${value}

Locate Processing Rule By Name
    [Arguments]    ${rule_name}
    Locate Row By Id    Processing Rules    ${rule_name}

Read Processing Rule Value
    [Documentation]    Reads a named rule's threshold live from the
    ...                Processing Rules sheet, so a generated script never
    ...                hardcodes a tolerance like "2%" (Part 9 robustness).
    ...                Strips a trailing "%" so percentage rules come back
    ...                as a plain number (e.g. "2%" -> 2), matching the
    ...                IR Variable's declared numeric dataType.
    [Arguments]    ${rule_name}
    Locate Processing Rule By Name    ${rule_name}
    ${value}=    Read Cell Value    ${rule_name}    value
    ${value}=    Remove String    ${value}    %
    RETURN    ${value}


# ------------------------------------------------------------------
# Write primitives
# ------------------------------------------------------------------

Write Cell Value
    [Documentation]    Double-clicks a cell to open its edit input, fills the
    ...                new value, and commits by blurring.
    ...
    ...                Double-click, not click: the application follows the
    ...                spreadsheet convention where a single click SELECTS and a
    ...                double click EDITS. A single click here opens nothing, so
    ...                the fill times out waiting for an input that never
    ...                appears. The commit is a blur rather than Enter because
    ...                the edit input commits on losing focus.
    [Arguments]    ${row_key}    ${field}    ${value}
    ${cell}=    Set Variable    css=[data-row-key="${row_key}"] [data-field="${field}"], [data-row-key="${row_key}"] [data-label="${field}"]
    Click With Options    ${cell}    left    clickCount=2
    Fill Text    ${cell} >> css=input    ${value}
    Click    css=h1

Update Invoice Status
    [Arguments]    ${invoice_id}    ${status}
    Locate Invoice By ID    ${invoice_id}
    Write Cell Value    ${invoice_id}    status    ${status}

Update Invoice Resolution
    [Arguments]    ${invoice_id}    ${resolution}
    Locate Invoice By ID    ${invoice_id}
    Write Cell Value    ${invoice_id}    resolution    ${resolution}
