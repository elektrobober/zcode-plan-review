"""Drive two Write calls through a fresh headless ZCode CLI session."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
DOC = Path("docs/superpowers/plans/live.md")
VERSIONS = ["Plan version one\n", "Plan version two\n"]


def wait_for(predicate, timeout=25):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("Timed out waiting for the hook or reviewer")
        time.sleep(0.1)


def model_endpoint(root):
    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            step = sum(msg["role"] == "tool" for msg in body["messages"])
            delta = {"role": "assistant", "content": "Test complete."}
            finish = "stop"
            if step < 2:
                if step:
                    wait_for(lambda: (root / "v1-started").exists())
                args = {"file_path": str(root / DOC), "content": VERSIONS[step]}
                delta = {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": f"write_{step}",
                            "type": "function",
                            "function": {"name": "Write", "arguments": json.dumps(args)},
                        }
                    ],
                }
                finish = "tool_calls"
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for part, reason in [(delta, None), ({}, finish)]:
                event = {
                    "id": "live-test",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "gpt-4o",
                    "choices": [{"index": 0, "delta": part, "finish_reason": reason}],
                }
                self.wfile.write(("data: " + json.dumps(event) + "\n\n").encode())
            self.wfile.write(b"data: [DONE]\n\n")

    return Endpoint


def write_provider(path, port):
    api = {"type": "openai-chat-completions", "baseUrl": f"http://127.0.0.1:{port}/v1"}
    rule = {
        "providerId": "fixture",
        "providerName": "Test fixture",
        "enabled": True,
        "config": {
            "group": "standard-personal",
            "api": api,
            "access": {"type": "api-key", "apiKey": "local-test"},
            "personalModelIds": ["gpt-4o"],
            "modelOrder": ["gpt-4o"],
        },
    }
    config = {
        "providerOrder": ["fixture"],
        "providerConfigRules": {"providerRules": [rule]},
        "modelConfigRules": {"providerModelRules": [], "manualProviderModelRules": []},
        "defaultModelSelection": {"providerId": "fixture", "modelId": "gpt-4o"},
    }
    path.write_text(json.dumps({"schemaVersion": 1, "config": config}))


def exercise(tmp, cli, builtin):
    root = tmp / "repo"
    (root / DOC).parent.mkdir(parents=True)
    profile = tmp / "profile"
    profile.mkdir()
    binaries = tmp / "bin"
    binaries.mkdir()
    fake = binaries / "codex"
    shutil.copyfile(REPO / "tests/fake_codex.py", fake)
    fake.chmod(0o700)
    # ZCode 0.17.1 resolves its global config via os.homedir(). Only the child
    # processes get this temporary user profile; no global override is installed.
    env = dict(
        os.environ,
        HOME=str(profile),
        PATH=str(binaries) + os.pathsep + os.environ["PATH"],
        SPR_FIXTURE_ROOT=str(root),
        ZCODE_STORAGE_DIR=str(profile / ".zcode"),
        ZCODE_DATA_BASE_DIR=str(profile),
        ZCODE_BUILTIN_PROVIDER_CONFIG_FILE=str(builtin),
        ZCODE_PERSONAL_PROVIDER_CONFIG_FILE=str(tmp / "provider.json"),
    )
    subprocess.run([sys.executable, str(REPO / "install"), "zcode"], env=env, check=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), model_endpoint(root))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state = profile / ".zcode/hooks/zcode-plan-review/.spr-state.json"
    doc = root / DOC

    def entry():
        return json.loads(state.read_text()).get(str(doc), {})

    write_provider(tmp / "provider.json", server.server_port)
    try:
        with subprocess.Popen(
            [
                *cli,
                "--cwd",
                str(root),
                "--mode",
                "edit",
                "-p",
                "Write the two versions of the test plan.",
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        ) as proc:
            try:
                wait_for(lambda: entry().get("status") == "done" or proc.poll() not in (None, 0))
                if proc.poll() not in (None, 0):
                    raise RuntimeError(proc.stdout.read()[:1500])
                current = entry()
                digest = hashlib.sha256(VERSIONS[1].encode()).hexdigest()
                assert current["hash"] == digest == hashlib.sha256(doc.read_bytes()).hexdigest()
                assert (root / "v1-started").exists()
                report = Path(current["out"])
                assert report.read_text().splitlines() == [f"sha256={digest}", "VERDICT: APPROVE"]
                (root / "release-first").touch()
                wait_for(lambda: len(list(report.parent.glob("*.out"))) == 2)
                # Wait for the older worker to exit after writing its report.
                # The worker holds the state lock during its final ownership check.
                pid = int((root / "first-worker").read_text())

                def exited():
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        return True
                    return False

                wait_for(exited)
                (first,) = [f for f in report.parent.glob("*.out") if f != report]
                first_digest = hashlib.sha256(VERSIONS[0].encode()).hexdigest()
                assert first.read_text().splitlines()[0] == f"sha256={first_digest}"
                assert entry() == current and current["verdict"] == "APPROVE"
                output, _ = proc.communicate(timeout=20)
                if proc.returncode:
                    raise RuntimeError(output[:1500])
                print(f"PASS: two ZCode writes, two reviews; final SHA-256 {digest}; APPROVE")
            finally:
                (root / "release-first").touch()
                if proc.poll() is None:
                    proc.kill()
    finally:
        server.shutdown()
        server.server_close()
        subprocess.run([sys.executable, str(REPO / "uninstall"), "zcode"], env=env, check=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("adapter", choices=["zcode"])
    opts = Path("/Applications/ZCode.app/Contents/Resources")
    parser.add_argument("--cli", type=Path, default=opts / "glm/zcode.cjs")
    parser.add_argument("--builtin", type=Path, default=opts / "config/provider/zcode-builtin.json")
    args = parser.parse_args()
    if not args.cli.is_file() or not args.builtin.is_file():
        parser.error(
            "Provide paths to the ZCode CLI and bundled provider JSON via --cli and --builtin"
        )
    if not __debug__:
        parser.error("Run with Python assertions enabled")
    cli = [str(args.cli.resolve())]
    if args.cli.suffix == ".cjs":
        cli.insert(0, "node")
    with tempfile.TemporaryDirectory(prefix="spr-live-") as tmp:
        exercise(Path(tmp).resolve(), cli, args.builtin.resolve())
