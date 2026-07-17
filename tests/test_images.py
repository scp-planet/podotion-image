from __future__ import annotations

import base64
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

    def test_text_result_beside_valid_image_is_not_a_candidate(self) -> None:
        results = self.module.extract_image_results(
            {
                "data": [
                    {"b64_json": PNG_B64, "result": "completed"},
                    {"result": "this is metadata, not image base64"},
                ]
            }
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].value, PNG_B64)

    def test_nonstandard_top_level_result_fields_are_rejected(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Images API"):
            self.module.extract_image_results(
                {"b64_json": PNG_B64, "result": "completed"}
            )

    def test_valid_candidate_survives_invalid_declared_candidate_with_warning(self) -> None:
        results = self.module.extract_image_results(
            {
                "data": [
                    {"b64_json": PNG_B64},
                    {"b64_json": "not-image-base64"},
                ]
            }
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            saved, warnings = self.module.save_image_results(results, output)
            files = [path for path in output.iterdir() if path.is_file()]
            saved_bytes = saved[0].path.read_bytes()
            saved_path_matches = files[0].samefile(saved[0].path)

        self.assertEqual(len(saved), 1)
        self.assertEqual(saved_bytes, PNG_BYTES)
        self.assertEqual(len(files), 1)
        self.assertTrue(saved_path_matches)
        self.assertNotIn("_01", saved[0].path.name)
        self.assertEqual(len(warnings), 1)
        self.assertEqual(warnings[0]["code"], "invalid_image_candidate")
        self.assertEqual(warnings[0]["result_index"], 2)
        self.assertEqual(warnings[0]["candidate_count"], 2)
        self.assertEqual(warnings[0]["source"], "$.data[1].b64_json")
        self.assertEqual(warnings[0]["value_length"], len("not-image-base64"))
        self.assertNotIn("not-image-base64", str(warnings[0]))

    def test_all_invalid_candidates_leave_no_files_and_have_safe_details(self) -> None:
        results = self.module.extract_image_results(
            {"data": [{"b64_json": "bad!"}, {"url": "not-a-url"}]}
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir)
            with self.assertRaises(self.module.ImageCandidateError) as raised:
                self.module.save_image_results(results, output)
            leftovers = list(output.iterdir())

        self.assertEqual(leftovers, [])
        self.assertEqual(raised.exception.details["candidate_count"], 2)
        self.assertEqual(len(raised.exception.details["invalid_candidates"]), 2)
        self.assertNotIn("bad!", str(raised.exception.details))
        self.assertNotIn("not-a-url", str(raised.exception.details))

    def test_commit_failure_rolls_back_staged_and_committed_files(self) -> None:
        second_png = base64.b64encode(PNG_BYTES + b"trailing-test-data").decode("ascii")
        results = self.module.extract_image_results(
            {"data": [{"b64_json": PNG_B64}, {"b64_json": second_png}]}
        )
        real_replace = self.module.os.replace
        replace_calls = 0

        def fail_second_replace(source, destination):
            nonlocal replace_calls
            replace_calls += 1
            if replace_calls == 2:
                raise OSError("simulated commit failure")
            return real_replace(source, destination)

        with tempfile.TemporaryDirectory() as temp_dir, mock.patch.object(
            self.module.os, "replace", side_effect=fail_second_replace
        ):
            output = Path(temp_dir)
            with self.assertRaisesRegex(RuntimeError, "atomically save"):
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
