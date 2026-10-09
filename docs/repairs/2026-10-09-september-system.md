# September source recovery and review candidate

## Authorized boundary

User authorized September source repair, restoration of the existing automatic monthly jobs, cost checks and the existing 47-column **unapproved review candidate**. No colleague messages, approvals, operating/final publication, company index, payroll, new service or table.

## Corrections

- Monthly USD FX comes from the target month; identical CBT reruns do not invalidate approval.
- Existing monthly work claims and durable result files cover synchronous and background source/cost writes. Only a completed result advances the cost step. Interrupted writes keep their claim until actual data has been reconciled; do not blindly retry.
- The existing monthly, CBT and ninth-day cost workflows retain their triggers/connections. Target nodes submit asynchronous jobs, verify their final result and skip approved months.
- Billing uses sequential 25-second pacing, bounded 429 retries, exact remaining counts, duplicate/cursor checks and a one-hour complete-result cache. Both BILL and CREDIT_NOTE are read and merged by detail ID.
- Brazilian payment/receipt fees already in native order sale_fee are not charged twice. CFONPN/BFONPN buyer financing additions/reversals are excluded from seller cost; actual seller installment fees are retained.
- Native MXN partial refund 24.70 is preserved without treating paid/total as an exchange rate or inferring a whole physical return.
- Cost APIs must return valid complete sources. Writes retain a source backup, then read back values. Failed sources are not zero cost.

## Single-record September BR shipping credit

Credit 70838835146 reverses original charge 70083277162, shipment 47877948646, pack 2000014751989369, amount 14.45 BRL. The September 7 official August sales export row 121 shows this pack delivered and shipping cost 14.45. The August 31 shipment cache independently contains sender cost 14.45. The retained approved August FB07-7 source has 201 units and gross revenue 17625.65, exactly equal to the complete 201-row historical non-cancelled official SKU set that includes this pack. Historical month-scope reconciliation recorded zero missing/extra non-cancelled packs; the aggregation charged each cached shipment once. This proves inclusion in that retained source without rewriting August.

Only this exact credit/date/currency/original-charge/shipment/pack/amount combination may reduce September shipping expense. Other unproved BFFI credits remain blocked. Evidence: finance workspace `outputs/mercadolibre-september-system-20261009/BR-prior-charge-proof.json` with source file SHA256 and all 201 financial rows. The SKU's total official-vs-API shipping difference is 10.68 BRL; this record-level correction does not certify whole-month August A/B reconciliation.

## Verification and recovery

Targeted tests reproduce monthly FX, native partial refund allocation, unchanged CBT approval, failed upstream costs, multi-page sources, cross-entry write exclusion, cancellation, cleanup failure and long-running claims. Production verification must additionally record deployment commit, complete workflow readback, previews, narrow seller/month backups, written/verified counts and report readback. Until those exist, implementation is not completion.

On failure: use the recorded source backup and job receipt; reconcile the month before releasing an interrupted claim. Restore only affected source rows or the three backed-up workflow definitions. Financial release gates remain unchanged.
