import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app import billing


class BillingAdjustmentTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
