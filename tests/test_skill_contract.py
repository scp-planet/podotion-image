from __future__ import annotations

import json
import unittest
from pathlib import Path


SKILL_ROOT = (
    Path(__file__).resolve().parents[1] / "skills" / "podotion-image"
)


class SkillContractTests(unittest.TestCase):
    def test_output_location_is_resolved_from_user_language(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("Resolve the user's save-location intent", skill)
        self.assertIn("An explicit absolute directory", skill)
        self.assertIn(
            "A relative directory is resolved from the active project workspace",
            skill,
        )
        self.assertIn("<workspace>/PodotionImage", skill)
        self.assertIn("If multiple directories are plausible, ask before calling", skill)
        self.assertIn("Do not resolve paths from the plugin installation directory", skill)

    def test_success_contract_includes_outputs_fallback(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("images[].markdown_path", skill)
        self.assertIn("separate absolute-path file link", skill)
        self.assertIn("outputs_registered", skill)
        self.assertIn("expect `resource_uri` only when registration succeeded", skill)
        self.assertIn("registration fails completely", skill)
        self.assertIn("without changing `ok: true`", skill)
        self.assertIn("Do not call `generate` or `edit` again", skill)

    def test_request_identity_and_single_post_contract_are_explicit(self) -> None:
        skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("new UUID `request_key`", skill)
        self.assertIn("stable `state_scope`", skill)
        self.assertIn("fixed 600-second provider timeout", skill)
        self.assertIn("one upstream POST", skill)
        self.assertIn("call `request_status`", skill)
        self.assertIn("explicit user acknowledgement", skill)
        self.assertIn("not retried", skill)

    def test_path_and_output_failure_evals_are_present(self) -> None:
        payload = json.loads(
            (SKILL_ROOT / "evals" / "evals.json").read_text(encoding="utf-8")
        )
        prompts = {item["id"]: item["prompt"] for item in payload["evals"]}

        self.assertIn("assets/generated", prompts[8])
        self.assertIn("conversation workspace", prompts[9])
        self.assertIn("assets/images", prompts[10])
        self.assertIn("/srv/site/static/renders", prompts[11])


if __name__ == "__main__":
    unittest.main()
