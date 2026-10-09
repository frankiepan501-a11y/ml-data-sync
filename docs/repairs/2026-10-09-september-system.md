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

## Production checkpoint — 2026-10-09 15:01 UTC+8

- Production `94e85f0d09444544056b7fbd76bceefa56353c5b`, deployment `6ac88daa353b26457a9043e1` RUNNING. MX3 37/37, BR12/12 and corrected CBT17/17 written/read back. CBT original17 rows retained; identical rerun created0/archived0/verified17.
- Existing three n8n definitions were read back with nodes/connections/settings/active preserved. Direct business jobs completed. The next natural full workflow execution has not been observed; no full workflow was triggered to avoid colleague cards.
- Cost commit `df525b9c3e0f46488e996ce96b6a9906` wrote/verified57 merchandise rows and retained `/data/cost-backups/4333af1912194cff8a4a2420dd298dcb.json`. Eight zero-unit system fee rows need no product cost. BR FF05-2, 23units, remains missing.
- **Third authorized outcome is blocked, not complete.** Logistics row1414 explicitly links FF05-2 to FF05A-01, but shipment10006769 has a USD quote, amount2619 and unit cost around3.73 with direct allocation formulas and no observed FX conversion. It also contributes to September BR TZ06/TZ07 costs already written by the existing algorithm. Those costs need rechecking; write/readback success is not financial verification. Four other FF05A-01 shipment rows202601-KT-54..57 lack actual shipment/arrival dates and are marked planned customs declaration.
- Minimum missing facts: currency and RMB conversion proof for10006769; actual shipment/cancellation status of those four batches. The user has been asked. Do not enable the alias and accept all-history averages without this evidence, substitute Mexican rates, write zero, or bypass the report gate.
- 47-column preview `3d0f335bcff54179a2a93b4fef97ec99` succeeded with mapping issues0 and arithmetic checks passing. Actual review commit was rejected409 before any candidate creation. Close status refreshed/read back as `成本缺失待补`; all release flags false. Company September ML index link empty, prior-month fields unchanged, August approval hash unchanged.
- Resume only affected BR costs after evidence, preserve backup/readback, rerun audit and create the existing unapproved47-column candidate. Do not repair August or build a new cost platform.
- Business handoff: `D:/Documents/财务与资产/outputs/mercadolibre-september-system-20261009/执行结果与剩余阻塞.md`.

## 16:20 成本证据补齐后的最小纠偏（上线待验证）

- 用户明确授权成本纠偏。梁俊辉群原信 15:53/16:03 及附件已归档到财务工作区 evidence；四箱截至9/30在途，1/14已发，准备退运不等于已退款。
- 撤回前轮日期空值推定未发货：原Sheet合并C1911:C1968，锚点46036=2026-01-14。当前成本引擎Z列从未按到仓日期过滤，故无需加入新到仓门槛或改物流源表。
- 三沐对账单 `2026.2月堃铎海运对账单(2).xlsx` SHA256 fd99a452ef21a1f9817ee5bc580979439bfc94f43b67b11ffa67c8f6597f8051；92箱清单第11行包含KT-02～12与54～57，15箱450件；(98257.13593+450四舍五入98707.14)×1.298745/9.37554132/450=30.38530438197674 RMB，与Z一致。账单2/5发货及预计2/13开船是不同物流节点，保留俊辉确认的1/14源日期。
- 神龙行10006769：俊辉线程关联24箱账单202607300078/客户运单SLX0260508-J；2619 USD×6.9062=18087.3378→18087.34 CNY。读侧按原逐箱Z权重换成账单人民币总额，保留原USD源值，不套9月销售汇率；24箱ID/产品/数量/总额变动阻止计算，已换人民币不再换汇。
- 巴西限定成本别名FF05-2→FF05A-01（原表1414行标签和ERP列同一行），Mexico及采购SKU别名不改。
- 影响仅BR FF05-2、TZ06、TZ07成本；继续原历史Z按数量均价，不新增成本平台/表/到仓门槛，不写8月。9月成本真写前备份、写后逐行核验，再生成原47列review V4候选。审批/正式索引仍关闭。
