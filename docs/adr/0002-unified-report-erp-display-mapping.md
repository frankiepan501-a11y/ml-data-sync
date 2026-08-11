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

The approved 2026-07 wiki spreadsheet is copied as the visual template. The generator replaces all three sheets (`美客多毛利报表-YYYY-MM`, `数据源`, `检查`), verifies the written result, and stores `ML_UNIFIED_REPORT_V1|COMPLETE|...` in `检查!E1`. An existing completed marker makes retries idempotent. An unmarked same-title file is not overwritten.

## Consequences

- Finance receives the same format every month without manual product-field filling.
- Bad or incomplete ERP master data is visible as a blocking error instead of an empty cell or zero.
- A successful spreadsheet can exist briefly before final-state write if a new advertising failure arrives; the later retry reuses it after the close gate is clean.
- The report generator needs read access to the product Base through the configured product-data Feishu App and copy/write access to the finance wiki through the reporting App.
