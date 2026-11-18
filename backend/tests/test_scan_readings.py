import asyncio
import io
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from starlette.datastructures import UploadFile

import main


class ScanReadingsTests(unittest.TestCase):
    def test_scan_reads_each_page_once_and_verifies_rows(self):
        readings = [
            {"rows": [{"item": f"Page {page} reading", "quantity": "1", "unit_price_without_vat": "10"}], "warnings": []}
            for page in (1, 2)
        ]

        with (
            patch.dict(os.environ, {"GEMINI_API_KEY": "test-key"}, clear=False),
            patch("main.convert_to_images", return_value=[b"page-1", b"page-2"]),
            patch("main.recognize_image", new_callable=AsyncMock, side_effect=readings) as recognize,
        ):
            result = asyncio.run(main.scan([
                UploadFile(filename="schedule.jpg", file=io.BytesIO(b"image"))
            ]))

        self.assertEqual(recognize.call_count, 2)
        self.assertEqual([call.args[0] for call in recognize.call_args_list], [b"page-1", b"page-2"])
        self.assertEqual(len(result["pages"]), 2)
        for page_index, page in enumerate(result["pages"], start=1):
            self.assertEqual(page["filename"], "schedule.jpg")
            self.assertEqual(page["rows"][0]["item"], f"Page {page_index} reading")
            self.assertEqual(page["rows"][0]["calculated_total_without_vat"], "10.00")

    def test_recognize_image_falls_back_across_api_keys_and_models(self):
        valid_payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps(
                                    {
                                        "rows": [{"item": "Fallback Item", "quantity": "2", "unit_price_without_vat": "50"}],
                                        "document_grand_total": None,
                                        "document_grand_total_without_vat": "100.00",
                                        "warnings": [],
                                    }
                                )
                            }
                        ]
                    }
                }
            ]
        }

        def make_response(status_code: int, body: dict | str):
            resp = MagicMock()
            resp.status_code = status_code
            if isinstance(body, dict):
                resp.json.return_value = body
                resp.text = json.dumps(body)
            else:
                resp.text = body
            return resp

        # 3 keys x 3 models: fail all 3 keys on model 1 (429/503), fail key 1 on model 2, succeed on key 2 of model 2
        responses = [
            make_response(429, "Quota exceeded for key 1 on gemini-3.6-flash"),
            make_response(429, "Quota exceeded for key 2 on gemini-3.6-flash"),
            make_response(503, "Model busy on gemini-3.6-flash"),
            make_response(429, "Quota exceeded for key 1 on gemini-3.8-flash"),
            make_response(200, valid_payload),
        ]

        post_mock = AsyncMock(side_effect=responses)
        mock_client = AsyncMock()
        mock_client.post = post_mock
        mock_client.__aenter__.return_value = mock_client
        mock_client.__aexit__.return_value = False

        env = {
            "GEMINI_API_KEYS": "key-1,key-2,key-3",
            "GEMINI_MODELS": "gemini-3.6-flash,gemini-3.8-flash,gemini-3.5-flash-lite",
        }
        with (
            patch.dict(os.environ, env, clear=True),
            patch("main.httpx.AsyncClient", return_value=mock_client),
        ):
            result = asyncio.run(main.recognize_image(b"sample-image"))

        self.assertEqual(result["rows"][0]["item"], "Fallback Item")
        self.assertEqual(post_mock.call_count, 5)
        called_attempts = [
            (call.args[0].split("/models/")[1].split(":")[0], call.kwargs["params"]["key"])
            for call in post_mock.call_args_list
        ]
        self.assertEqual(
            called_attempts,
            [
                ("gemini-3.6-flash", "key-1"),
                ("gemini-3.6-flash", "key-2"),
                ("gemini-3.6-flash", "key-3"),
                ("gemini-3.8-flash", "key-1"),
                ("gemini-3.8-flash", "key-2"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
