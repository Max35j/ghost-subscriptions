---
name: monthly-audit
description: Run a subscription audit from a bank statement export with the ghost-subscriptions tools, read the confidence scores, and tell the user what the numbers can and cannot prove.
---

# Monthly subscription audit

Use this when the user wants to know what they pay for every month, wants to cut costs, or asks you to
check their bank statement for subscriptions. Everything stays on this computer: the tools read the file
the user points to and write only into the plugin's own data folder.

## 1. Get a statement

Ask for a CSV or OFX export covering **at least 12 months** (13 is better: it shows each monthly charge
twelve times and the gap before the next one). Most banks offer it under "Download transactions",
"Export", "Télécharger les opérations" or "Money / Quicken (OFX)". A PDF statement does not work.

Call `statement_import` with the path. Read back to the user:

- the bank profile used and the period covered,
- rows read, skipped (balance lines, pending card payments) and rejected (with the reason),
- any warning, for example card numbers that were removed.

If the result says the format was not recognised, show the columns it found and ask which ones hold the
date, the description and the amount. The user sets them in the plugin's `column_mapping` setting; then call
`statement_import` again with `profile="custom"`.

For a monthly routine, import the new month's export each time: overlapping exports are merged without
duplicates.

## 2. Run the audit

Call `statement_audit`. For an export that ended a while ago, pass `as_of` = the statement's last day,
otherwise every subscription looks cancelled.

Report, in this order:

1. **Total**: yearly and monthly cost of active subscriptions, per currency.
2. **The ones they probably forgot**: active items with a small amount or a merchant they did not
   mention. Ask "do you still use this?" rather than asserting it is useless.
3. **Anomalies**, most expensive first:
   - `duplicate`: charged twice within a few days. Suggest asking the merchant for a refund.
   - `price_increase`: show old price, new price, date and yearly impact.
   - `overlap`: two services of the same kind (two video platforms, two cloud plans). This is a choice,
     not an error: present it as such.
   - `disappeared`: no charge since the expected date. Probably cancelled; already left out of the total.
   - `installment`: a payment plan, not a subscription.
4. **Low-confidence items and leads**: list them as "to check", never as confirmed subscriptions.

Answer follow-up questions (price increases, duplicates, what stopped) from the audit result you already
have: the `anomalies` list carries the old and new price, the date and the yearly impact. Do not re-parse
the raw file to recompute them. Quote dates exactly as returned (`YYYY-MM-DD`) so day and month cannot be
confused.

Offer `statement_cancel_pack` for the ones the user wants to stop. Its reminders always target the next
charge still ahead of today; read its `warnings` back to the user.

## 3. Read the confidence

`confidence` = 0.4 x (occurrences, capped at 6, / 6) + 0.4 x interval regularity + 0.2 x amount
regularity, reduced by 10 % when the merchant is not in the alias table.

- **0.90 and above**: a regular charge seen many times. Safe to call it a subscription.
- **0.60 to 0.90**: probably recurring; dates or amounts move a little. Say "looks like".
- **Below the threshold** (0.6 by default): only in `low_confidence`. Ask the user.
- **Two charges** only: a `lead`. Never count it in the total.

`amount_kind: variable` means a bill whose amount changes (energy, water, phone usage). Its yearly cost is
an average, and no price-increase check runs on it. Say so.

## 4. Limits to state plainly

- Yearly subscriptions need three charges, so over two years of history, to be confirmed.
- A subscription paid by another card, account or PayPal balance does not show on this statement.
- Merchant names come from bank descriptions; an unknown merchant keeps a cleaned-up name and a lower
  confidence.
- Currencies are never converted unless the user set `fx_rates`.
- Notice periods and addresses in the letters are generic: the user must check their contract and fill in
  the bracketed fields.
- Nothing is cancelled or sent by these tools. The user sends the letters.

## 5. Privacy

Imported data (dates, amounts, cleaned descriptions with IBANs masked and card numbers removed) lives
in the plugin data folder. The file path is not stored. When the user asks to delete it, confirm, then call
`statement_purge` with `confirm=true`.
