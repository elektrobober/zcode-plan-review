#!/usr/bin/env python3
"""Test-only codex executable, available only inside the temporary ZCode session."""

import hashlib
import os
import sys
import time
from pathlib import Path

if sys.argv[1:5] != ["exec", "-s", "read-only", "--color"]:
    sys.exit("Expected read-only Codex review")
root = Path(os.environ["SPR_FIXTURE_ROOT"])
if Path.cwd() != root:
    sys.exit("Unexpected review directory")
data = (root / "docs/superpowers/plans/live.md").read_bytes()
if data == b"Plan version one\n":
    (root / "first-worker").write_text(str(os.getppid()))
    (root / "v1-started").touch()
    for _ in range(300):
        if (root / "release-first").exists():
            break
        time.sleep(0.1)
    else:
        sys.exit("Second review never completed")
out = Path(sys.argv[sys.argv.index("-o") + 1])
out.write_text(f"sha256={hashlib.sha256(data).hexdigest()}\nVERDICT: APPROVE\n")
