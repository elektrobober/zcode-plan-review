"""Exercise the installed Stop handler with real child processes and no API."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from src.setup import configure


class StopTests(unittest.TestCase):
    def test_missed_write_fix_cycle_and_review_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            base = root / "profile"
            configure(base, True)
            package = base / "hooks/zcode-plan-review"
            doc = root / "docs/superpowers/specs/demo.md"
            doc.parent.mkdir(parents=True)
            fake = root / "codex"
            fake.write_text(
                "#!/bin/sh\n"
                'while [ "$1" != -o ]; do shift; done\n'
                "doc=docs/superpowers/specs/demo.md\n"
                'cat "$doc" >> calls\n'
                'case "$(cat "$doc")" in\n'
                "  error) exit 1 ;;\n"
                "  accept) verdict=APPROVE ;;\n"
                "  *) verdict=NEEDS_CHANGES ;;\n"
                'esac\nprintf "## Существенные проблемы\\nissue\\nVERDICT: %s\\n" "$verdict" > "$2"\n'
            )
            fake.chmod(0o700)
            env = dict(os.environ, PATH=str(root) + os.pathsep + os.environ["PATH"])

            def stop(soft=False):
                return subprocess.run(
                    [sys.executable, str(package / "review.py"), "stop"],
                    input=json.dumps({"cwd": str(root), "stop_hook_active": soft}),
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                )

            doc.write_text("old")
            os.utime(doc, (1, 1))
            self.assertEqual(stop().returncode, 0)
            for content, expected in [
                ("one", 1),
                ("two", 2),
                ("three", 3),
                ("accept", 0),
                ("error", 0),
            ]:
                doc.write_text(content)
                self.assertEqual(stop().returncode, 2)
                deadline = time.monotonic() + 5
                while True:
                    record = json.loads((package / ".spr-state.json").read_text())[str(doc)]
                    if record["status"] != "running" or time.monotonic() > deadline:
                        break
                    time.sleep(0.02)
                self.assertEqual(record["status"], "error" if content == "error" else "done")
                self.assertEqual(record["rounds"], expected)
                result = stop()
                self.assertEqual(result.returncode, 0 if content == "accept" else 2)
                if expected == 3:
                    self.assertIn("лимит", result.stderr)
                if content == "error":
                    self.assertIn("rc=1", result.stderr)
                self.assertEqual(stop(soft=True).returncode, 0)
            self.assertEqual((root / "calls").read_text(), "onetwothreeaccepterror")
