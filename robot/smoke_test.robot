*** Settings ***
Documentation    Manual smoke test for library.resource — not the generated
...              automation itself. Exercises each keyword against the real
...              app to prove the Layer 2 library actually works before any
...              LLM-generated Layer 1 task script depends on it.
Resource    library.resource
Suite Setup       Open Sheet Application
Suite Teardown    Close Sheet Application


*** Test Cases ***
Can Locate And Read Invoice Fields
    Locate Invoice By ID    INV-93821
    ${po}=      Read Invoice Field    INV-93821    poNumber
    ${price}=   Read Invoice Field    INV-93821    unitPrice
    Should Be Equal As Strings    ${po}       PO-4500123
    Should Be Equal As Strings    ${price}    101.5

Can Locate And Read Purchase Order Fields
    ${po_price}=    Read Purchase Order Field    PO-4500123    unitPrice
    ${po_qty}=      Read Purchase Order Field    PO-4500123    orderedQty
    Should Be Equal As Strings    ${po_price}    100
    Should Be Equal As Strings    ${po_qty}      100

Can Locate And Read Goods Receipt Fields
    ${received}=    Read Goods Receipt Field    PO-4500198    receivedQty
    Should Be Equal As Strings    ${received}    80

Can Detect A Missing Purchase Order
    ${exists}=    Purchase Order Exists    PO-9999999
    Should Be Equal As Strings    ${exists}    False

Can Detect An Existing Purchase Order
    ${exists}=    Purchase Order Exists    PO-4500123
    Should Be Equal As Strings    ${exists}    True

Can Update Invoice Status And Read It Back
    Update Invoice Status    INV-93821    RELEASED
    ${status}=    Read Invoice Field    INV-93821    status
    Should Be Equal As Strings    ${status}    RELEASED

Can Update Invoice Resolution
    Update Invoice Resolution    INV-93821    Price within tolerance
    ${resolution}=    Read Invoice Field    INV-93821    resolution
    Should Be Equal As Strings    ${resolution}    Price within tolerance
