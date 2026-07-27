from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.support import PNG_B64, PNG_BYTES, SCRIPT_PATH, parse_multipart_request


def load_module():
    spec = importlib.util.spec_from_file_location("podotion_image_images", SCRIPT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import {SCRIPT_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class ImagesPayloadTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.module = load_module()

    def test_generation_payload_targets_gpt_image_2_without_response_format(self) -> None:
        payload = self.module.build_images_generation_payload("draw a cat", "1152x2048")

        self.assertEqual(
            payload,
            {
                "model": "gpt-image-2",
                "prompt": "draw a cat",
                "size": "1152x2048",
                "quality": "auto",
                "output_format": "png",
                "n": 1,
            },
        )
        self.assertNotIn("stream", payload)

    def test_edit_multipart_repeats_image_array_parts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "first.png"
            second = root / "second.png"
            first.write_bytes(PNG_BYTES)
            second.write_bytes(PNG_BYTES)
            body, content_type = self.module.build_images_edit_multipart(
                "make both blue", [first, second], "1152x2048"
            )

        parts = parse_multipart_request(
            {"content_type": content_type, "raw_body": body}
        )
        fields = {
            part["name"]: part["data"].decode("utf-8")
            for part in parts
            if part["filename"] is None
        }
        images = [part for part in parts if part["name"] == "image[]"]

        self.assertEqual(fields["model"], "gpt-image-2")
        self.assertEqual(fields["prompt"], "make both blue")
        self.assertEqual(fields["size"], "1152x2048")
        self.assertEqual(fields["quality"], "auto")
        self.assertEqual(fields["output_format"], "png")
        self.assertEqual(fields["n"], "1")
        self.assertNotIn("stream", fields)
        self.assertEqual(len(images), 2)
        self.assertTrue(all(part["content_type"] == "image/png" for part in images))
        self.assertTrue(all(part["data"] == PNG_BYTES for part in images))
        self.assertFalse(any(part["name"] == "input_fidelity" for part in parts))

    def test_images_api_data_response_is_reused_by_result_extractor(self) -> None:
        results = self.module.extract_image_results(
            {"created": 1710000000, "data": [{"b64_json": PNG_B64}]}
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "base64")
        self.assertEqual(results[0].value, PNG_B64)
        self.assertEqual(results[0].source, "$.data[0].b64_json")

    def test_b64_json_takes_precedence_over_url_in_the_same_item(self) -> None:
        results = self.module.extract_image_results(
            {
                "data": [
                    {
                        "b64_json": PNG_B64,
                        "url": "https://example.com/the-same-image.png",
                    }
                ]
            }
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].value, PNG_B64)
        self.assertEqual(results[0].kind, "base64")
        self.assertEqual(results[0].source, "$.data[0].b64_json")

    def test_url_is_used_only_when_b64_json_is_absent(self) -> None:
        results = self.module.extract_image_results(
            {"data": [{"url": "https://example.com/generated.png"}]}
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].kind, "url")
        self.assertEqual(results[0].source, "$.data[0].url")

    def test_nonstandard_or_non_single_image_responses_are_rejected(self) -> None:
        cases = {
            "top-level-result": {"b64_json": PNG_B64},
            "images-wrapper": {"images": [{"b64_json": PNG_B64}]},
            "nested-response": {"response": {"data": [{"b64_json": PNG_B64}]}},
            "empty-data": {"data": []},
            "multiple-data": {
                "data": [{"b64_json": PNG_B64}, {"b64_json": PNG_B64}]
            },
            "non-object-item": {"data": [PNG_B64]},
            "missing-image-field": {"data": [{"result": "completed"}]},
        }

        for name, payload in cases.items():
            with self.subTest(case=name), self.assertRaisesRegex(
                RuntimeError, "ImagesResponse"
            ):
                self.module.extract_image_results(payload)

    def test_single_result_is_decoded_and_saved_once(self) -> None:
        results = self.module.extract_image_results({"data": [{"b64_json": PNG_B64}]})
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            with mock.patch.object(
                self.module,
                "_decode_image_result",
                wraps=self.module._decode_image_result,
            ) as decode:
                saved, warnings = self.module.save_image_results(results, output)
            files = [path for path in output.iterdir() if path.is_file()]
            saved_bytes = saved[0].path.read_bytes()
            saved_path_matches = files[0].samefile(saved[0].path)

        decode.assert_called_once_with(results[0], self.module.TIMEOUT_SECONDS)
        self.assertEqual(len(saved), 1)
        self.assertEqual(saved_bytes, PNG_BYTES)
        self.assertEqual(len(files), 1)
        self.assertTrue(saved_path_matches)
        self.assertRegex(saved[0].path.name, r"^\d{8}_\d{6}_\d{6}\.png$")
        self.assertEqual(warnings, [])

    def test_invalid_single_result_leaves_no_files_and_has_safe_details(self) -> None:
        results = self.module.extract_image_results({"data": [{"b64_json": "bad!"}]})
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            with self.assertRaises(self.module.ImageCandidateError) as raised:
                self.module.save_image_results(results, output)
            leftovers = list(output.iterdir())

        self.assertEqual(leftovers, [])
        self.assertEqual(raised.exception.details["candidate_count"], 1)
        self.assertEqual(len(raised.exception.details["invalid_candidates"]), 1)
        self.assertNotIn("bad!", str(raised.exception.details))

    def test_commit_failure_removes_the_staged_single_image(self) -> None:
        results = self.module.extract_image_results({"data": [{"b64_json": PNG_B64}]})
        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module.os, "replace", side_effect=OSError("simulated commit failure")
        ):
            output = Path(temp_dir)
            with self.assertRaisesRegex(RuntimeError, "atomically save the decoded image"):
                self.module.save_image_results(results, output)
            leftovers = list(output.iterdir())

        self.assertEqual(leftovers, [])

    def test_size_mismatch_is_a_structured_nonfatal_warning(self) -> None:
        image = self.module.SavedImage(
            path=Path("generated.png"),
            mime_type="image/png",
            bytes=123,
            width=941,
            height=1672,
        )
        warnings = self.module.image_size_warnings("1152x2048", [image])

        self.assertEqual(
            warnings,
            [
                {
                    "code": "image_size_mismatch",
                    "message": "provider returned different pixel dimensions than requested",
                    "image_index": 1,
                    "requested_size": "1152x2048",
                    "actual_size": "941x1672",
                }
            ],
        )


if __name__ == "__main__":
    unittest.main()
