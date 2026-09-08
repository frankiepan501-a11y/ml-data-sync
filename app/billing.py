"""Mercado Libre billing adjustments for local-store monthly profit.

Order and Product Ads occurrence-month values come from their dedicated APIs.
This adapter adds only charges that those APIs do not cover: Display Ads,
Fulfillment storage/violation charges, and return handling charges.
"""

from __future__ import annotations

import asyncio
from datetime import date
import math
import unicodedata

import httpx

from app import db


def _month_bounds(month: str) -> tuple[date, date]:
    try:
        year, month_number = (int(part) for part in month.split("-"))
        start = date(year, month_number, 1)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"invalid month={month!r}; expected YYYY-MM") from exc
    if month_number == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month_number + 1, 1)
    return start, end


def _plain(value: object) -> str:
    normalized = unicodedata.normalize("NFKD", str(value or "").lower())
    return "".join(char for char in normalized if not unicodedata.combining(char))


def summarize_month_details(
    details: list[dict], month: str, default_currency: str = ""
) -> dict:
    """Classify non-order billing charges by charge date for one natural month."""
    month_start, next_month = _month_bounds(month)
    totals = {
        "display_ads": 0.0,
        "full_fees": 0.0,
        "return_fees": 0.0,
        "other_platform_fees": 0.0,
        "product_ads_ignored": 0.0,
    }
    currencies: set[str] = set()
    relevant_count = 0
    seen_detail_ids: set[int] = set()
    unclassified: list[dict] = []
    known_order_derived_subtypes = {"CV", "BV", "CFF", "BFF"}

    for detail in details:
        charge = detail.get("charge_info") or {}
        detail_id = charge.get("detail_id")
        if detail_id in (None, ""):
            raise ValueError("billing detail missing charge_info.detail_id")
        detail_id = int(detail_id)
        if detail_id in seen_detail_ids:
            continue
        seen_detail_ids.add(detail_id)

        raw_created = str(charge.get("creation_date_time") or "")
        try:
            created = date.fromisoformat(raw_created[:10])
        except ValueError as exc:
            raise ValueError(f"billing detail has invalid creation_date_time detail_id={detail_id}") from exc
        if not (month_start <= created < next_month):
            continue

        try:
            amount = float(charge.get("detail_amount"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"billing detail has invalid amount detail_id={detail_id}") from exc
        if not math.isfinite(amount):
            raise ValueError(f"billing detail has non-finite amount detail_id={detail_id}")
        signed_amount = -abs(amount) if str(charge.get("detail_type") or "").upper() == "BONUS" else abs(amount)
        label = _plain(charge.get("transaction_detail"))
        subtype = str(charge.get("detail_sub_type") or "").upper()

        bucket = None
        if subtype == "PADS" or "product ads" in label:
            bucket = "product_ads_ignored"
        elif subtype in {"CDLIT", "BDLIT"} or "display ads" in label:
            bucket = "display_ads"
        elif subtype in {"CFWA", "CFPB"} or (
            "full" in label and ("almacenamiento" in label or "incumplimiento" in label)
        ):
            bucket = "full_fees"
        elif subtype == "CDSD" or "cargo por devolucion" in label:
            bucket = "return_fees"
        elif subtype in {"CESM", "BESM"} or "mantenimiento de eshop" in label:
            bucket = "other_platform_fees"
        elif subtype in known_order_derived_subtypes:
            continue
        else:
            unclassified.append({
                "detail_id": detail_id,
                "subtype": subtype,
                "detail_type": str(charge.get("detail_type") or ""),
                "label": str(charge.get("transaction_detail") or ""),
                "amount": round(signed_amount, 2),
            })
            continue

        currency = str(
            (detail.get("currency_info") or {}).get("currency_id")
            or default_currency
            or ""
        ).strip()
        if not currency:
            raise ValueError(f"billing detail missing currency detail_id={detail_id}")
        currencies.add(currency)
        totals[bucket] += signed_amount
        if bucket != "product_ads_ignored":
            relevant_count += 1

    if len(currencies) > 1:
        raise ValueError(f"billing adjustment currencies conflict: {sorted(currencies)}")
    return {
        "currency": next(iter(currencies), ""),
        "detail_count": relevant_count,
        "unclassified_count": len(unclassified),
        "unclassified_amount": round(sum(row["amount"] for row in unclassified), 2),
        "unclassified": unclassified[:20],
        **{key: round(value, 2) for key, value in totals.items()},
    }


async def _get_json(client: httpx.AsyncClient, url: str, headers: dict, params: dict) -> dict:
    retry_delays = (2.0, 5.0, 12.0)
    for attempt in range(len(retry_delays) + 1):
        try:
            response = await client.get(url, headers=headers, params=params)
        except httpx.RequestError:
            if attempt == len(retry_delays):
                raise
            await asyncio.sleep(retry_delays[attempt])
            continue
        if response.status_code == 200:
            payload = response.json()
            if payload.get("errors"):
                raise RuntimeError(f"billing API returned errors url={url}")
            return payload
        retryable = response.status_code == 429 or 500 <= response.status_code < 600
        if retryable and attempt < len(retry_delays):
            raw_retry_after = response.headers.get("retry-after")
            try:
                retry_after = float(raw_retry_after) if raw_retry_after else retry_delays[attempt]
            except ValueError:
                retry_after = retry_delays[attempt]
            await asyncio.sleep(max(retry_after, retry_delays[attempt] if not raw_retry_after else 0.0))
            continue
        raise RuntimeError(f"billing API failed status={response.status_code} url={url}")
    raise RuntimeError(f"billing API retry loop exhausted url={url}")


def _periods_cover_month(periods: list[dict], month: str) -> list[dict]:
    month_start, next_month = _month_bounds(month)
    overlapping: list[dict] = []
    covered_days: set[date] = set()
    for period in periods:
        bounds = period.get("period") or {}
        try:
            start = date.fromisoformat(str(bounds.get("date_from") or ""))
            end = date.fromisoformat(str(bounds.get("date_to") or ""))
        except ValueError:
            continue
        if end < month_start or start >= next_month:
            continue
        overlapping.append(period)
        cursor = max(start, month_start)
        last = min(end, date.fromordinal(next_month.toordinal() - 1))
        while cursor <= last:
            covered_days.add(cursor)
            cursor = date.fromordinal(cursor.toordinal() + 1)
    expected_days = set(
        date.fromordinal(ordinal)
        for ordinal in range(month_start.toordinal(), next_month.toordinal())
    )
    if covered_days != expected_days:
        missing = sorted(day.isoformat() for day in expected_days - covered_days)
        raise RuntimeError(f"billing periods do not cover natural month={month}; missing={missing[:5]}")
    return overlapping


async def fetch_month_adjustments(seller_id: int, month: str) -> dict:
    """Fetch current billing pages and return classified natural-month charges."""
    token = await db.get_token(seller_id)
    if not token or not token.get("access_token"):
        raise RuntimeError(f"billing token missing seller_id={seller_id}")
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    base = "https://api.mercadolibre.com"
    async with httpx.AsyncClient(timeout=60) as client:
        periods_payload = await _get_json(
            client,
            f"{base}/billing/integration/monthly/periods",
            headers,
            {"group": "ML", "document_type": "BILL", "offset": 0, "limit": 12},
        )
        periods = _periods_cover_month(periods_payload.get("results") or [], month)
        all_details: list[dict] = []
        raw_full_details = 0
        period_keys: list[str] = []
        for period in periods:
            key = str(period.get("key") or "")
            if not key:
                raise RuntimeError("billing period missing key")
            period_keys.append(key)
            for endpoint_suffix in ("details", "full/details"):
                from_id = 0
                fetched = 0
                expected_total = None
                while True:
                    page_params = {
                        "document_type": "BILL",
                        "limit": 1000,
                        "sort_by": "ID",
                        "order_by": "ASC",
                    }
                    # Mercado Libre returns 404 when from_id=0 is sent on the
                    # first page.  Add the cursor only after the API gives us a
                    # real last_id from the preceding page.
                    if from_id:
                        page_params["from_id"] = from_id
                    payload = await _get_json(
                        client,
                        f"{base}/billing/integration/periods/key/{key}/group/ML/{endpoint_suffix}",
                        headers,
                        page_params,
                    )
                    page = payload.get("results") or []
                    total = payload.get("total")
                    if not isinstance(total, int) or total < 0:
                        raise RuntimeError(
                            f"billing {endpoint_suffix} missing total key={key}"
                        )
                    if expected_total is None:
                        expected_total = total
                    elif total != expected_total - fetched:
                        raise RuntimeError(
                            f"billing {endpoint_suffix} remaining total mismatch key={key} "
                            f"expected={expected_total - fetched} actual={total}"
                        )
                    all_details.extend(page)
                    if endpoint_suffix == "full/details":
                        raw_full_details += len(page)
                    fetched += len(page)
                    if fetched >= expected_total:
                        break
                    next_from_id = payload.get("last_id")
                    if not page or next_from_id in (None, from_id):
                        raise RuntimeError(
                            f"billing {endpoint_suffix} pagination truncated key={key} "
                            f"expected={expected_total} fetched={fetched}"
                        )
                    from_id = int(next_from_id)

    currency_by_seller = {2378517428: "BRL", 3383185411: "MXN"}
    result = summarize_month_details(
        all_details,
        month,
        default_currency=currency_by_seller.get(seller_id, ""),
    )
    result.update({
        "seller_id": seller_id,
        "month": month,
        "period_keys": sorted(set(period_keys)),
        "raw_details": len(all_details),
        "raw_full_details": raw_full_details,
    })
    return result
