import io
import unittest
from datetime import datetime

import openpyxl

from app.final_billing import HEADERS, read_local_export, check_closed_coverage, month_bounds, compare_charge_sources


def export(rows, currency="BRL"):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "REPORT"
    for _ in range(7):
        ws.append([None])
    ws.append(list(HEADERS[currency].values()))
    for row in rows:
        ws.append(row)
    out = io.BytesIO()
    wb.save(out)
    wb.close()
    return out.getvalue()


def charge(identifier="100", amount=12):
    return [identifier, datetime(2026, 8, 5), "Custo por vender no Mercado Livre", amount, "2000014805653647", "", ""]


class FinalBillingTests(unittest.TestCase):
    def test_export_is_not_closed_period_proof(self):
        result = read_local_export(export([charge()]), "2378517428", "BRL")
        self.assertFalse(result["ready_for_final_confirmation"])
        self.assertFalse(result["closed_period_verified"])
        self.assertEqual(result["month_counts"], {"2026-08": 1})
        self.assertEqual(result["rows"][0]["source_row"], 9)

    def test_credit_sign_and_reference_preserved(self):
        row = charge("101", -12)
        row[5] = "100"
        result = read_local_export(export([charge(), row]), "2378517428", "BRL")
        self.assertEqual(result["rows"][1]["amount"], "-12")
        self.assertEqual(result["rows"][1]["reversed_charge_id"], "100")

    def test_duplicate_dedup_and_conflict_reject(self):
        result = read_local_export(export([charge(), charge()]), "2378517428", "BRL")
        self.assertEqual(len(result["rows"]), 1)
        self.assertEqual(result["duplicates"], 1)
        with self.assertRaisesRegex(ValueError, "冲突"):
            read_local_export(export([charge(), charge(amount=13)]), "2378517428", "BRL")

    def test_missing_amount_and_ambiguous_date_reject(self):
        for index, value in [(3, None), (1, "08/09/2026"), (0, ""), (4, 2000014805653647)]:
            row = charge()
            row[index] = value
            with self.assertRaises(ValueError):
                read_local_export(export([row]), "2378517428", "BRL")

    def test_mx_headers_and_wrong_currency(self):
        self.assertEqual(read_local_export(export([charge()], "MXN"), "3383185411", "MXN")["currency"], "MXN")
        with self.assertRaises(ValueError):
            read_local_export(export([charge()]), "3383185411", "BRL")

    def period(self, start, end, **kwargs):
        return dict(seller_id="2378517428", currency="BRL", date_from=start, date_to=end,
                    status="CLOSED", source_sha256="a"*64, source_reference="official-period-1",
                    documents_complete=True, **kwargs)

    def test_two_closed_periods_cover_natural_month(self):
        result = check_closed_coverage("2026-08", [self.period("2026-07-18", "2026-08-17"), self.period("2026-08-18", "2026-09-17")], "2378517428", "BRL")
        self.assertTrue(result["complete"])

    def test_open_missing_documents_and_missing_day_fail_closed(self):
        for field, value in [("status", "OPEN"), ("documents_complete", False), ("source_reference", ""), ("currency", "MXN")]:
            p = self.period("2026-08-01", "2026-08-31")
            p[field] = value
            self.assertFalse(check_closed_coverage("2026-08", [p], "2378517428", "BRL")["complete"])
        p = self.period("2026-08-01", "2026-08-30")
        self.assertEqual(check_closed_coverage("2026-08", [p], "2378517428", "BRL")["missing_dates"], ["2026-08-31"])

    def test_invalid_month(self):
        for month in ["2026-8", "2026-13", "", None]:
            with self.assertRaises((ValueError, TypeError)):
                month_bounds(month)

    def test_ab_matches_without_authorizing_close(self):
        rows = read_local_export(export([charge()]), "2378517428", "BRL")["rows"]
        result = compare_charge_sources(rows, [{**rows[0], "amount": "12.00", "source_sha256": "b"*64}])
        self.assertTrue(result["matched"])
        self.assertFalse(result["ready_for_final_confirmation"])

    def test_ab_refund_not_deducted_is_a_difference(self):
        rows = read_local_export(export([charge()]), "2378517428", "BRL")["rows"]
        other = [{**rows[0], "amount": "-12", "reversed_charge_id": "99"}]
        result = compare_charge_sources(rows, other)
        self.assertFalse(result["matched"])
        self.assertIn("amount", result["differences"][0]["fields"])
        self.assertIn("reversed_charge_id", result["differences"][0]["fields"])

    def test_ab_missing_and_empty_block(self):
        rows = read_local_export(export([charge()]), "2378517428", "BRL")["rows"]
        self.assertFalse(compare_charge_sources([], [])["matched"])
        self.assertEqual(compare_charge_sources([], rows)["differences"][0]["kind"], "missing_in_automatic")
        self.assertEqual(compare_charge_sources(rows, [])["differences"][0]["kind"], "missing_in_official")

    def test_ab_version_changes_and_conflicts_block(self):
        rows = read_local_export(export([charge()]), "2378517428", "BRL")["rows"]
        altered = [{**rows[0], "amount": "-12"}]
        self.assertNotEqual(compare_charge_sources(rows, rows)["evidence_sha256"], compare_charge_sources(rows, altered)["evidence_sha256"])
        with self.assertRaises(ValueError):
            compare_charge_sources(rows + altered, rows)
