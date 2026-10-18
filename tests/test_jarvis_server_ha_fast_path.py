"""Home Assistant fast path: simple home requests are answered by HA's own intent matcher."""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from jarvis_server import ha_fast_path


class _FakeHA:
    """Minimal /api/conversation/process that records requests and replies as told."""

    def __init__(self, response_type="query_answer", speech="23.6 degrees", code=None, success=(), states=None):
        fake = self
        self.requests = []
        self.states = states or {}  # entity_id -> last_updated (ISO)

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append({"path": self.path, "auth": self.headers.get("Authorization"), **body})
                data = {"code": code} if code else {"success": list(success)}
                payload = json.dumps({"response": {
                    "response_type": response_type, "data": data,
                    "speech": {"plain": {"speech": speech}}}}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                entity = self.path.rsplit("/", 1)[-1]
                payload = json.dumps({"entity_id": entity, "last_updated": fake.states.get(entity, "2000-01-01T00:00:00+00:00")}).encode()
                self.send_response(200)
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


def _now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def test_performed_action_returns_has_reply_once_the_device_changed(ha):
    ha(response_type="action_done", speech="Turned off the lights",
       success=[{"type": "entity", "id": "light.kitchen", "name": "Kitchen"}],
       states={"light.kitchen": _now()})
    assert ha_fast_path.answer("turn off the kitchen lights") == "Turned off the lights"


def test_action_that_changed_nothing_goes_to_the_llm(ha, monkeypatch):
    """HA "succeeds" on a thermostat that is off; the device never updates."""
    monkeypatch.setattr(ha_fast_path, "CHECK_SEC", 0.3)
    monkeypatch.setattr(ha_fast_path, "CHECK_SEC_BY_DOMAIN", {})
    ha(response_type="action_done", speech="Temperature set to 23",
       success=[{"type": "entity", "id": "climate.family_room", "name": "Family Room"}])
    assert ha_fast_path.answer("set the thermostat to 23") is None


def test_action_without_entity_targets_goes_to_the_llm(ha):
    ha(response_type="action_done", speech="Done", success=[{"type": "area", "id": "kitchen", "name": "Kitchen"}])
    assert ha_fast_path.answer("turn off the kitchen") is None


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


@pytest.mark.parametrize("text", [
    "Jarvis, is anyone in the nursery right now and are its lights on?",
    "turn off the kitchen lights then lock the front door",
    "what is the temperature, also is the garage open",
])
def test_multi_part_requests_go_to_the_llm(ha, text):
    """HA answers one part of a compound request; the LLM handles all of it."""
    fake = ha(response_type="query_answer", speech="No")
    assert ha_fast_path.answer(text) is None
    assert fake.requests == []
