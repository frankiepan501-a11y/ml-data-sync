import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import billing


class BillingAdjustmentTests(unittest.TestCase):
    def test_september_brazil_tax_is_classified_but_prior_month_shipping_bonus_stays_blocked(self):
        def row(detail_id, created, subtype, amount, detail_type, **extra):
            return {
                "charge_info": {
                    "detail_id": detail_id,
                    "creation_date_time": created,
                    "transaction_detail": extra.pop("label", subtype),
                    "detail_sub_type": subtype,
                    "detail_type": detail_type,
                    "detail_amount": amount,
                    **extra.pop("charge", {}),
                },
                "shipping_info": extra.pop("shipping", None),
                "items_info": extra.pop("items", None),
                "currency_info": {"currency_id": "BRL"},
            }

        prior_shipping = {"shipping_id": "47877948646", "pack_id": "2000014751989369"}
        details = [
            row(70083277162, "2026-08-28T14:28:19", "CFFI", 14.45, "CHARGE",
                shipping=prior_shipping),
            row(70524298805, "2026-09-03T20:18:32", "CDIFAL", 8.46, "CHARGE",
                label="Cobrança do diferencial de alíquota interestadual (ICMS-DIFAL)",
                charge={"debited_from_operation": "NO"}),
            row(70838835146, "2026-09-08T12:51:34", "BFFI", 14.45, "BONUS",
                label="Cancelamento da tarifa por envio interno ao município",
                charge={"charge_bonified_id": 70083277162}, shipping=prior_shipping),
        ]

        result = billing.summarize_month_details(details, "2026-09", default_currency="BRL")

        self.assertEqual(1, result["unclassified_count"])
        self.assertEqual(8.46, result["tax_adjustments"])
        self.assertEqual(-14.45, result["unclassified_amount"])
        self.assertEqual(70838835146, result["unclassified"][0]["detail_id"])

    def test_unlinked_shipping_bonus_stays_blocked(self):
        detail = {
            "charge_info": {
                "detail_id": 70838835146,
                "creation_date_time": "2026-09-08T12:51:34",
                "transaction_detail": "Cancelamento da tarifa por envio interno ao município",
                "detail_sub_type": "BFFI",
                "detail_type": "BONUS",
                "detail_amount": 14.45,
                "charge_bonified_id": 70083277162,
            },
            "shipping_info": {"shipping_id": "47877948646"},
            "currency_info": {"currency_id": "BRL"},
        }

        result = billing.summarize_month_details([detail], "2026-09", default_currency="BRL")

        self.assertEqual(1, result["unclassified_count"])

    def test_mexico_full_stock_withdrawal_is_full_cost(self):
        detail = {
            "charge_info": {
                "detail_id": 72308065403,
                "creation_date_time": "2026-09-29T19:27:26",
                "transaction_detail": "Cargo por retiro de stock Full",
                "detail_sub_type": "CFRS",
                "detail_type": "CHARGE",
                "detail_amount": 6.8,
                "concept_type": "FULFILLMENT",
            },
            "fulfillment_info": {"type": "WITHDRAWAL", "amount": 6.8},
        }

        result = billing.summarize_month_details([detail], "2026-09", default_currency="MXN")

        self.assertEqual(0, result["unclassified_count"])
        self.assertEqual(6.8, result["full_fees"])

        detail["fulfillment_info"]["amount"] = 7.8
        mismatch = billing.summarize_month_details([detail], "2026-09", default_currency="MXN")
        self.assertEqual(1, mismatch["unclassified_count"])

    def test_split_billing_periods_must_cover_every_day_of_natural_month(self):
        periods = [
            {"key": "2026-07-18", "period": {"date_from": "2026-07-18", "date_to": "2026-08-17"}},
            {"key": "2026-08-18", "period": {"date_from": "2026-08-18", "date_to": "2026-09-17"}},
        ]

        selected = billing._periods_cover_month(periods, "2026-08")

        self.assertEqual(["2026-07-18", "2026-08-18"], [period["key"] for period in selected])

    def test_incomplete_billing_period_coverage_is_rejected(self):
        periods = [
            {"key": "2026-08-18", "period": {"date_from": "2026-08-18", "date_to": "2026-09-17"}},
        ]

        with self.assertRaisesRegex(RuntimeError, "do not cover natural month"):
            billing._periods_cover_month(periods, "2026-08")

    def test_natural_month_adjustments_are_classified_without_double_counting_product_ads(self):
        def row(detail_id, created, label, subtype, amount, detail_type="CHARGE"):
            return {
                "charge_info": {
                    "detail_id": detail_id,
                    "creation_date_time": created,
                    "transaction_detail": label,
                    "detail_sub_type": subtype,
                    "detail_type": detail_type,
                    "detail_amount": amount,
                },
                "currency_info": {"currency_id": "MXN"},
            }

        details = [
            row(1, "2026-08-02T10:00:00", "Cargo por campaña de publicidad de Display Ads", "CDLIT", 50),
            row(2, "2026-08-03T10:00:00", "Anulación del cargo por campaña de publicidad de Display Ads", "BDLIT", 10, "BONUS"),
            row(3, "2026-08-04T10:00:00", "Cargo por servicio de almacenamiento Full", "CFWA", 20),
            row(4, "2026-08-05T10:00:00", "Cargo por incumplimiento en Envíos Full", "CFPB", 30),
            row(5, "2026-08-06T10:00:00", "Cargo por devolución", "CDSD", 40),
            row(6, "2026-08-07T10:00:00", "Cargo por campaña de publicidad de Product Ads", "PADS", 999),
            row(7, "2026-07-31T23:59:59", "Cargo por devolución", "CDSD", 100),
            row(8, "2026-09-01T00:00:00", "Cargo por devolución", "CDSD", 100),
            row(5, "2026-08-06T10:00:00", "Cargo por devolución", "CDSD", 40),
        ]

        result = billing.summarize_month_details(details, "2026-08", default_currency="MXN")

        self.assertEqual(result["currency"], "MXN")
        self.assertEqual(result["detail_count"], 5)
        self.assertEqual(result["display_ads"], 40.0)
        self.assertEqual(result["full_fees"], 50.0)
        self.assertEqual(result["return_fees"], 40.0)
        self.assertEqual(result["product_ads_ignored"], 999.0)

    def test_full_rows_without_currency_use_the_seller_currency_and_deduplicate(self):
        full_row = {
            "charge_info": {
                "detail_id": 11,
                "creation_date_time": "2026-08-10T10:00:00",
                "transaction_detail": "Cargo por servicio de almacenamiento Full",
                "detail_sub_type": "CFWA",
                "detail_type": "CHARGE",
                "detail_amount": 25,
            },
        }

        result = billing.summarize_month_details(
            [full_row, full_row], "2026-08", default_currency="MXN"
        )

        self.assertEqual("MXN", result["currency"])
        self.assertEqual(25.0, result["full_fees"])
        self.assertEqual(1, result["detail_count"])

    def test_unknown_billing_subtype_is_reported_instead_of_silently_ignored(self):
        detail = {
            "charge_info": {
                "detail_id": 12,
                "creation_date_time": "2026-08-11T10:00:00",
                "transaction_detail": "Cargo nuevo no mapeado",
                "detail_sub_type": "NEWFEE",
                "detail_type": "CHARGE",
                "detail_amount": 12.5,
            },
            "currency_info": {"currency_id": "MXN"},
        }

        result = billing.summarize_month_details([detail], "2026-08")

        self.assertEqual(1, result["unclassified_count"])
        self.assertEqual(12.5, result["unclassified_amount"])
        self.assertEqual("NEWFEE", result["unclassified"][0]["subtype"])

    def test_brazilian_billing_labels_separate_order_costs_from_extra_fees(self):
        def row(detail_id, label, subtype, amount, detail_type="CHARGE"):
            return {
                "charge_info": {
                    "detail_id": detail_id,
                    "creation_date_time": "2026-08-12T10:00:00",
                    "transaction_detail": label,
                    "detail_sub_type": subtype,
                    "detail_type": detail_type,
                    "detail_amount": amount,
                },
                "currency_info": {"currency_id": "BRL"},
            }

        details = [
            row(30, "Custo por vender no Mercado Livre", "CVVML", 10),
            row(31, "Tarifa de envio extra ou intermunicipal", "CFFE", 20),
            row(32, "Custo por cobrar no Mercado Pago", "CVVPRC", 2),
            row(33, "Estorno do custo por cobrar no Mercado Pago", "BVVPRC", 0.5, "BONUS"),
            row(34, "Taxa de recebimento", "CVVFNU", 0.2),
            row(35, "Taxa de parcelamento", "CVVPARC", 3),
            row(36, "Tarifa de manutenção da sua loja virtual", "CESM", 10),
            row(37, "Tarifa por devolução", "CDFR", 4),
            row(38, "Estorno da tarifa por devolução", "BDFR", 1, "BONUS"),
            row(39, "Tarifa pelo serviço de armazenamento Full", "CFWA", 5),
            row(40, "Tarifa por envio interno ao município", "CFFI", 6),
            row(41, "Custo do serviço de coleta Full", "CFCBI", 7),
        ]

        result = billing.summarize_month_details(details, "2026-08")

        self.assertEqual(0, result["unclassified_count"])
        self.assertEqual(14.7, result["other_platform_fees"])
        self.assertEqual(3.0, result["return_fees"])
        self.assertEqual(12.0, result["full_fees"])


class BillingFetchTests(unittest.IsolatedAsyncioTestCase):
    async def test_bad_request_reports_safe_cursor_for_diagnosis(self):
        bad = MagicMock(status_code=400, headers={})
        bad.json.return_value = {"error": "invalid_from_id"}
        client = MagicMock()
        client.get = AsyncMock(return_value=bad)

        with self.assertRaisesRegex(RuntimeError, "from_id.*123.*invalid_from_id"):
            await billing._get_json(client, "https://example.test", {}, {"from_id": 123})

    async def test_get_json_retries_rate_limit_before_returning_complete_page(self):
        limited = MagicMock(status_code=429, headers={"retry-after": "1"})
        ok = MagicMock(status_code=200, headers={})
        ok.json.return_value = {"total": 0, "results": []}
        client = MagicMock()
        client.get = AsyncMock(side_effect=[limited, ok])

        with patch.object(billing.asyncio, "sleep", AsyncMock()) as sleeper:
            result = await billing._get_json(client, "https://example.test", {}, {})

        self.assertEqual(0, result["total"])
        self.assertEqual(2, client.get.await_count)
        sleeper.assert_awaited_once_with(1.0)

    async def test_fetch_reads_general_and_full_endpoints(self):
        full_row = {
            "charge_info": {
                "detail_id": 21,
                "creation_date_time": "2026-08-12T10:00:00",
                "transaction_detail": "Cargo por servicio de almacenamiento Full",
                "detail_sub_type": "CFWA",
                "detail_type": "CHARGE",
                "detail_amount": 35,
            },
        }
        responses = [
            {"results": [{
                "key": "2026-08-01",
                "period": {"date_from": "2026-08-01", "date_to": "2026-08-31"},
            }]},
            {"total": 0, "results": []},
            {"total": 1, "results": [full_row], "last_id": 21},
        ]
        getter = AsyncMock(side_effect=responses)

        with (
            patch.object(billing.db, "get_token", AsyncMock(return_value={"access_token": "x"})),
            patch.object(billing, "_get_json", getter),
        ):
            result = await billing.fetch_month_adjustments(3383185411, "2026-08")

        called_urls = [call.args[1] for call in getter.await_args_list]
        self.assertTrue(any(url.endswith("/group/ML/details") for url in called_urls))
        self.assertTrue(any(url.endswith("/group/ML/full/details") for url in called_urls))
        self.assertNotIn("from_id", getter.await_args_list[1].args[3])
        self.assertNotIn("from_id", getter.await_args_list[2].args[3])
        self.assertEqual(35.0, result["full_fees"])
        self.assertEqual(1, result["raw_full_details"])

    async def test_fetch_accepts_remaining_total_that_decreases_after_cursor(self):
        def row(detail_id):
            return {
                "charge_info": {
                    "detail_id": detail_id,
                    "creation_date_time": "2026-08-12T10:00:00",
                    "transaction_detail": "Cargo por venta",
                    "detail_sub_type": "CV",
                    "detail_type": "CHARGE",
                    "detail_amount": 1,
                },
                "currency_info": {"currency_id": "MXN"},
            }

        getter = AsyncMock(side_effect=[
            {"results": [{
                "key": "2026-08-01",
                "period": {"date_from": "2026-08-01", "date_to": "2026-08-31"},
            }]},
            {"total": 3, "results": [row(1), row(2)], "last_id": 2},
            {"total": 1, "results": [row(3)], "last_id": 3},
            {"total": 0, "results": []},
        ])

        with (
            patch.object(billing.db, "get_token", AsyncMock(return_value={"access_token": "x"})),
            patch.object(billing, "_get_json", getter),
        ):
            result = await billing.fetch_month_adjustments(3383185411, "2026-08")

        self.assertEqual(3, result["raw_details"])
        self.assertEqual(2, getter.await_args_list[2].args[3]["from_id"])

    async def test_fetch_retries_transient_remaining_total_mismatch_without_skipping_page(self):
        def row(detail_id):
            return {
                "charge_info": {
                    "detail_id": detail_id,
                    "creation_date_time": "2026-08-12T10:00:00",
                    "transaction_detail": "Cargo por venta",
                    "detail_sub_type": "CV",
                    "detail_type": "CHARGE",
                    "detail_amount": 1,
                },
                "currency_info": {"currency_id": "MXN"},
            }

        getter = AsyncMock(side_effect=[
            {"results": [{"key": "2026-08-01", "period": {"date_from": "2026-08-01", "date_to": "2026-08-31"}}]},
            {"total": 3, "results": [row(1), row(2)], "last_id": 2},
            {"total": 200, "results": [row(3)], "last_id": 3},
            {"total": 1, "results": [row(3)], "last_id": 3},
            {"total": 0, "results": []},
        ])
        with (
            patch.object(billing.db, "get_token", AsyncMock(return_value={"access_token": "x"})),
            patch.object(billing, "_get_json", getter),
            patch.object(billing.asyncio, "sleep", AsyncMock()),
        ):
            result = await billing.fetch_month_adjustments(3383185411, "2026-08")

        self.assertEqual(3, result["raw_details"])
        self.assertEqual(2, getter.await_args_list[2].args[3]["from_id"])
        self.assertEqual(2, getter.await_args_list[3].args[3]["from_id"])

    async def test_fetch_still_blocks_persistent_remaining_total_mismatch(self):
        row = {"charge_info": {"detail_id": 1}}
        getter = AsyncMock(side_effect=[
            {"results": [{"key": "2026-08-01", "period": {"date_from": "2026-08-01", "date_to": "2026-08-31"}}]},
            {"total": 2, "results": [row], "last_id": 1},
            {"total": 200, "results": [row], "last_id": 2},
            {"total": 200, "results": [row], "last_id": 2},
            {"total": 200, "results": [row], "last_id": 2},
        ])
        with (
            patch.object(billing.db, "get_token", AsyncMock(return_value={"access_token": "x"})),
            patch.object(billing, "_get_json", getter),
            patch.object(billing.asyncio, "sleep", AsyncMock()),
        ):
            with self.assertRaisesRegex(RuntimeError, "remaining total mismatch"):
                await billing.fetch_month_adjustments(3383185411, "2026-08")

        self.assertEqual(5, getter.await_count)
        self.assertEqual([1, 1, 1], [call.args[3]["from_id"] for call in getter.await_args_list[2:]])


if __name__ == "__main__":
    unittest.main()
