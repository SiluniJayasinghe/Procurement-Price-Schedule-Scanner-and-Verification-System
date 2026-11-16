import asyncio
import io
import os
import unittest
from unittest.mock import AsyncMock, patch

from starlette.datastructures import UploadFile

import main


class ScanReadingsTests(unittest.TestCase):
    def test_scan_keeps_three_readings_per_page_and_defaults_to_first(self):
        readings = [
            {"rows": [{"item": f"Page {page} reading {reading}", "quantity": "1", "unit_price_without_vat": "10"}], "warnings": []}
            for page in (1, 2)
            for reading in (1, 2, 3)
        ]

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}),
            patch("main.convert_to_images", return_value=[b"page-1", b"page-2"]),
            patch("main.recognize_image", new_callable=AsyncMock, side_effect=readings) as recognize,
        ):
            result = asyncio.run(main.scan([
                UploadFile(filename="schedule.jpg", file=io.BytesIO(b"image"))
            ]))

        self.assertEqual(recognize.call_count, 6)
        self.assertEqual([call.args[0] for call in recognize.call_args_list], [b"page-1"] * 3 + [b"page-2"] * 3)
        self.assertEqual(len(result["pages"]), 2)
        for page_index, page in enumerate(result["pages"], start=1):
            self.assertEqual(page["selected_reading_index"], 0)
            self.assertEqual(len(page["ocr_readings"]), 3)
            self.assertEqual(page["rows"][0]["item"], f"Page {page_index} reading 1")
            self.assertEqual(
                [reading["rows"][0]["item"] for reading in page["ocr_readings"]],
                [f"Page {page_index} reading {reading}" for reading in (1, 2, 3)],
            )


if __name__ == "__main__":
    unittest.main()
