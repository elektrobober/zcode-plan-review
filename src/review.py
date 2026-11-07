#!/usr/bin/env python3
"""ZCode plan review hook and detached worker for POSIX systems."""

import fcntl
import glob
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import uuid

HOOKS_DIR = os.path.dirname(os.path.realpath(__file__))
STATE_FILE = os.path.join(HOOKS_DIR, ".spr-state.json")
LOCK_FILE = os.path.join(HOOKS_DIR, ".spr-lock")
LOG_DIR = os.path.join(HOOKS_DIR, ".spr-logs")

MAX_ROUNDS = 3
RUN_TIMEOUT = 600  # codex exec wall-clock limit, seconds
STALE_RUNNING = 1200  # running entry older than this is considered dead

DOCS_RE = re.compile(r"docs/superpowers/(specs|plans)/[^/]+\.md$")


def now():
    return int(time.time())


# ---------- state ----------


def load_state():
    try:
        with open(STATE_FILE) as f:
            st = json.load(f)
    except FileNotFoundError:
        st = {}
    if "_meta" not in st:
        # True install moment is persisted at wiring time; this is a fallback.
        st["_meta"] = {"installed_at": now()}
    return st


def save_state(st):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


class StateLock:
    """Serialize read-modify-write cycles across hook invocations."""

    def __enter__(self):
        self.fh = open(LOCK_FILE, "w")
        fcntl.flock(self.fh, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        fcntl.flock(self.fh, fcntl.LOCK_UN)
        self.fh.close()


def scan_docs(root):
    """{abs_path: sha256} for every spec/plan under root. Cheap: few small md files."""
    out = {}
    for sub in ("specs", "plans"):
        for p in glob.glob(os.path.join(root, "docs", "superpowers", sub, "*.md")):
            try:
                with open(p, "rb") as f:
                    out[os.path.abspath(p)] = hashlib.sha256(f.read()).hexdigest()
            except OSError:
                pass
    return out


# ---------- review command ----------

PROMPT_TEMPLATE = (
    "Ты — критический ревьюер технических документов. Прочитай документ {DOC}.\n"
    "Это {KIND}. При необходимости сверяйся с кодом репозитория — он у тебя "
    "доступен только для чтения.\n\n"
    "Найди: противоречия с кодом репозитория, невыполнимые или непроверяемые шаги, "
    "пропущенные edge cases и сценарии отказа, ошибки в именах файлов/функций/сигнатур, "
    "тестовые дыры (что заявлено, но ничем не pinned).\n\n"
    "Ответь на языке документа. Строго в этом формате, блок вердикта — самым последним:\n\n"
    "## Существенные проблемы\n<нумерованный список; можно пусто>\n\n"
    "## Мелкие проблемы\n<нумерованный список; можно пусто>\n\n"
    "## Вкусовые замечания\n<нумерованный список; можно пусто>\n\n"
    "VERDICT: APPROVE | NEEDS_CHANGES\n"
    "APPROVE — только если существенных проблем нет. Мелкие и вкусовые не блокируют.\n"
)

# Verdict = the LAST line that is exactly a verdict. A reviewer quoting an old
# "VERDICT: APPROVE" mid-text must not override the final verdict.
VERDICT_LINE_RE = re.compile(r"^\s*VERDICT:\s*(APPROVE|NEEDS_CHANGES)\s*\.?\s*$", re.M)


def parse_verdict(text):
    lines = text.rstrip().splitlines()
    match = VERDICT_LINE_RE.fullmatch(lines[-1]) if lines else None
    return match.group(1) if match else None


def build_cmd(doc, out_file, prompt):
    return ["codex", "exec", "-s", "read-only", "--color", "never", "-o", out_file, prompt]


def launch_review(st, root, path, h):
    """Mark running and spawn the detached worker. Caller holds the state lock."""
    tag = uuid.uuid4().hex
    log = os.path.join(LOG_DIR, tag + ".log")
    out = os.path.join(LOG_DIR, tag + ".out")
    os.makedirs(LOG_DIR, exist_ok=True)
    prev = st.get(path, {})
    st[path] = {
        "root": root,
        "hash": h,
        "status": "running",
        "started": now(),
        "log": log,
        "out": out,
        "rounds": 0 if prev.get("rounds", 0) >= MAX_ROUNDS else prev.get("rounds", 0),
        "last_verdict": None,
    }
    with open(log, "ab") as lf:
        subprocess.Popen(
            [sys.executable, os.path.abspath(__file__), "run", path, h, root, out],
            stdout=lf,
            stderr=lf,
            start_new_session=True,
            cwd=root,
            stdin=subprocess.DEVNULL,
        )
    return st[path]


def sync_state(st, root, docs):
    """Reconcile on-disk docs with state; launch a review for everything new
    or changed. Returns (launched, dirty): launched = list of (path, hash),
    dirty = True if state was mutated at all (a baseline-only update must be
    persisted too). Caller holds the state lock and saves afterwards."""
    launched = []
    dirty = False
    for path, h in docs.items():
        entry = st.get(path)
        if entry is None:
            # Pre-existing doc: baseline it, don't burn an API call on history.
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                continue
            if mtime >= st["_meta"]["installed_at"]:
                launched.append((path, h))
                launch_review(st, root, path, h)
            else:
                st[path] = {
                    "root": root,
                    "hash": h,
                    "status": "baseline",
                    "rounds": 0,
                    "last_verdict": None,
                }
            dirty = True
            continue
        if entry.get("status") == "running":
            if entry.get("hash") == h:
                continue
            # Doc changed mid-run: the stale worker aborts on hash mismatch in
            # its final update; only a new worker can review the new content.
            launched.append((path, h))
            launch_review(st, root, path, h)
            dirty = True
            continue
        if entry.get("hash") != h:
            launched.append((path, h))
            launch_review(st, root, path, h)
            dirty = True
    return launched, dirty


# ---------- hooks ----------


def hook_posttool():
    payload = json.load(sys.stdin)
    root = os.path.realpath(payload.get("cwd") or os.getcwd())
    # A Write/Edit may target a doc outside the session cwd (absolute path,
    # other repo/worktree): derive the project root from the file path itself.
    fp = (payload.get("tool_input") or {}).get("file_path") or ""
    fp = os.path.join(root, fp)
    m = DOCS_RE.search(fp)
    if m:
        root = os.path.realpath(os.path.join(os.path.dirname(fp), "..", "..", ".."))
    if not os.path.isdir(os.path.join(root, "docs", "superpowers")):
        return
    with StateLock():
        docs = scan_docs(root)
        st = load_state()
        launched, dirty = sync_state(st, root, docs)
        if dirty:
            save_state(st)


def status_command(root):
    return shlex.join([sys.executable, __file__, "status", root])


def hook_stop():
    payload = json.load(sys.stdin)
    soft = bool(payload.get("stop_hook_active"))
    root = os.path.realpath(payload.get("cwd") or os.getcwd())
    if not os.path.isdir(os.path.join(root, "docs", "superpowers")):
        return
    with StateLock():
        docs = scan_docs(root)
        st = load_state()
        # Second chance: PostToolUse may have missed the write entirely
        # (unmatched tool, hooks enabled mid-session) — sync here too.
        launched, dirty = sync_state(st, root, docs)
        if dirty:
            save_state(st)
        if launched:
            names = ", ".join(os.path.relpath(p, root) for p, _ in launched)
            return block(
                soft,
                "Для %s запущено codex-ревью (новое или изменённое содержимое). "
                "Дождись вердикта (%s) "
                "и доложи. Не объявляй 'Plan complete' без вердикта."
                % (names, status_command(root)),
            )
        # Aggregate every blocking doc into one message: real flows change
        # spec and plan together, and the agent must see both at once.
        blocks = []
        for path, h in docs.items():
            entry = st.get(path)
            if not entry or entry.get("status") == "baseline":
                continue
            rel = os.path.relpath(path, root)
            if entry.get("status") == "running":
                if now() - entry.get("started", 0) > STALE_RUNNING:
                    entry["status"] = "error"
                    entry["error"] = "review timed out / worker stale"
                    save_state(st)
                else:
                    blocks.append(
                        "Codex-ревью %s ещё выполняется. Дождись вердикта "
                        "(%s), "
                        "затем доложи его." % (rel, status_command(root))
                    )
                    continue
            if entry.get("status") == "error":
                blocks.append(
                    "Codex-ревью %s упало с ошибкой: %s. Сообщи владельцу фактический "
                    "результат; 'Plan complete' без ревью не объявляй."
                    % (rel, entry.get("error", "unknown"))
                )
                continue
            if entry.get("hash") != h:
                continue  # defensive: sync above launches all mismatches
            if entry.get("last_verdict") == "APPROVE":
                continue
            # NEEDS_CHANGES for the current content, within the current fix cycle
            rounds = entry.get("rounds", 0)
            issues = extract_issues(entry.get("out"))
            if rounds < MAX_ROUNDS:
                blocks.append(
                    "Codex: NEEDS_CHANGES для %s (раунд %d/%d). Существенные замечания:\n%s\n"
                    "Внеси правки в документ — ревью перезапустится автоматически."
                    % (rel, rounds, MAX_ROUNDS, issues)
                )
            else:
                blocks.append(
                    "Codex: NEEDS_CHANGES для %s, лимит раундов (%d) исчерпан.\n"
                    "Последние существенные замечания:\n%s\n"
                    "Доложи владельцу вердикт и открытые замечания, затем завершай "
                    "(повторный стоп пройдёт)." % (rel, MAX_ROUNDS, issues)
                )
        if blocks:
            return block(soft, "\n---\n".join(blocks))


def block(soft, reason):
    """Exit-2 block (classic Claude Code semantics). Soft mode: inform, allow."""
    if soft:
        sys.stderr.write("SPR (мягко, повторный стоп): " + reason + "\n")
        sys.exit(0)
    sys.stderr.write(reason + "\n")
    sys.exit(2)


def extract_issues(out_file):
    try:
        with open(out_file) as f:
            text = f.read()
    except OSError:
        return "(текст ревью недоступен)"
    m = re.search(r"##\s*Существенные проблемы(.*?)(?:\n##\s|$)", text, re.S)
    body = (m.group(1) if m else "").strip()
    return body[:2000] if body else "(существенных нет — см. полное ревью: %s)" % out_file


# ---------- worker / cli ----------


def cmd_run(path, h, root, out_file):
    with StateLock():
        st = load_state()
        entry = st.get(path)
        if (
            not entry
            or entry.get("out") != out_file
            or entry.get("hash") != h
            or entry.get("status") != "running"
        ):
            return  # superseded or state lost; nothing to do
    kind = "spec" if "/specs/" in path else "implementation plan"
    prompt = PROMPT_TEMPLATE.replace("{DOC}", path).replace("{KIND}", kind)
    argv = build_cmd(path, out_file, prompt)
    try:
        rc = subprocess.call(
            argv, cwd=root, timeout=RUN_TIMEOUT, stdout=subprocess.DEVNULL, stderr=sys.stderr
        )
    except subprocess.TimeoutExpired:
        rc = 124
    except OSError as exc:
        print(exc, file=sys.stderr)
        rc = 127
    verdict = None
    try:
        with open(out_file) as f:
            verdict = parse_verdict(f.read())
    except OSError:
        pass
    with StateLock():
        st = load_state()
        entry = st.get(path)
        if not entry or entry.get("out") != out_file or entry.get("hash") != h:
            return  # doc changed again meanwhile; newer run owns the state
        if rc == 0 and verdict:
            rounds = entry.get("rounds", 0)
            if verdict == "APPROVE":
                rounds = 0  # fix cycle closed
            else:
                rounds += 1
            entry.update(
                status="done", verdict=verdict, last_verdict=verdict, rounds=rounds, finished=now()
            )
        else:
            entry.update(status="error", error="rc=%s verdict=%s" % (rc, verdict), finished=now())
        save_state(st)


def cmd_status(root):
    root = os.path.realpath(root or os.getcwd())
    st = load_state()
    for path, e in sorted(st.items()):
        if path == "_meta" or e.get("root") != root:
            continue
        print(
            "%s  hash=%s status=%s verdict=%s rounds=%s report=%s"
            % (
                os.path.relpath(path, root),
                e.get("hash", "")[:12],
                e.get("status"),
                e.get("last_verdict"),
                e.get("rounds"),
                e.get("out", "—"),
            )
        )


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: superpowers-review.py posttool|stop|run|status [args]")
    cmd = sys.argv[1]
    if cmd == "posttool":
        hook_posttool()
    elif cmd == "stop":
        hook_stop()
    elif cmd == "run":
        cmd_run(sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])
    elif cmd == "status":
        cmd_status(sys.argv[2] if len(sys.argv) > 2 else os.getcwd())
    else:
        sys.exit("unknown subcommand: " + cmd)


if __name__ == "__main__":
    main()
