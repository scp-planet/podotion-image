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

    def test_first_image_preflight_and_lazy_credentials_are_explicit(self) -> None:
        self.assertIn("Before the first generate or edit action in every new task", self.skill)
        self.assertIn("run `configure_direct.py --check` and `manage.py status`", self.skill)
        self.assertIn("local and read-only", self.skill)
        self.assertIn("once per task", self.skill)
        self.assertIn("valid default-only config remains ready for non-4K actions", self.skill)
        self.assertIn("inline `PodotionImageSk=<value>`", self.skill)
        self.assertIn("`PodotionImage4kSk=<value>`", self.skill)
        self.assertIn("UTF-8 text attachment", self.skill)
        self.assertIn("--input-file <path>", self.skill)

    def test_credential_input_security_and_restart_boundary_are_explicit(self) -> None:
        self.assertIn("avoids placing the literal key in the chat prompt or process argv", self.skill)
        self.assertIn("still become input to the current Codex session", self.skill)
        self.assertIn("do not describe it as an out-of-session secret channel", self.skill)
        self.assertIn("After any successful credential write, run non-billable", self.skill)
        self.assertIn("After configuration and any accepted migration finish, stop", self.skill)
        self.assertIn("do not migrate or generate", self.skill)

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

    def test_single_request_and_serial_multi_image_contract_are_explicit(self) -> None:
        self.assertIn("requests exactly one image with `n=1`", self.skill)
        self.assertIn("exactly one top-level Images API `data[]` item", self.skill)
        self.assertIn("explicit count from 1 through 10", self.skill)
        self.assertIn("Never launch these actions in parallel", self.skill)
        self.assertIn("second and later actions", self.skill)
        self.assertIn("`--force-new`", self.skill)
        self.assertIn("Stop immediately after any failure", self.skill)
        self.assertIn("Preserve earlier successful images", self.skill)

    def test_lifecycle_and_non_billable_doctor_are_explicit(self) -> None:
        self.assertIn("manage.py update --dry-run", self.skill)
        self.assertIn("manage.py uninstall-legacy-plugin --yes", self.skill)
        self.assertIn("manage.py uninstall --yes", self.skill)
        self.assertIn("restart Codex and create a new task", self.skill)
        self.assertIn("Never run `--image-probe` without explicit authorization", self.skill)
        self.assertIn("Do not run it before each ordinary image action", self.skill)
        self.assertIn("Do not ask the user to run `codex plugin remove`", self.skill)
        self.assertIn("Ask again on the first image action of every new task", self.skill)
        self.assertIn("a refusal applies only to the current task", self.skill)
        self.assertIn("`legacy_plugin.safe_to_remove` are both true", self.skill)
        self.assertIn("do not ask for cleanup confirmation", self.skill)
        self.assertIn("ask for the required assignment lines and the migration yes/no decision in one response", self.skill)
        doctor = self.skill.index("run non-billable `podotion_image.py doctor`")
        cleanup = self.skill.index("Only when that current doctor succeeds", doctor)
        self.assertLess(doctor, cleanup)
        self.assertIn("After update, migration, uninstall, or completed credential onboarding, stop", self.skill)

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
            "PodotionImageSk=<value>",
            "UTF-8",
            "$skill-installer",
            "request-status",
            "update --dry-run",
            "uninstall-legacy-plugin --yes",
            "uninstall --yes",
            "restart",
            "严格串行",
            "最多串行生成 10 张",
            "completed_unusable",
        ):
            with self.subTest(marker=marker):
                self.assertIn(marker, combined)


if __name__ == "__main__":
    unittest.main()
