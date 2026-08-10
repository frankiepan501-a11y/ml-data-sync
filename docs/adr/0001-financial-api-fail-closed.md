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
7. An advertising fetch failure returns HTTP 502 with the exact business phrase `广告费抓取失败`, records the affected shop in the monthly-close status ledger, and sends a red error card when `commit=true`.
8. An open advertising failure blocks later monthly-close audits from returning `待运营确认`, even if the preserved report rows contain an old numeric zero.
9. Failure markers are tracked per shop and are cleared only after that shop's replacement rows pass final Feishu read-back; clearing the last shop moves the month to `退回重算` so a fresh audit is still required.
10. Read-only audits and stale card callbacks honor the same open failure marker; neither may create or approve a confirmation card while advertising is unknown.
11. Each failure is written to both persistent SQLite and an independent atomic fallback file before Feishu publication. Monthly-close audit, status, card generation, and confirmation callbacks read both restart-safe markers as authoritative fail-closed gates. An in-process emergency marker blocks the same paths immediately if persistent publication is temporarily unavailable; the recorder reports an error if no persistent gate succeeds.
12. Failure, clear, card-generation, error-card delivery, and confirmation mutations are serialized per month in the single-worker service. Failure time is captured at recorder entry, before lock acquisition, and the same nanosecond timestamp is written to both stores. A successful sync may clear only failures no newer than that sync's start time.
13. The status ledger stores a compact, valid JSON summary instead of truncating arbitrary JSON text.
14. Failure visibility uses two independent Feishu paths: the status ledger and the red error card. One path failing does not prevent the other from being attempted; the failure is considered unpublished only if both paths fail.
15. Recovery writes the fail-closed monthly status (`退回重算` or the remaining-shop error) before deleting SQLite, fallback-file, or emergency markers. If the status write fails, all existing markers remain intact.

## Consequences

- A transient upstream failure blocks a monthly refresh instead of presenting an unverified zero.
- Operators see a failed run and can safely replay one seller/month.
- Background (`nowait=true`) runs also produce a visible error card instead of leaving the failure only in logs.
- Multiple shop failures cannot overwrite each other within the deployed single-worker service.
- A temporary Feishu status/card outage cannot turn an open advertising failure into an approvable month.
- A SQLite write outage cannot erase the gate on service restart because the independent atomic marker is read on startup-era requests.
- Legitimate zero spend is still allowed after a successful response whose official summary is zero.
