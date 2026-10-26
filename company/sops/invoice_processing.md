---
title: Vendor invoice processing
description: How Accounts Payable turns vendor invoices from the inbox into bills in the ERP, including duplicate checks, PO matching, exceptions and approvals.
triggers:
  - process the invoices
  - process vendor invoices
  - enter vendor bills
  - clear the AP inbox
  - book this week's invoices
owner: Finance / Accounts Payable
---

# Vendor invoice processing

Vendors email invoices to AP, and they are saved as PDFs in the **invoice inbox folder**
(`file_inbox` in the system directory). Every invoice in that folder must be handled.
Work through them one at a time.

## For each invoice

1. **Read the invoice.** Note the vendor name, invoice number, invoice date, PO number
   and the invoice **total**.

2. **Check for a duplicate first.** In the ERP, go to **Bills** and search for the invoice
   number. If a bill already exists for that invoice number **from the same vendor**,
   do not enter it again. Record it as a duplicate and note the existing bill number.
   Vendors often re-send old invoices; entering one twice means paying twice.

3. **Find the purchase order.** Open the PO named on the invoice (**Purchase Orders**,
   or go straight to the PO page). If the PO does not exist, do not create a bill.
   Log an AP exception with the reason `PO not found: <PO number>`. Bills can only be
   raised against an **Open** PO from the **same vendor**; if the PO is closed or belongs
   to another vendor, log an exception with the reason `PO not open` or `PO vendor mismatch`.
   Use exactly the PO number printed on the invoice: several POs can look alike.

4. **Match the amount.** Compare the invoice total with the PO total.
   - A difference of **$5.00 or less** is acceptable (rounding, small fees).
   - Anything larger: do not create a bill. Log an AP exception with the reason
     `Amount mismatch: invoice $<invoice total> vs PO $<PO total>`.

5. **Create the bill.** Go to **Bills → New Bill** and enter:
   - Vendor: the invoice's vendor
   - PO Number, Invoice Number and Invoice Date: as printed on the invoice
   - Amount: the **invoice total** (not the PO total)
   - GL Code: the vendor's GL code from the **Vendors** page

   After saving, note the new bill number shown on the confirmation page.

## Logging an exception

Use **Exceptions → New Exception**: choose the vendor, enter the invoice number, the PO
number if there is one, and the reason in the format above. The AP team reviews this
queue daily, so a clear reason saves a phone call.

## Approvals

- **FIN-3:** bills above **$10,000** need manager approval before they are created.
  If approval is denied, do not create the bill; record the invoice as not entered
  because approval was denied.

## Done when

Every invoice in the inbox has **exactly one** outcome, with its record noted:
- a bill was created (bill number), or
- an exception was logged (exception number and reason), or
- it was skipped as a duplicate (existing bill number), or
- it was not entered because an approval was denied.

## Common mistakes

- Entering the PO amount instead of the invoice amount.
- Skipping the duplicate check because the invoice "looks new".
- Creating a bill when the amounts don't match "because it's close": only $5.00 is allowed.
- Matching on vendor alone: vendors have several open POs, so always use the PO number.
