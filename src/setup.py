"""Manage two ZCode hook registrations; keep unrelated user settings."""

import argparse
import fcntl
import json
import shlex
import sys
import tempfile
import time
from pathlib import Path

SOURCE = Path(__file__).with_name("review.py")
EVENTS = (("PostToolUse", "Edit|Write|Bash", "posttool"), ("Stop", "*", "stop"))
AGENT_RULE = """<!-- zcode-plan-review:begin -->
При сохранении spec/plan дождись результата автоматического ревью. Запусти
`python3 ~/.zcode/hooks/zcode-plan-review/review.py status "$PWD"` из корня проекта.
Сверь актуальный хеш, сообщи вердикт вместе с итогом. При NEEDS_CHANGES исправь
существенные замечания. После третьего отказа или ошибки сообщи результат.
Повторный Stop может разрешить завершение без одобрения.
<!-- zcode-plan-review:end -->"""


def atomic(path, data):
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        tmp = Path(f.name)
        f.write(data)
    try:
        tmp.replace(path)
    finally:
        tmp.unlink(missing_ok=True)


def configure(base, installing, migrate=False):
    cli = base / "cli"
    if not installing and not cli.exists():
        return
    cli.mkdir(parents=True, exist_ok=True)
    with (cli / ".plan-review.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        update(base, installing, migrate)


def update(base, installing, migrate):
    config = base / "cli/config.json"
    package = base / "hooks/zcode-plan-review"
    script = package / "review.py"
    marker = package / "managed.json"
    if package.exists() and not marker.exists():
        raise ValueError(f"Unmanaged directory: {package}")
    if not installing and not marker.exists():
        return
    old = json.loads(marker.read_text()) if marker.exists() else {}
    data = json.loads(config.read_text()) if config.exists() else {}
    hooks = data.setdefault("hooks", {})
    if installing and hooks.get("enabled") is False:
        raise ValueError("ZCode hooks are disabled in config.json")
    events = hooks.setdefault("events", {})
    if installing:
        hooks.setdefault("enabled", True)
    commands = {}
    for event, matcher, action in EVENTS:
        groups = events.get(event, [])
        cmd = shlex.join([sys.executable, str(script), action])
        commands[action] = cmd
        for group in list(groups):
            for entry in list(group["hooks"]):
                existing = entry.get("command", "")
                legacy = existing == f'python3 "$HOME/.zcode/hooks/superpowers-review.py" {action}'
                if legacy and installing and not migrate:
                    raise ValueError(
                        "Existing standalone hook. See README and use --migrate-legacy."
                    )
                if existing in (cmd, old.get(action)) or (legacy and installing and migrate):
                    group["hooks"].remove(entry)
                    if not group["hooks"]:
                        groups.remove(group)
        if installing:
            group = next((g for g in groups if g.get("matcher") == matcher), None)
            if group is None:
                group = {"matcher": matcher, "hooks": []}
                groups.append(group)
            group["hooks"].append({"type": "command", "command": cmd, "timeout": 10})
            events[event] = groups
        elif not groups:
            events.pop(event, None)

    instructions = base / "AGENTS.md"
    original = instructions.read_text() if instructions.exists() else ""
    start, end = AGENT_RULE.splitlines()[0], AGENT_RULE.splitlines()[-1]
    if start in original or end in original:
        if (
            original.count(start) != 1
            or original.count(end) != 1
            or original.index(start) > original.index(end)
        ):
            raise ValueError("Invalid managed block in AGENTS.md")
        prefix, tail = original.split(start)
        suffix = tail.split(end, 1)[1]
        if not installing and suffix.startswith("\n"):
            suffix = suffix[1:]
        updated = prefix + (AGENT_RULE if installing else "") + suffix
    else:
        updated = original
        if installing:
            updated += ("" if not original or original.endswith("\n") else "\n") + AGENT_RULE + "\n"
    if installing:
        package.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic(marker, json.dumps(commands).encode())
        state = package / ".spr-state.json"
        if not state.exists():
            atomic(state, json.dumps({"_meta": {"installed_at": time.time()}}).encode())
        atomic(script, SOURCE.read_bytes())
    if config.exists() and json.loads(config.read_text()) != data:
        backup = config.with_name(f"config.plan-review-{time.time_ns()}.bak")
        atomic(backup, config.read_bytes())
    atomic(config, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode())
    if original != updated:
        atomic(instructions, updated.encode())
    if not installing:
        script.unlink(missing_ok=True)
        # Keep ownership and reports so reinstall can reuse the state.


def command(action):
    parser = argparse.ArgumentParser()
    parser.add_argument("adapter", choices=["zcode"])
    if action == "install":
        parser.add_argument("--migrate-legacy", action="store_true")
    args = parser.parse_args()
    try:
        configure(
            Path.home() / ".zcode", action == "install", getattr(args, "migrate_legacy", False)
        )
    except (ValueError, OSError) as exc:
        parser.exit(1, f"{action}: {exc}\n")
    print(f"{action}: OK. Start a new ZCode session.")
