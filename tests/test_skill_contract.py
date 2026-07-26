from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL_ROOT = ROOT / "skills" / "podotion-image"


class SkillContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.skill = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    def test_frontmatter_has_only_supported_skill_fields(self) -> None:
        _, frontmatter, _ = self.skill.split("---", 2)
        keys = {
            line.split(":", 1)[0].strip()
            for line in frontmatter.splitlines()
            if ":" in line
        }
        self.assertEqual(keys, {"name", "description"})

    def test_standalone_runtime_replaces_mcp_contract(self) -> None:
        self.assertIn("bundled standalone Python scripts", self.skill)
        self.assertIn("Do not look for Plugin or MCP tools", self.skill)
        self.assertIn("podotion_image.py generate", self.skill)
        self.assertIn("podotion_image.py edit", self.skill)
        self.assertIn("Send the prompt through stdin", self.skill)
        self.assertNotIn("call the Podotion MCP", self.skill)

    def test_exact_dimensions_and_dual_key_routing_are_explicit(self) -> None:
        self.assertIn("PodotionImageSk", self.skill)
        self.assertIn("PodotionImage4kSk", self.skill)
        self.assertIn("8,294,400", self.skill)
        self.assertIn("exact `2560x1440` uses the default profile", self.skill)
        self.assertIn("explicit 4K and `3840x2160` use the 4K profile", self.skill)
        self.assertIn("Never fall back to the default key", self.skill)
        self.assertIn("Do not silently map valid exact dimensions to a tier", self.skill)

    def test_output_and_request_safety_contracts_are_explicit(self) -> None:
        self.assertIn("stable `state_scope`", self.skill)
        self.assertIn("new UUID `request_key`", self.skill)
        self.assertIn("one upstream POST", self.skill)
        self.assertIn("request-status", self.skill)
        self.assertIn("--acknowledge-possible-charge", self.skill)
        self.assertIn("<workspace>/PodotionImageOutput", self.skill)
        self.assertIn("images[].markdown_path", self.skill)
        self.assertIn("separate absolute local file link", self.skill)
        self.assertIn("does not register MCP `resource_link`", self.skill)

    def test_lifecycle_and_non_billable_doctor_are_explicit(self) -> None:
        self.assertIn("manage.py update --dry-run", self.skill)
        self.assertIn("manage.py uninstall-legacy-plugin --yes", self.skill)
        self.assertIn("manage.py uninstall --yes", self.skill)
        self.assertIn("restart Codex and create a new task", self.skill)
        self.assertIn("Never run `--image-probe` without explicit authorization", self.skill)
        self.assertIn("Do not run it before each ordinary image action", self.skill)
        self.assertIn("Do not ask the user to run `codex plugin remove`", self.skill)

    def test_agents_metadata_is_minimal_and_mentions_the_skill(self) -> None:
        metadata = (SKILL_ROOT / "agents" / "openai.yaml").read_text(encoding="utf-8")
        self.assertIn('display_name: "Podotion Image"', metadata)
        self.assertIn("short_description:", metadata)
        self.assertIn("$podotion-image", metadata)
        self.assertNotIn("dependencies:", metadata)

    def test_evals_cover_routing_recovery_and_lifecycle(self) -> None:
        payload = json.loads((SKILL_ROOT / "evals" / "evals.json").read_text(encoding="utf-8"))
        combined = json.dumps(payload, ensure_ascii=False)
        for marker in (
            "2560x1440",
            "PodotionImage4kSk",
            "request-status",
            "update --dry-run",
            "uninstall-legacy-plugin --yes",
            "uninstall --yes",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, combined)


if __name__ == "__main__":
    unittest.main()
