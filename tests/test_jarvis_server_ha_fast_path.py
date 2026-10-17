"""Home Assistant fast path: simple home requests are answered by HA's own intent matcher."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jarvis_server import ha_fast_path


class _FakeHA:
    """Minimal /api/conversation/process that records requests and replies as told."""

    def __init__(self, response_type="query_answer", speech="23.6 degrees", code=None):
        fake = self
        self.requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), **body})
                data = {"code": code} if code else {}
                payload = json.dumps({"response": {
                    "response_type": response_type, "data": data,
                    "speech": {"plain": {"speech": speech}}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def ha(monkeypatch):
    fakes = []

    def make(**kwargs):
        fake = _FakeHA(**kwargs)
        fakes.append(fake)
        monkeypatch.setenv("JARVIS_HA_URL", fake.url)
        monkeypatch.setenv("JARVIS_HA_TOKEN", "secret")
        return fake

    yield make
    for fake in fakes:
        fake.close()


def test_answered_question_returns_has_reply_without_the_wake_word(ha):
    fake = ha(response_type="query_answer", speech="23.6 degrees")

    assert ha_fast_path.answer("Jarvis, what is the temperature in the family room?") == "23.6 degrees"
    assert fake.requests[0]["text"] == "what is the temperature in the family room?"
    assert fake.requests[0]["auth"] == "Bearer secret"
    assert fake.requests[0]["path"] == "/api/conversation/process"


def test_performed_action_returns_has_reply(ha):
    ha(response_type="action_done", speech="Turned off the lights")
    assert ha_fast_path.answer("turn off the kitchen lights") == "Turned off the lights"


def test_unmatched_request_falls_back_to_the_llm(ha):
    ha(response_type="error", code="no_valid_targets", speech="Sorry, I am not aware of any device called a joke")
    assert ha_fast_path.answer("Jarvis, tell me a joke") is None


def test_unreachable_home_assistant_falls_back(monkeypatch):
    monkeypatch.setenv("JARVIS_HA_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("JARVIS_HA_TOKEN", "secret")
    assert ha_fast_path.answer("turn off the kitchen lights") is None


def test_disabled_without_configuration(monkeypatch):
    monkeypatch.delenv("JARVIS_HA_URL", raising=False)
    monkeypatch.delenv("JARVIS_HA_TOKEN", raising=False)
    assert ha_fast_path.answer("turn off the kitchen lights") is None


def test_bare_wake_word_is_not_sent(ha):
    fake = ha()
    assert ha_fast_path.answer("Jarvis") is None
    assert fake.requests == []
