"""Home Assistant actions are checked against live state before Jarvis reports them."""

import json
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

import pytest

from jarvis_server import action_check


@dataclass
class _Result:
    success: bool
    reply_text: Optional[str]
    error_message: Optional[str] = None


def _action_result(*entity_ids):
    """HA's intent response for an action, as its MCP server returns it."""
    return _Result(True, json.dumps({
        "speech": {}, "response_type": "action_done",
        "data": {"targets": [], "failed": [],
                 "success": [{"name": e, "type": "entity", "id": e} for e in entity_ids]}}))


def _fake_tools(action_result, live_context="Live Context: lights on"):
    calls = []

    def run(db, cfg, tool_name, tool_args, *rest, **kw):
        calls.append((tool_name, dict(tool_args or {})))
        if len(calls) > 1:  # the follow-up state read
            return _Result(True, live_context)
        return action_result

    return run, calls


class _FakeHA:
    """/api/states: entity states, each last updated `ago` seconds before the request."""

    def __init__(self, states):
        fake = self
        self.states = states  # entity_id -> (state, attributes, ago_seconds)

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                now = datetime.now(timezone.utc)
                if self.path == "/api/states":
                    body = [fake._entity(e, now) for e in fake.states]
                else:
                    body = fake._entity(self.path.rsplit("/", 1)[-1], now)
                data = json.dumps(body).encode()
                self.send_response(200)
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def _entity(self, entity_id, now):
        state, attrs, ago = self.states.get(entity_id, ("unknown", {}, 3600))
        return {"entity_id": entity_id, "state": state, "attributes": attrs,
                "last_updated": (now - timedelta(seconds=ago)).isoformat()}


@pytest.fixture
def ha(monkeypatch):
    fakes = []
    monkeypatch.setattr(action_check, "POLL_SEC", 0.05)
    monkeypatch.setattr(action_check, "CONFIRM_SEC", 0.3)
    monkeypatch.setattr(action_check, "_climate_cache", (0.0, set()))

    def make(states):
        fake = _FakeHA(states)
        fakes.append(fake)
        monkeypatch.setenv("JARVIS_HA_URL", fake.url)
        monkeypatch.setenv("JARVIS_HA_TOKEN", "secret")
        return fake

    yield make
    for fake in fakes:
        fake.server.shutdown()


def test_updated_entity_is_reported_as_confirmed(ha):
    ha({"light.kitchen": ("off", {"friendly_name": "Kitchen"}, 0)})
    run, calls = _fake_tools(_action_result("light.kitchen"))

    result = action_check.wrap(run)(None, None, "home_assistant__intent__HassTurnOff", {"name": "Kitchen"}, "", "", "")

    assert "VERIFICATION: CONFIRMED" in result.reply_text and "light.kitchen: off" in result.reply_text
    assert len(calls) == 1  # no GetLiveContext needed


def test_entity_that_never_updates_is_reported_unconfirmed(ha):
    ha({"light.kitchen": ("on", {}, 3600)})
    run, _ = _fake_tools(_action_result("light.kitchen"))

    result = action_check.wrap(run)(None, None, "home_assistant__intent__HassTurnOff", {"name": "Kitchen"}, "", "", "")

    assert "VERIFICATION: NOT CONFIRMED" in result.reply_text


def test_thermostat_that_is_off_is_reported_at_once(ha):
    ha({"climate.family_room": ("off", {"friendly_name": "Family Room"}, 3600)})
    run, _ = _fake_tools(_action_result("climate.family_room"))

    result = action_check.wrap(run)(None, None, "home_assistant__climate__HassClimateSetTemperature", {"temperature": 23}, "", "", "")

    assert "THERMOSTAT IS OFF" in result.reply_text


def test_running_thermostat_is_pending_without_waiting(ha, monkeypatch):
    monkeypatch.setattr(action_check, "CONFIRM_SEC", 30)  # the test would hang if it waited
    ha({"climate.family_room": ("heat", {"friendly_name": "Family Room", "temperature": 22}, 3600)})
    run, _ = _fake_tools(_action_result("climate.family_room"))

    result = action_check.wrap(run)(None, None, "home_assistant__climate__HassClimateSetTemperature", {"temperature": 23}, "", "", "")

    assert "PENDING" in result.reply_text


@pytest.mark.parametrize("args", [
    {"name": "Family Room", "domain": ["climate"]},
    {"name": "family room"},          # the thermostat's own name
    {"domain": ["climate"]},
])
def test_thermostat_cannot_be_turned_on_or_off(ha, args):
    ha({"climate.family_room": ("off", {"friendly_name": "Family Room"}, 3600)})
    run, calls = _fake_tools(_action_result("climate.family_room"))

    for tool in ("home_assistant__intent__HassTurnOn", "home_assistant__intent__HassTurnOff"):
        result = action_check.wrap(run)(None, None, tool, args, "", "", "")
        assert not result.success and "thermostat" in result.error_message
    assert calls == []  # never sent to Home Assistant


def test_other_devices_can_still_be_turned_on(ha):
    ha({"climate.family_room": ("off", {"friendly_name": "Family Room"}, 3600),
        "light.kitchen": ("on", {"friendly_name": "Kitchen"}, 0)})
    run, calls = _fake_tools(_action_result("light.kitchen"))

    result = action_check.wrap(run)(None, None, "home_assistant__intent__HassTurnOn", {"name": "Kitchen"}, "", "", "")

    assert result.success and calls[0][0] == "home_assistant__intent__HassTurnOn"


def test_without_entity_ids_it_falls_back_to_live_context(monkeypatch):
    monkeypatch.setattr(action_check, "SETTLE_SEC", 0)
    monkeypatch.delenv("JARVIS_HA_URL", raising=False)
    run, calls = _fake_tools(_Result(True, "done"))

    result = action_check.wrap(run)(None, None, "home_assistant__light__HassLightSet",
                                    {"area": "Kitchen", "brightness": 50}, "", "", "")

    assert "Live Context: lights on" in result.reply_text
    assert calls[1] == ("home_assistant__homeassistant__GetLiveContext", {"area": "Kitchen", "domain": ["light"]})


def test_reads_failures_and_other_tools_are_untouched(monkeypatch):
    monkeypatch.setattr(action_check, "SETTLE_SEC", 0)
    monkeypatch.delenv("JARVIS_HA_URL", raising=False)
    for name, result in [
        ("home_assistant__homeassistant__GetLiveContext", _Result(True, "state")),
        ("home_assistant__todo__get_items", _Result(True, "items")),
        ("home_assistant__intent__HassTurnOn", _Result(False, None, "no device")),
        ("getWeather", _Result(True, "sunny")),
    ]:
        run, calls = _fake_tools(result)
        assert action_check.wrap(run)(None, None, name, {}, "", "", "") is result
        assert len(calls) == 1, name


def test_keyword_calls_as_the_reply_engine_makes_them(ha):
    ha({"light.kitchen": ("on", {}, 0)})
    run, _ = _fake_tools(_action_result("light.kitchen"))
    result = action_check.wrap(run)(
        db=None, cfg=None, tool_name="home_assistant__light__HassLightSet",
        tool_args={"name": "Kitchen", "brightness": 50}, system_prompt="", original_prompt="", redacted_text="")
    assert "VERIFICATION: CONFIRMED" in result.reply_text
