import unittest
from unittest.mock import AsyncMock, patch

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


class BillingFetchTests(unittest.IsolatedAsyncioTestCase):
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
        self.assertEqual(35.0, result["full_fees"])
        self.assertEqual(1, result["raw_full_details"])


if __name__ == "__main__":
    unittest.main()
