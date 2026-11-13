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

    def test_given_vat_exclusive_totals_get_correct_or_incorrect_status_across_scan_paths(self):
        cases = [
            (
                "direct unit price matches",
                {
                    "quantity": "2",
                    "unit_price_without_vat": "100",
                    "document_total_without_vat": "200",
                },
                None,
                "correct",
                "0.00",
            ),
            (
                "direct unit price differs",
                {
                    "quantity": "2",
                    "unit_price_without_vat": "100",
                    "document_total_without_vat": "201",
                },
                None,
                "incorrect",
                "-1.00",
            ),
            (
                "selected supplier quote matches",
                {
                    "quantity": "2",
                    "quote_options": [
                        {"supplier": "Supplier A", "unit_price_without_vat": "120"},
                        {"supplier": "Supplier B", "unit_price_without_vat": "100"},
                    ],
                    "document_total_without_vat": "200",
                },
                None,
                "correct",
                "0.00",
            ),
            (
                "package quote matches",
                {
                    "quantity": "2",
                    "quote_options": [
                        {"supplier": "Supplier A", "bulk_quantity": "5", "bulk_price_without_vat": "500"},
                    ],
                    "document_total_without_vat": "200",
                },
                None,
                "correct",
                "0.00",
            ),
            (
                "exclusive total mismatch remains visible with correct inclusive total",
                {
                    "quantity": "2",
                    "unit_price_with_vat": "118",
                    "unit_price_without_vat": "100",
                    "document_total": "236",
                    "document_total_without_vat": "201",
                },
                "18",
                "incorrect",
                "-1.00",
            ),
        ]

        for name, row_input, vat_multiplier, expected_status, expected_difference in cases:
            with self.subTest(name=name):
                result = verify(
                    [{"rows": [row_input]}],
                    vat_multiplier=vat_multiplier,
                )

                row = result["pages"][0]["rows"][0]
                self.assertEqual(row["calculated_total_without_vat"], "200.00")
                self.assertEqual(row["document_total_without_vat_status"], expected_status)
                self.assertEqual(row["difference_without_vat"], expected_difference)

                if name.startswith("exclusive total mismatch"):
                    self.assertEqual(row["status"], "correct")

    def test_partial_grand_total_sums_rows_with_amounts_and_ignores_dashes(self):
        result = verify([{
            "rows": [
                {
                    "item": "Available amount",
                    "quantity": "2",
                    "unit_price_without_vat": "100",
                },
                {
                    "item": "Missing amount",
                    "quantity": "—",
                    "unit_price_with_vat": "—",
                    "unit_price_without_vat": "—",
                    "document_total": "—",
                    "document_total_without_vat": "—",
                },
            ],
        }])

        page = result["pages"][0]
        self.assertEqual(page["calculated_grand_total"], "200.00")
        self.assertEqual(page["grand_total_status"], "needs_review")
        self.assertEqual(page["table_group_overall_total"], "200.00")
        self.assertEqual(result["overall_total"], "200.00")
        self.assertFalse(result["complete"])

    def test_grand_total_stays_blank_when_no_rows_have_calculable_amounts(self):
        result = verify([{
            "rows": [{
                "item": "Missing amount",
                "quantity": "—",
                "unit_price_with_vat": "—",
                "unit_price_without_vat": "—",
                "document_total": "—",
                "document_total_without_vat": "—",
            }],
        }])

        self.assertIsNone(result["pages"][0]["calculated_grand_total"])
        self.assertIsNone(result["overall_total"])
        self.assertEqual(result["pages"][0]["grand_total_status"], "needs_review")

    def test_inclusive_totals_and_grand_totals_keep_three_decimal_places(self):
        result = verify([{
            "rows": [{
                "quantity": "3",
                "unit_price_with_vat": "1.111",
            }],
        }])

        row = result["pages"][0]["rows"][0]
        self.assertEqual(row["calculated_total_with_vat"], "3.333")
        self.assertEqual(result["pages"][0]["calculated_grand_total"], "3.333")
        self.assertEqual(result["overall_total"], "3.333")

    def test_edited_inclusive_unit_price_takes_precedence_over_bulk_and_vat(self):
        result = verify([{
            "rows": [{
                "quantity": "2",
                "bulk_quantity": "10",
                "bulk_price_with_vat": "100",
                "bulk_price_without_vat": "80",
                "unit_price_with_vat": "12.345",
                "unit_price_without_vat": "8",
                "edited_price_key": "unit_price_with_vat",
            }],
        }], vat_multiplier="18")

        row = result["pages"][0]["rows"][0]
        self.assertEqual(row["calculated_total_with_vat"], "24.690")

    def test_edited_exclusive_unit_price_updates_inclusive_unit_and_both_totals(self):
        result = verify([{
            "rows": [{
                "quantity": "2",
                "unit_price_with_vat": "118",
                "unit_price_without_vat": "200",
                "unit_price_with_vat_present": False,
                "edited_price_key": "unit_price_without_vat",
            }],
        }], vat_multiplier="18")

        row = result["pages"][0]["rows"][0]
        self.assertEqual(row["unit_price_with_vat"], "236.00")
        self.assertEqual(row["unit_price_with_vat_status"], "calculated")
        self.assertEqual(row["calculated_total_without_vat"], "400.00")
        self.assertEqual(row["calculated_total_with_vat"], "472.000")

    def test_restoring_exclusive_unit_price_restores_inclusive_verification_status(self):
        result = verify([{
            "rows": [{
                "quantity": "2",
                "unit_price_with_vat": "118",
                "unit_price_without_vat": "100",
            }],
        }], vat_multiplier="18")

        self.assertEqual(
            result["pages"][0]["rows"][0]["unit_price_with_vat_status"],
            "correct",
        )

    def test_inclusive_total_uses_rounded_derived_unit_price(self):
        result = verify([{
            "rows": [{
                "quantity": "2",
                "unit_price_without_vat": "13728.81",
                "unit_price_with_vat_present": False,
            }],
        }], vat_multiplier="18")

        row = result["pages"][0]["rows"][0]
        self.assertEqual(row["unit_price_with_vat"], "16200.00")
        self.assertEqual(row["calculated_total_with_vat"], "32400.000")

    def test_bulk_totals_use_rounded_display_unit_prices(self):
        result = verify([{
            "rows": [{
                "quantity": "3",
                "bulk_quantity": "2",
                "bulk_price_without_vat": "1.01",
            }],
        }], vat_multiplier="18")

        row = result["pages"][0]["rows"][0]
        self.assertEqual(row["unit_price_without_vat"], "0.51")
        self.assertEqual(row["calculated_total_without_vat"], "1.53")
        self.assertEqual(row["unit_price_with_vat"], "0.60")
        self.assertEqual(row["calculated_total_with_vat"], "1.800")

    def test_edited_bulk_exclusive_price_updates_both_price_chains(self):
        result = verify([{
            "rows": [{
                "quantity": "2",
                "bulk_quantity": "10",
                "bulk_price_without_vat": "100",
                "bulk_price_with_vat": "90",
                "edited_price_key": "bulk_price_without_vat",
                "generated_price_fields": [
                    "bulk_price_with_vat",
                    "document_total",
                    "document_total_without_vat",
                ],
                "quote_options": [
                    {"bulk_quantity": "10", "bulk_price_without_vat": "1"},
                ],
            }],
        }], vat_multiplier="18")

        row = result["pages"][0]["rows"][0]
        self.assertEqual(row["bulk_price_with_vat"], "118.00")
        self.assertEqual(row["unit_price_without_vat"], "10.00")
        self.assertEqual(row["unit_price_with_vat"], "11.80")
        self.assertEqual(row["calculated_total_without_vat"], "20.00")
        self.assertEqual(row["calculated_total_with_vat"], "23.600")
        self.assertEqual(row["bulk_price_with_vat_status"], "calculated")
        self.assertEqual(row["document_total_status"], "calculated")
        self.assertEqual(row["document_total_without_vat_status"], "calculated")


if __name__ == "__main__":
    unittest.main()
