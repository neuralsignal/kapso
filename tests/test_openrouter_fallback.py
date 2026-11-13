"""OpenRouter as Codex's fallback (fork patch, SANOPTIS.md): only a spent
subscription or a rate limit triggers it, every request it sends carries
`provider: {zdr: true, allow_fallbacks: false}`, and it is off unless the
config turns it on."""

import http.client
import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from kapso.core.config import load_config
from kapso.core.openrouter_fallback import fallback_settings, is_quota_error, relay_url, zdr_body
from kapso.execution.coding_agents.adapters.codex_agent import CodexCodingAgent
from kapso.execution.coding_agents.base import CodingAgentConfig
from kapso.learning.update_frame import UpdateFrame

USAGE_LIMIT = "You’ve hit your usage limit. Visit https://chatgpt.com/settings/usage to purchase more credits"


def settings(base_url="https://openrouter.ai/api/v1", **changes):
    return {"enabled": True, "base_url": base_url, "api_key_env": "OR_TEST_KEY",
            "model": "openai/gpt-5.6-sol", "timeout_seconds": 30, **changes}


@pytest.mark.parametrize("message", [
    USAGE_LIMIT,
    "Quota exceeded. Check your plan and billing details.",
    "rate limit exceeded: try again in 20s",
    "exceeded retry limit, last status: 429 Too Many Requests",
    "Your workspace is out of credits. Add credits to continue.",
])
def test_codex_quota_and_rate_limit_messages_trigger_the_fallback(message):
    assert is_quota_error(f"Codex CLI exited with code 1: {message}")


@pytest.mark.parametrize("message", [
    "exceeded retry limit, last status: 500 Internal Server Error",
    "Codex CLI killed by its deadline after 900s",
    "Selected model is at capacity. Please try a different model.",
    "",
])
def test_other_failures_do_not(message):
    assert not is_quota_error(message)


def test_the_body_is_forced_to_zdr_endpoints_without_provider_fallback():
    body = json.loads(zdr_body(json.dumps({
        "model": "openai/gpt-5.6-sol", "input": "x", "provider": {"allow_fallbacks": True, "order": ["a"]},
    }).encode()))
    assert body["provider"] == {"zdr": True, "allow_fallbacks": False}
    assert body["input"] == "x"
    with pytest.raises(ValueError, match="JSON object"):
        zdr_body(b"[]")


def test_the_packaged_config_ships_it_off_and_a_bad_block_is_refused():
    assert fallback_settings(load_config()["hosts"]["codex"]["openrouter_fallback"]) == {}
    assert fallback_settings(None) == {}
    with pytest.raises(ValueError, match="exactly the keys"):
        fallback_settings({"enabled": True})
    with pytest.raises(ValueError, match="HTTPS"):
        fallback_settings(settings(base_url="http://openrouter.ai/api/v1"))


def test_the_relay_adds_the_routing_and_passes_the_answer_through():
    seen = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append((self.path, self.headers["Authorization"],
                         json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
            self.send_response(429)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b"data: one\n\ndata: two\n\n")

        def log_message(self, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    relay = relay_url(fallback_settings(settings(base_url=f"http://127.0.0.1:{upstream.server_port}/api/v1")))
    connection = http.client.HTTPConnection(relay.split("//")[1])
    connection.request("POST", "/responses", body=json.dumps({"model": "m", "input": "x"}),
                       headers={"Authorization": "Bearer k", "Content-Type": "application/json"})
    response = connection.getresponse()
    assert (response.status, response.getheader("Content-Type")) == (429, "text/event-stream")
    assert response.read() == b"data: one\n\ndata: two\n\n"
    [(path, auth, body)] = seen
    assert (path, auth) == ("/api/v1/responses", "Bearer k")
    assert body == {"model": "m", "input": "x", "provider": {"zdr": True, "allow_fallbacks": False}}
    upstream.shutdown()


# A stand-in `codex`: on the subscription (no model_provider override) it
# fails with the given message; on OpenRouter it answers. Every call's
# arguments are logged one line each.
FAKE_CODEX = """#!/bin/bash
echo "$*" >> "{log}"
out=""; prev=""
for arg in "$@"; do [ "$prev" = "--output-last-message" ] && out="$arg"; prev="$arg"; done
cat > /dev/null
if [[ "$*" == *model_provider=* ]]; then
  echo '{{"type":"thread.started","thread_id":"t-or"}}'
  echo "answer from openrouter" > "$out"; exit 0
fi
echo '{{"type":"thread.started","thread_id":"t-sub"}}'
echo '{{"type":"error","message":"{message}"}}'
exit 1
"""


def fake_codex(tmp_path, monkeypatch, message):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "codex-calls.log"
    script = bin_dir / "codex"
    script.write_text(FAKE_CODEX.format(log=log, message=message))
    script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    return log


def codex_agent(tmp_path, block):
    agent = CodexCodingAgent(CodingAgentConfig(
        agent_type="codex", model="gpt-5.6-sol", debug_model="gpt-5.6-sol",
        agent_specific={"web_search": False, "openrouter_fallback": block},
    ))
    agent.initialize(str(tmp_path))
    return agent


def test_a_spent_subscription_reruns_the_session_on_openrouter(tmp_path, monkeypatch):
    log = fake_codex(tmp_path, monkeypatch, USAGE_LIMIT)
    result = codex_agent(tmp_path, settings()).generate_code("task")
    assert result.success and result.output == "answer from openrouter"
    subscription, openrouter = log.read_text().splitlines()
    assert "-m gpt-5.6-sol" in subscription and "model_provider" not in subscription
    assert "-m openai/gpt-5.6-sol" in openrouter
    assert 'model_provider="kapso_openrouter_zdr"' in openrouter
    assert 'model_providers.kapso_openrouter_zdr.env_key="OR_TEST_KEY"' in openrouter
    assert "model_providers.kapso_openrouter_zdr.base_url=\"http://127.0.0.1:" in openrouter


def test_any_other_failure_stays_a_failure(tmp_path, monkeypatch):
    log = fake_codex(tmp_path, monkeypatch, "Selected model is at capacity.")
    assert not codex_agent(tmp_path, settings()).generate_code("task").success
    assert len(log.read_text().splitlines()) == 1


def test_a_disabled_fallback_leaves_the_quota_failure_as_it_is(tmp_path, monkeypatch):
    log = fake_codex(tmp_path, monkeypatch, USAGE_LIMIT)
    result = codex_agent(tmp_path, settings(enabled=False)).generate_code("task")
    assert not result.success and "usage limit" in result.error
    assert len(log.read_text().splitlines()) == 1


def test_the_update_crew_worker_falls_back_inside_its_launcher(tmp_path, monkeypatch):
    log = fake_codex(tmp_path, monkeypatch, USAGE_LIMIT)
    config = load_config()
    config["hosts"]["codex"]["openrouter_fallback"] = settings()
    config["learning"]["update_crew"]["run_root"] = str(tmp_path / "runs")
    frame = UpdateFrame(SimpleNamespace(), config, bank_home=tmp_path / "bank")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    workers = SimpleNamespace(
        guidance="", env={}, codex_exec_args=lambda: [],
        agent_specific={"stream_artifact_path": str(run_dir / "workers.jsonl")},
    )
    frame._stage_crew(run_dir, "lr_1", [], "v1", workers)
    (run_dir / "assignment.md").write_text("do it")
    done = subprocess.run(["./run-role.sh", "card-writer", "assignment.md", "out.md"],
                          cwd=run_dir, capture_output=True, text=True)
    assert done.returncode == 0 and done.stdout.strip() == "answer from openrouter"
    subscription, openrouter = log.read_text().splitlines()
    assert "model_provider" not in subscription and 'model_provider="kapso_openrouter_zdr"' in openrouter
    exits = [json.loads(line) for line in (run_dir / "workers.jsonl").read_text().splitlines()
             if line.startswith('{"type":"kapso.exit"')]
    assert [e["code"] for e in exits] == [1, 0]
