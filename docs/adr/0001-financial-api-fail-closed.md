# ADR-0001: Advertising API failures fail closed

- Status: Accepted
- Date: 2026-08-10

## Context

The July 2026 local-store advertising request failed during a monthly sync. The caller recorded `ad_error` but continued to replace the Feishu rows with advertising cost `0`, overstating gross profit. A later cost-only recalculation did not refetch advertising data.

Mercado Libre can also return duplicate item rows from the advertising search endpoint. Summing transport rows can exceed the official `metrics_summary` total.

## Decision

1. A configured advertising source must succeed before a seller/month Feishu replacement starts.
2. Duplicate advertising rows are collapsed by `item_id + campaign_id + ad_group_id`.
3. Deduplicated item metrics must match the response `metrics_summary` within rounding tolerance; mismatch is an error.
4. The Feishu replacement creates and verifies fresh rows before deleting old rows. A create or pre-delete verification failure preserves old rows; a delete failure rolls back the fresh rows.
5. A successful replacement is read back by seller/month and reconciled on row count and advertising totals.
6. Monthly-close re-audit remains a separate explicit operation after a manual repair; it is not triggered by every per-seller sync.

## Consequences

- A transient upstream failure blocks a monthly refresh instead of presenting an unverified zero.
- Operators see a failed run and can safely replay one seller/month.
- Legitimate zero spend is still allowed after a successful response whose official summary is zero.
