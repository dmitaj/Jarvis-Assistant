"""Home Assistant actions are checked against live state before Jarvis reports them."""

from dataclasses import dataclass
from typing import Optional

from jarvis_server import action_check


@dataclass
class _Result:
    success: bool
    reply_text: Optional[str]
    error_message: Optional[str] = None


def _fake_tools(action_result, live_context="climate.family_room: off, 20.8 °C"):
    calls = []

    def run(db, cfg, tool_name, tool_args, *rest, **kw):
        calls.append((tool_name, dict(tool_args or {})))
        if len(calls) > 1:  # the follow-up state read
            return _Result(True, live_context)
        return action_result

    return run, calls


def test_successful_action_carries_live_state_and_check_instructions(monkeypatch):
    monkeypatch.setattr(action_check, "SETTLE_SEC", 0)
    monkeypatch.setattr(action_check, "SETTLE_SEC_BY_DOMAIN", {})
    run, calls = _fake_tools(_Result(True, '{"success": true}'))
    checked = action_check.wrap(run)

    result = checked(None, None, "home_assistant__climate__HassClimateSetTemperature", {"temperature": 23}, "", "", "")

    assert result.success
    assert "climate.family_room: off" in result.reply_text
    assert "do not say it worked" in result.reply_text  # the model is told to compare, not assume
    assert "not confirmed yet" in result.reply_text     # slow cloud devices are pending, not failed
    assert calls[1] == ("home_assistant__homeassistant__GetLiveContext", {"domain": ["climate"]})


def test_live_state_is_narrowed_to_the_actions_domain_and_area(monkeypatch):
    monkeypatch.setattr(action_check, "SETTLE_SEC", 0)
    monkeypatch.setattr(action_check, "SETTLE_SEC_BY_DOMAIN", {})
    run, calls = _fake_tools(_Result(True, "ok"))
    action_check.wrap(run)(None, None, "home_assistant__intent__HassTurnOn",
                           {"area": "Kitchen", "domain": ["light"]}, "", "", "")
    assert calls[1][1] == {"area": "Kitchen", "domain": ["light"]}


def test_reads_failures_and_other_tools_are_untouched(monkeypatch):
    monkeypatch.setattr(action_check, "SETTLE_SEC", 0)
    monkeypatch.setattr(action_check, "SETTLE_SEC_BY_DOMAIN", {})
    for name, result in [
        ("home_assistant__homeassistant__GetLiveContext", _Result(True, "state")),
        ("home_assistant__todo__get_items", _Result(True, "items")),
        ("home_assistant__intent__HassTurnOn", _Result(False, None, "no device")),
        ("getWeather", _Result(True, "sunny")),
    ]:
        run, calls = _fake_tools(result)
        assert action_check.wrap(run)(None, None, name, {}, "", "", "") is result
        assert len(calls) == 1, name


def test_unavailable_check_leaves_the_action_result(monkeypatch):
    monkeypatch.setattr(action_check, "SETTLE_SEC", 0)
    monkeypatch.setattr(action_check, "SETTLE_SEC_BY_DOMAIN", {})
    action = _Result(True, "done")

    def run(db, cfg, tool_name, tool_args, *rest, **kw):
        if tool_name.endswith("GetLiveContext"):
            raise RuntimeError("HA down")
        return action

    assert action_check.wrap(run)(None, None, "home_assistant__intent__HassTurnOff", {}, "", "", "") is action


def test_keyword_calls_as_the_reply_engine_makes_them(monkeypatch):
    monkeypatch.setattr(action_check, "SETTLE_SEC", 0)
    monkeypatch.setattr(action_check, "SETTLE_SEC_BY_DOMAIN", {})
    run, calls = _fake_tools(_Result(True, "ok"))
    result = action_check.wrap(run)(
        db=None, cfg=None, tool_name="home_assistant__light__HassLightSet",
        tool_args={"name": "Kitchen", "brightness": 50}, system_prompt="", original_prompt="", redacted_text="")
    assert "VERIFICATION" in result.reply_text
    assert calls[1] == ("home_assistant__homeassistant__GetLiveContext", {"domain": ["light"]})


def test_thermostats_get_longer_to_settle(monkeypatch):
    slept = []
    monkeypatch.setattr(action_check.time, "sleep", slept.append)
    run, _ = _fake_tools(_Result(True, "ok"))
    action_check.wrap(run)(None, None, "home_assistant__climate__HassClimateSetTemperature", {"temperature": 23}, "", "", "")
    action_check.wrap(run)(None, None, "home_assistant__light__HassLightSet", {"brightness": 50}, "", "", "")
    assert slept == [action_check.SETTLE_SEC_BY_DOMAIN["climate"], action_check.SETTLE_SEC]
