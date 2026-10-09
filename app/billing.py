"""Mercado Libre billing adjustments for local-store monthly profit.

Order and Product Ads occurrence-month values come from their dedicated APIs.
This adapter adds only charges that those APIs do not cover: Display Ads,
Fulfillment fees, return handling, and standalone tax. Shipping credits require
proof that the original charge was included in a previous report.
"""

from __future__ import annotations

import asyncio
from datetime import date
import math
import json
import os
import time
import uuid
from pathlib import Path
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
    # /details has the currency and order metadata while /full/details has the
    # Fulfillment type. Both can describe the same detail_id. Merge their
    # nonempty fields before deduplication so neither half is discarded.
    merged_details: dict[int, dict] = {}
    for detail in details:
        charge = detail.get("charge_info") or {}
        detail_id = charge.get("detail_id")
        if detail_id in (None, ""):
            raise ValueError("billing detail missing charge_info.detail_id")
        detail_id = int(detail_id)
        if detail_id not in merged_details:
            merged_details[detail_id] = detail.copy()
            continue
        merged = merged_details[detail_id]
        previous_charge = merged.get("charge_info") or {}
        if (
            str(previous_charge.get("detail_sub_type") or "") != str(charge.get("detail_sub_type") or "")
            or str(previous_charge.get("detail_type") or "") != str(charge.get("detail_type") or "")
            or not math.isclose(
                float(previous_charge.get("detail_amount")),
                float(charge.get("detail_amount")),
                abs_tol=0.001,
            )
        ):
            raise ValueError(f"billing detail conflict detail_id={detail_id}")
        for key, value in detail.items():
            if not value:
                continue
            if isinstance(value, dict) and isinstance(merged.get(key), dict):
                merged[key] = {**merged[key], **{k: v for k, v in value.items() if v is not None}}
            else:
                merged[key] = value
    totals = {
        "display_ads": 0.0,
        "full_fees": 0.0,
        "return_fees": 0.0,
        "other_platform_fees": 0.0,
        "tax_adjustments": 0.0,
        "shipping_adjustments": 0.0,
        "product_ads_ignored": 0.0,
        "order_payment_fees_ignored": 0.0,
        "buyer_financing_ignored": 0.0,
    }
    currencies: set[str] = set()
    relevant_count = 0
    unclassified: list[dict] = []
    known_order_derived_subtypes = {
        "CV", "BV", "CFF", "BFF",  # Mexico sale/shipping
        "CVVML", "BVVML", "CFFE", "BFFE",  # Brazil sale/shipping
        "CFFI",  # Brazil municipal shipping (already covered per order)
    }
    for detail in merged_details.values():
        charge = detail.get("charge_info") or {}
        detail_id = charge.get("detail_id")
        if detail_id in (None, ""):
            raise ValueError("billing detail missing charge_info.detail_id")
        detail_id = int(detail_id)

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
        fulfillment = detail.get("fulfillment_info") or {}
        try:
            fulfillment_amount = float(fulfillment.get("amount"))
        except (TypeError, ValueError):
            fulfillment_amount = float("nan")

        bucket = None
        if subtype in known_order_derived_subtypes or any(
            phrase in label
            for phrase in (
                "custo por vender no mercado livre",
                "tarifa de envio extra ou intermunicipal",
                "cargo por venta",
                "cargo por envios de mercado libre",
            )
        ):
            continue
        if (default_currency == 'BRL' or (detail.get('currency_info') or {}).get('currency_id') == 'BRL') and subtype in {'CVVPRC','BVVPRC','CVVFNU','BVVFNU'}:
            # Native /orders.sale_fee already contains these payment components.
            bucket = 'order_payment_fees_ignored'
        elif (default_currency == 'BRL' or (detail.get('currency_info') or {}).get('currency_id') == 'BRL') and subtype in {'CFONPN','BFONPN'}:
            # Buyer-financed price addition and its reversal are not seller cost.
            bucket = 'buyer_financing_ignored'
        elif subtype == "PADS" or "product ads" in label:
            bucket = "product_ads_ignored"
        elif subtype == "CDIFAL" and str(charge.get("detail_type") or "").upper() == "CHARGE" and (
            str(charge.get("debited_from_operation") or "").upper() == "NO"
            and not detail.get("shipping_info") and not detail.get("items_info")
        ):
            # Separate ICMS-DIFAL bill: not debited from an order and not present
            # in order/shipment detail. Keep it in a tax field, not commission.
            bucket = "tax_adjustments"
        elif (month == '2026-09' and detail_id == 70838835146 and subtype == 'BFFI'
              and str(charge.get('detail_type')).upper() == 'BONUS'
              and str(charge.get('charge_bonified_id')) == '70083277162'
              and str((detail.get('shipping_info') or {}).get('shipping_id')) == '47877948646'
              and str((detail.get('shipping_info') or {}).get('pack_id')) == '2000014751989369'
              and ((detail.get('currency_info') or {}).get('currency_id') or default_currency) == 'BRL'
              and math.isclose(abs(amount), 14.45, abs_tol=0.001)):
            # Single verified replay, not a blanket BFFI rule. Prior expense
            # evidence and retained August scope: docs/repairs/2026-10-09-september-system.md.
            bucket = 'shipping_adjustments'
        elif subtype in {"CDLIT", "BDLIT"} or "display ads" in label:
            bucket = "display_ads"
        elif subtype == "CFRS" and (
            str(charge.get("detail_type") or "").upper() == "CHARGE"
            and str(charge.get("concept_type") or "").upper() == "FULFILLMENT"
            and str(fulfillment.get("type") or "").upper() == "WITHDRAWAL"
            and math.isfinite(fulfillment_amount)
            and math.isclose(abs(fulfillment_amount), abs(amount), abs_tol=0.001)
        ):
            bucket = "full_fees"
        elif subtype in {"CFWA", "CFPB", "CFCBI"} or (
            "full" in label and ("almacenamiento" in label or "incumplimiento" in label)
        ):
            bucket = "full_fees"
        elif subtype == "CDSD" or "devolucao" in label or "devolucion" in label:
            bucket = "return_fees"
        elif subtype in {"CESM", "BESM"} or any(
            phrase in label
            for phrase in (
                "mantenimiento de eshop",
                "manutencao da sua loja virtual",
                "custo por cobrar no mercado pago",
                "taxa de recebimento",
                "taxa de parcelamento",
            )
        ):
            bucket = "other_platform_fees"
        if bucket is None:
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
        if bucket not in {'product_ads_ignored','order_payment_fees_ignored','buyer_financing_ignored'}:
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
            floor = 60.0 if response.status_code == 429 and not raw_retry_after else 0.0
            await asyncio.sleep(max(retry_after, floor, retry_delays[attempt] if not raw_retry_after else 0.0))
            continue
        try:
            error_payload = response.json()
        except ValueError:
            error_payload = {}
        error_code = error_payload.get("error") if isinstance(error_payload, dict) else None
        error_message = error_payload.get("message") if isinstance(error_payload, dict) else None
        raise RuntimeError(
            f"billing API failed status={response.status_code} url={url} "
            f"params={params} error={str(error_code or '')[:120]} "
            f"message={str(error_message or '')[:120]}"
        )
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


async def fetch_month_adjustments(seller_id: int, month: str, cache: bool = False) -> dict:
    _month_bounds(month)
    if not cache:
        return await _fetch_month_adjustments(seller_id, month)
    path = Path(db.DB_PATH).parent / 'billing-month-cache' / f'{int(seller_id)}-{month}.json'
    if path.exists() and time.time() - path.stat().st_mtime < 3600:
        payload = json.loads(path.read_text(encoding='utf-8'))
        result = summarize_month_details(payload['details'], month, payload['currency'])
        return {**result, **payload['metadata'], 'cache_hit': True, 'captured_at': payload['captured_at']}
    for attempt in range(2):
        try:
            result = await _fetch_month_adjustments(seller_id, month, retain_details=True, request_gap=25)
            break
        except RuntimeError as exc:
            if attempt or 'remaining total mismatch' not in str(exc):
                raise
            # Discard the entire inconsistent read; never join two snapshots.
            await asyncio.sleep(10)
    details = result.pop('_details')
    captured = int(time.time())
    payload = {'details': details, 'currency': result['currency'], 'captured_at': captured,
               'metadata': {k:result[k] for k in ('seller_id','month','period_keys','raw_details','raw_full_details')}}
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f'.{uuid.uuid4().hex}.tmp')
    temp.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
    os.replace(temp, path)
    return {**result, 'cache_hit': False, 'captured_at': captured}


async def _fetch_month_adjustments(seller_id: int, month: str, retain_details: bool = False,
                                  request_gap: float = 0) -> dict:
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
            for document_type, endpoint_suffix in (
                ('BILL','details'), ('BILL','full/details'),
                ('CREDIT_NOTE','details'), ('CREDIT_NOTE','full/details'),
            ):
                from_id = 0
                fetched = 0
                expected_total = None
                seen_ids = set()
                while True:
                    page_params = {
                        "document_type": document_type,
                        "limit": 1000,
                        "sort_by": "ID",
                        "order_by": "ASC",
                    }
                    # Mercado Libre returns 404 when from_id=0 is sent on the
                    # first page.  Add the cursor only after the API gives us a
                    # real last_id from the preceding page.
                    if from_id:
                        page_params["from_id"] = from_id
                    # The billing API can briefly return a page from a different
                    # count snapshot. Retry the SAME cursor before treating the
                    # report as incomplete; never consume a mismatched page.
                    for consistency_attempt in range(3):
                        if request_gap:
                            await asyncio.sleep(request_gap)
                        payload = await _get_json(
                            client,
                            f"{base}/billing/integration/periods/key/{key}/group/ML/{endpoint_suffix}",
                            headers,
                            page_params,
                        )
                        total = payload.get("total")
                        if not isinstance(total, int) or total < 0:
                            raise RuntimeError(
                                f"billing {endpoint_suffix} missing total key={key}"
                            )
                        if expected_total is None or total == expected_total - fetched:
                            break
                        if consistency_attempt == 2:
                            raise RuntimeError(
                                f"billing {endpoint_suffix} remaining total mismatch key={key} "
                                f"expected={expected_total - fetched} actual={total} "
                                f"fetched={fetched}"
                            )
                        await asyncio.sleep(consistency_attempt + 1)
                    page = payload.get("results") or []
                    if expected_total is None:
                        expected_total = total
                    ids = [int((row.get('charge_info') or {}).get('detail_id')) for row in page]
                    if len(ids) != len(set(ids)) or seen_ids.intersection(ids):
                        raise RuntimeError(f'billing duplicate detail ids key={key}')
                    if fetched + len(page) > expected_total:
                        raise RuntimeError(f'billing over-count key={key}')
                    seen_ids.update(ids)
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
    if retain_details:
        result['_details'] = all_details
    return result
