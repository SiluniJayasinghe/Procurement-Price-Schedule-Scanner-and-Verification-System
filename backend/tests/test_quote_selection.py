import unittest

from main import verify


class QuoteSelectionTests(unittest.TestCase):
    def test_selects_lowest_vat_exclusive_unit_price_and_calculates_total(self):
        result = verify([{
            "rows": [{
                "item": "Item A",
                "quantity": "10",
                "quote_options": [
                    {
                        "supplier": "Supplier A",
                        "unit_price_with_vat": "90",
                        "unit_price_without_vat": "70",
                    },
                    {
                        "supplier": "Supplier B",
                        "unit_price_with_vat": "110",
                        "unit_price_without_vat": "60",
                    },
                ],
            }],
        }])

        row = result["pages"][0]["rows"][0]
        self.assertEqual(row["selected_quote_index"], 1)
        self.assertEqual(row["selected_quote_unit_price"], "60.00")
        self.assertEqual(row["unit_price_without_vat"], "60.00")
        self.assertEqual(row["unit_price_with_vat"], "110.00")
        self.assertEqual(row["calculated_total_without_vat"], "600.00")

    def test_selects_lowest_vat_exclusive_package_price_without_vat_rate(self):
        result = verify([{
            "rows": [{
                "item": "Item B",
                "quantity": "3",
                "quote_options": [
                    {"supplier": "Supplier A", "bulk_quantity": "10", "bulk_price_without_vat": "1000"},
                    {"supplier": "Supplier B", "bulk_quantity": "10", "bulk_price_without_vat": "700"},
                ],
            }],
        }])

        row = result["pages"][0]["rows"][0]
        self.assertEqual(row["selected_quote_index"], 1)
        self.assertEqual(row["unit_price_without_vat"], "70.00")
        self.assertEqual(row["calculated_total_without_vat"], "210.00")

    def test_exclusive_only_scan_keeps_inclusive_unit_price_blank(self):
        result = verify([{
            "rows": [{
                "item": "Item C",
                "quantity": "10",
                "unit_price_with_vat": "26000",
                "unit_price_with_vat_present": False,
                "unit_price_without_vat": "2600",
            }],
        }])

        row = result["pages"][0]["rows"][0]
        self.assertIsNone(row["unit_price_with_vat"])
        self.assertEqual(row["unit_price_with_vat_status"], "not_given")
        self.assertEqual(row["unit_price_without_vat"], "2600.00")
        self.assertEqual(row["calculated_total_without_vat"], "26000.00")

    def test_legacy_manual_inclusive_unit_price_remains_supported(self):
        result = verify([{
            "rows": [{
                "item": "Manual item",
                "quantity": "2",
                "unit_price_with_vat": "100",
            }],
        }])

        row = result["pages"][0]["rows"][0]
        self.assertEqual(row["unit_price_with_vat"], "100.00")


if __name__ == "__main__":
    unittest.main()
