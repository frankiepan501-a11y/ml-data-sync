import unittest

from app import shipping


class ShippingPayloadSafetyTests(unittest.TestCase):
    def test_explicit_zero_sender_cost_is_valid(self):
        result = shipping._validated_cost_payload({
            "senders": [{"cost": 0}],
            "gross_amount": 50,
            "currency_id": "MXN",
        })

        self.assertEqual(0.0, result["sender_cost"])
        self.assertEqual("MXN", result["currency"])

    def test_missing_sender_cost_or_currency_is_not_valid_zero(self):
        self.assertIsNone(shipping._validated_cost_payload({
            "senders": [{}],
            "gross_amount": 50,
            "currency_id": "MXN",
        }))
        self.assertIsNone(shipping._validated_cost_payload({
            "senders": [{"cost": 0}],
            "gross_amount": 50,
            "currency_id": "",
        }))

    def test_negative_or_nonfinite_sender_cost_is_rejected(self):
        for value in (-1, float("nan"), float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(shipping._validated_cost_payload({
                    "senders": [{"cost": value}],
                    "gross_amount": 50,
                    "currency_id": "MXN",
                }))


if __name__ == "__main__":
    unittest.main()
