import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


class CodexAppIntegrationTest(unittest.TestCase):
    def test_every_skill_has_visible_codex_app_metadata(self) -> None:
        skill_dirs = sorted(
            path.parent for path in (REPO_ROOT / "skills").glob("*/SKILL.md")
        )
        self.assertTrue(skill_dirs)

        for skill_dir in skill_dirs:
            with self.subTest(skill=skill_dir.name):
                metadata_path = skill_dir / "agents" / "openai.yaml"
                icon_path = skill_dir / "assets" / "gradient-ascent.svg"
                self.assertTrue(metadata_path.is_file())
                self.assertTrue(icon_path.is_file())
                metadata = metadata_path.read_text(encoding="utf-8")
                self.assertIn('icon_small: "./assets/gradient-ascent.svg"', metadata)
                self.assertIn('icon_large: "./assets/gradient-ascent.svg"', metadata)
                self.assertIn('brand_color: "#2F6F4E"', metadata)
                icon = icon_path.read_text(encoding="utf-8")
                self.assertIn('fill="#2F6F4E"', icon)
                self.assertIn(">G</text>", icon)
                self.assertNotIn(">C</text>", icon)


if __name__ == "__main__":
    unittest.main()
