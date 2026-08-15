import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from gradient_ascent.storage import write_json, write_text


class StorageTest(unittest.TestCase):
    def test_failed_json_write_preserves_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan" / "athlete.json"
            path.parent.mkdir(parents=True)
            original = '{"safe": true}\n'
            path.write_text(original, encoding="utf-8")

            with patch("gradient_ascent.storage.json.dump", side_effect=RuntimeError("interrupted")):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    write_json(path, {"safe": False})

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])

    def test_failed_text_write_preserves_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plan" / "goals.md"
            path.parent.mkdir(parents=True)
            original = "# Original goals\n"
            path.write_text(original, encoding="utf-8")

            with patch("gradient_ascent.storage.os.fsync", side_effect=RuntimeError("interrupted")):
                with self.assertRaisesRegex(RuntimeError, "interrupted"):
                    write_text(path, "# Replacement goals\n")

            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*")), [])


if __name__ == "__main__":
    unittest.main()
