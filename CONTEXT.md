# ML Direct Sync context

## Purpose

`ml-data-sync` turns Mercado Libre orders, advertising metrics, product costs, exchange rates, shipping costs, and refunds into a monthly Feishu gross-profit report.

## Core terms

- **Seller/month sync**: replaces Feishu rows for one Mercado Libre seller and one `month_YYYY-MM` period.
- **Monthly close**: audits the combined three-store report and controls operations/finance confirmation cards.
- **Advertising result**: item-level ad metrics returned by Mercado Libre. Duplicate transport rows must be collapsed by the logical ad identity `item_id + campaign_id + ad_group_id`.
- **Unknown vs zero**: an API failure is unknown and must block the write; zero is accepted only after a successful API response.
- **Unified finance report**: the finance-approved 47-column spreadsheet generated from the monthly Base after finance confirmation, with `数据源` and `检查` sheets retained for traceability.
- **ERP display mapping**: `中文名称` and `分类` are presentation fields resolved by exact ERP SKU tiers; they never replace ERP SKU as the purchase-cost join key.

## Critical modules

- `app/advertising.py`: Mercado Libre Ads fetch, validation, deduplication, and SKU attribution.
- `app/main.py`: seller/month aggregation and Feishu replacement write.
- `app/ml_close.py`: monthly-close audit, status ledger, and interactive-card actions.
- `app/unified_report.py`: strict ERP display mapping, 47-column workbook values/formulas, wiki-template copy, idempotency marker, and write-back verification.

## Acceptance boundary

A seller/month advertising refresh is valid only when the item-level totals match Mercado Libre `metrics_summary` within rounding tolerance. Fresh Feishu rows are verified before old rows are removed, then the final seller/month set is read back. If advertising fetch fails, the API response and monthly-close error card must say `广告费抓取失败`; report rows stay unchanged and all monthly-close confirmation paths, including stale cards, remain blocked. SQLite and an independent atomic fallback file provide restart-safe fail-closed markers when Feishu status or card publication is unavailable, with an immediate in-process emergency gate if persistent publication is temporarily unavailable. Failure, clear, card-generation, error-card delivery, and confirmation mutations are serialized per month. Failure time is captured before lock acquisition; a successful sync may clear only a failure older than that sync attempt and only after the final seller/month Feishu read-back. A manual production repair must call monthly-close re-audit as a separate final step so ordinary per-seller syncs do not emit premature or repeated confirmation cards.

Finance confirmation is complete only after the unified report is generated and read back successfully. ERP display mapping order is maintenance-table ERP SKU, cost-table ERP SKU, then cost-table distributor SKU. An unmatched SKU, blank name/category, or same-tier conflict blocks the whole report before any wiki copy. A completed report is identified by a generator marker in `检查!E1`; retries return the same report instead of creating a duplicate.
