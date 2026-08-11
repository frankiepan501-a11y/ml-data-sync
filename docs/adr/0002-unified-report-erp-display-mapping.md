# ADR 0002: Unified ML report uses strict ERP display mapping

## Status

Accepted — 2026-08-11, after finance approved the 2026-07 report format.

## Decision

The monthly-close finance-confirm action generates the standardized 47-column Feishu spreadsheet before writing `财务已确认终稿`.

`中文名称` and `分类` are resolved with exact normalized keys in this order:

1. `产品信息维护表.ERP SKU` → `ERP品名` + `产品类型` (fallback `三级分类`)
2. `产品采购成本台.ERP SKU` → `ERP品名` + `三级分类` (fallback `二级分类`)
3. `产品采购成本台.分销报价单SKU（Model No. ）` → the same display fields

The first tier containing the SKU is authoritative. A blank name/category or conflicting value in that tier blocks the report; the generator does not silently fall through. Purchase cost remains the value already calculated in the monthly production Base by ERP SKU. Product name and category are never cost join keys.

The approved 2026-07 wiki spreadsheet is copied as the visual template. The generator replaces all three sheets (`美客多毛利报表-YYYY-MM`, `数据源`, `检查`) and reads back every main/formula/source/check value. Only after those checks pass does it store `ML_UNIFIED_REPORT_V1|COMPLETE|...` in `检查!E1`.

SQLite keeps one durable generation owner per month, so concurrent service processes cannot copy duplicate reports. The marker contains a hash of report inputs and ERP display mappings. An unchanged retry returns the existing report; changed source data or mapping refreshes that generated report in place. An unmarked same-title file is not overwritten.

Advertising-fetch failures publish their persistent gate before waiting on the local month lock and cancel an active generation claim. Reject/recalculate actions also cancel the claim. Finance finalization rechecks the current Feishu state, ordered action record, in-process action epoch, advertising gates, and the durable generation record before writing `财务已确认终稿`.

Every state-changing Feishu callback also has a durable, ordered SQLite action record. Completed action keys stay deduplicated even after later cards are processed; failed action keys can be reclaimed by the same card unless a newer decision superseded them. Known stale cards are rejected before claiming an action sequence; if a post-claim race detects a stale card before any business write, that invalid claim is discarded. Only the latest valid action for a month can publish its final state, and an unhandled exception returns its claim to `failed` immediately. Feishu's last-action field is audit output, not the concurrency authority; it is written together with the final state inside the SQLite guard. Cost recalculation has a separate durable work-in-progress record: confirmation is blocked for the full row-write interval, and a newer reject prevents the old recalculation from committing close status or sending its result card. The current card message ID and finance-final state provide additional stale-card guards.

## Consequences

- Finance receives the same format every month without manual product-field filling.
- Bad or incomplete ERP master data is visible as a blocking error instead of an empty cell or zero.
- A failure or reject may leave an `IN_PROGRESS` report, but it cannot be promoted to the finance final state. A clean retry resumes the same generated report instead of creating a duplicate.
- The report generator needs read access to the product Base through the configured product-data Feishu App and copy/write access to the finance wiki through the reporting App.
