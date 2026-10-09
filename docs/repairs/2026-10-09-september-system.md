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

Credit 70838835146 reverses original charge 70083277162, shipment 47877948646, pack 2000014751989369, amount 14.45 BRL. The September 7 official August export row 121 and the August 31 shipment cache both show 14.45. Replaying all 201 historical non-cancelled FB07-7 rows with the historical production algorithm gives exactly the retained 2867.60 BRL shipping; removing the target gives 2853.15. That historical algorithm charged the full shipment per order. The later shared-package correction was applied to MX3, not this BR snapshot. Five mixed-SKU packages account for a separate historical 52.80 allocation issue; August remains read-only.

Only this exact credit/date/currency/original-charge/shipment/pack/amount combination may reduce September shipping expense. Other unproved BFFI credits remain blocked. Evidence: finance workspace `outputs/mercadolibre-september-system-20261009/BR-Aug-replay.json`, including historical commits, file hashes, 201 row mappings and cached fee timestamps. This proves the single credit's prior expense, not whole-month August A/B completion.

## September CBT field reconciliation

The September repair is bound to the three audited export hashes in `cbt_reconcile.py`. A changed export stops for renewed evidence; no September claim exception is silently reused for another month. Prior-month parsing and financial revisions are preserved. Further month validation is separate work.

- Four multi-unit rows store per-unit K while another already stores the line total. Each is resolved against Bill quantity and sale amount, not a blanket quantity multiplier.
- The blank-SKU package is allocated only to its two child orders. Child revenue/commission are observed; shared taxes/shipping use disclosed within-package revenue shares.
- Original cost quantity stays 459. Commercial net quantity is 436 and platform-inspected returns are 7, including two awaiting retrieval; these are not inventory restock facts.
- Product-sales refunds and settlement reversals are different measures. The adjustment explicitly bridges FX, refund/fee reclassification and source rounding, preserving every order's seller settlement. Buyer-protection exceptions use verified claim quantities, never mixed-currency payment amounts as USD.
- September return handling fees for August orders are attributed by listing to the corresponding two SKUs and added after the Orders settlement bridge; August is not rewritten.
- Existing source schema gains only `净销量` and `退货数量`. The 47-column main report is unchanged; only September's source sheet includes these fields. New null schema keys are ignored in pre-September approval hashes, but non-null changes remain bound. The production profit formula adds an exact month/shop/version branch, keeping the old formula as its fallback.
- Review generation rejects missing September CBT revision/rows or aggregate discrepancies before presenting verified figures. It never issues human approvals or writes the company index.

Independent verification: 453 orders × 13 fields matched the separately constructed financial bridge with zero differences. Full regression suite: 286 tests passed. Production source/cost/candidate completion is recorded separately in the finance workspace evidence, rather than inferred from these tests.

## Verification and recovery

Targeted tests reproduce monthly FX, native partial refund allocation, unchanged CBT approval, failed upstream costs, multi-page sources, cross-entry write exclusion, cancellation, cleanup failure and long-running claims. Production verification must additionally record deployment commit, complete workflow readback, previews, narrow seller/month backups, written/verified counts and report readback. Until those exist, implementation is not completion.

On failure: use the recorded source backup and job receipt; reconcile the month before releasing an interrupted claim. Restore only affected source rows or the three backed-up workflow definitions. Financial release gates remain unchanged.
