import json
import tempfile
import unittest
from pathlib import Path

from src.review import parse_verdict
from src.setup import AGENT_RULE, configure


class InstallTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.config = self.base / "cli/config.json"
        self.config.parent.mkdir()
        self.instructions = self.base / "AGENTS.md"
        self.instructions.write_text("Team notes\n")
        self.other = {
            "matcher": "Edit|Write|Bash",
            "hooks": [{"type": "command", "command": "backup"}],
        }
        self.initial = {
            "hooks": {"enabled": True, "events": {"PostToolUse": [self.other]}},
            "ui": {"locale": "en-US"},
        }
        self.config.write_text(json.dumps(self.initial))

    def test_reinstall_and_uninstall(self):
        configure(self.base, True)
        first = self.config.read_bytes()
        configure(self.base, True)
        self.assertEqual(self.config.read_bytes(), first)
        self.assertEqual(self.instructions.read_text().count(AGENT_RULE), 1)
        self.assertTrue(list(self.config.parent.glob("*.bak")))
        config = json.loads(first)
        config["ui"]["theme"] = "dark"
        self.config.write_text(json.dumps(config))
        configure(self.base, False)
        configure(self.base, False)
        self.initial["ui"]["theme"] = "dark"
        self.assertEqual(json.loads(self.config.read_text()), self.initial)
        self.assertEqual(self.instructions.read_text(), "Team notes\n")

    def test_disabled_and_invalid_configs_are_preserved(self):
        for content in ('{"hooks":{"enabled":false}}', "{bad"):
            self.config.write_text(content)
            with self.assertRaises(ValueError):
                configure(self.base, True)
            self.assertEqual(self.config.read_text(), content)

    def test_legacy_requires_explicit_migration(self):
        legacy = 'python3 "$HOME/.zcode/hooks/superpowers-review.py" posttool'
        self.other["hooks"].append({"type": "command", "command": legacy})
        self.config.write_text(json.dumps(self.initial))
        with self.assertRaisesRegex(ValueError, "migrate-legacy"):
            configure(self.base, True)
        configure(self.base, True, migrate=True)
        self.assertNotIn("superpowers-review.py", self.config.read_text())
        self.assertIn("backup", self.config.read_text())

    def test_final_verdict(self):
        self.assertEqual(
            parse_verdict("VERDICT: APPROVE\nVERDICT: NEEDS_CHANGES\n"), "NEEDS_CHANGES"
        )
        self.assertIsNone(parse_verdict("VERDICT: APPROVE\nreview incomplete"))
