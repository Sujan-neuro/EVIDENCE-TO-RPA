# Execution-correctness harness. Hand-written, NOT generated -- it is copied
# beside each generated script so the script can be run and its result read
# back in one browser session, which is what makes the check a post-condition
# rather than a claim.
*** Settings ***
Resource    invoice_review.resource
Suite Setup    Open Sheet Application
Suite Teardown    Close Sheet Application

*** Test Cases ***
Verify Outcome
    [Documentation]    Runs the generated process then reads the resulting
    ...                status back, all in one browser session, to prove
    ...                the write actually landed correctly. Run with:
    ...                robot --variable INVOICE_ID:<id> --variable EXPECTED_STATUS:<status> verify_outcome.robot
    Process Invoice Exception Review    ${INVOICE_ID}
    ${status}=    Read Invoice Field    ${INVOICE_ID}    resolution
    Log To Console    RESULT: ${INVOICE_ID} -> ${status}
    Should Be Equal As Strings    ${status}    ${EXPECTED_STATUS}
