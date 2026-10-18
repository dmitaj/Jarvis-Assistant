"""Check Home Assistant actions against live state before Jarvis reports them.

HA reports success when it accepted a command, not when the device changed:
setting a temperature on a thermostat that is off "succeeds" and does nothing.
After a successful HA action tool this reads the state of the entities HA says
it acted on and appends it to the tool result with a verdict, so the model
reports what actually happened:

- CONFIRMED: an entity updated after the request (polled for up to 2 s).
- NOT CONFIRMED: nothing updated in that time.
- Thermostats: a cloud Nest takes seconds to a minute to confirm, so there is
  no wait. If it is off the change cannot apply (reported at once), otherwise
  it is PENDING.

Without entity IDs in the result (or HA's REST API) it falls back to HA's
GetLiveContext for the action's domain and area.

Policy: Jarvis must not switch the thermostat on or off (that changes between
heating and cooling modes); those calls are refused before reaching HA.
"""

from __future__ import annotations

import json
import os
import time
import urllib.request
from dataclasses import replace
from datetime import datetime
from typing import Optional

from jarvis.debug import debug_log
from jarvis.tools.types import ToolExecutionResult

# ponytail: server name from this deployment's config.json; derive it from the
# server that offers GetLiveContext if another HA server name is ever used.
HA_PREFIX = "home_assistant__"
LIVE_CONTEXT = HA_PREFIX + "homeassistant__GetLiveContext"
SETTLE_SEC = 2.0       # fallback wait before GetLiveContext
CONFIRM_SEC = 2.0      # how long to poll acted entities for an update
POLL_SEC = 0.25
_READ_ONLY = {"homeassistant__GetLiveContext", "llm__GetDateTime", "todo__get_items", "media_player__search_media"}
_ON_OFF = {"intent__HassTurnOn", "intent__HassTurnOff"}

_INSTRUCTIONS = (
    "Report what actually happened. Never say a change worked unless it is CONFIRMED. "
    "For PENDING, say it was sent and the device has not confirmed yet. "
    "For NOT CONFIRMED, do not say it worked: say the device did not report the change."
)


def _ha_get(path: str):
    url = os.environ.get("JARVIS_HA_URL", "").rstrip("/")
    token = os.environ.get("JARVIS_HA_TOKEN", "")
    if not url or not token:
        return None
    req = urllib.request.Request(url + path, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req, timeout=4) as resp:
        return json.load(resp)


_climate_cache: tuple[float, set[str]] = (0.0, set())
CLIMATE_CACHE_SEC = 300.0


def climate_names() -> set[str]:
    """Casefolded IDs and names of HA's climate entities (cached for 5 minutes)."""
    global _climate_cache
    fetched, names = _climate_cache
    if names and time.time() - fetched < CLIMATE_CACHE_SEC:
        return names
    try:
        states = _ha_get("/api/states") or []
    except Exception as exc:
        debug_log(f"action check: cannot list thermostats: {exc}", "server")
        return names
    names = set()
    for e in states if isinstance(states, list) else []:
        if isinstance(e, dict) and e.get("entity_id", "").startswith("climate."):
            names.add(e["entity_id"].casefold())
            names.add(str(e.get("attributes", {}).get("friendly_name", "")).casefold())
    names -= {""}
    _climate_cache = (time.time(), names)
    return names


def _refusal(tool: str, args: dict) -> Optional[ToolExecutionResult]:
    """Refuse switching a thermostat on/off (mode changes stay with people)."""
    if tool not in _ON_OFF:
        return None
    if "climate" in (args.get("domain") or []) or str(args.get("name", "")).casefold() in climate_names():
        msg = ("Not allowed: Jarvis may change the thermostat's target temperature but not turn the "
               "thermostat on or off or change between heating and cooling. Tell the user to do that themselves.")
        print("    🚫 Refused turning the thermostat on/off", flush=True)
        return ToolExecutionResult(success=False, reply_text=None, error_message=msg)
    return None


def _acted_entities(result) -> list[str]:
    try:
        text = result.reply_text or ""
        data = json.loads(text[text.index("{"):]).get("data", {})
        return [t["id"] for t in data.get("success", []) if t.get("type") == "entity" and t.get("id")]
    except (ValueError, AttributeError, KeyError, TypeError):
        return []


def _brief(entity: dict) -> str:
    a = entity.get("attributes", {})
    keep = {k: a[k] for k in ("friendly_name", "brightness", "temperature", "current_temperature",
                              "hvac_action", "volume_level", "current_position") if k in a}
    return f"{entity['entity_id']}: {entity.get('state')} {json.dumps(keep)}"


def _verdict(ids: list[str], started: float) -> Optional[str]:
    """Read the acted entities; None when HA's REST API is unavailable."""
    try:
        climate = [i for i in ids if i.startswith("climate.")]
        if climate:
            states = [_ha_get(f"/api/states/{i}") for i in climate]
            if any(s is None for s in states):
                return None
            off = [s for s in states if s.get("state") == "off"]
            lines = "\n".join(_brief(s) for s in states)
            if off:
                return (f"THERMOSTAT IS OFF: a new target temperature has no effect while it is off. "
                        f"Tell the user it is off; you cannot turn it on.\n{lines}")
            return f"PENDING: the thermostat accepted the change and confirms slowly (up to a minute).\n{lines}"
        deadline = time.time() + CONFIRM_SEC
        while True:
            states = [_ha_get(f"/api/states/{i}") for i in ids]
            if any(s is None for s in states):
                return None
            updated = [s for s in states
                       if datetime.fromisoformat(s["last_updated"]).timestamp() >= started - 2.0]
            lines = "\n".join(_brief(s) for s in states)
            if updated:
                return f"CONFIRMED: the device reported the change.\n{lines}"
            if time.time() >= deadline:
                return f"NOT CONFIRMED: no device reported a change within {CONFIRM_SEC:g} s.\n{lines}"
            time.sleep(POLL_SEC)
    except Exception as exc:
        debug_log(f"action check: state read failed: {exc}", "server")
        return None


def _filters(tool: str, args: dict) -> dict:
    """GetLiveContext filters for the devices an action touched."""
    domain = tool.split("__", 1)[0]
    filters = {}
    if args.get("area"):
        filters["area"] = args["area"]
    if args.get("domain"):
        filters["domain"] = list(args["domain"])
    elif domain not in ("intent", "assist_satellite"):
        filters["domain"] = [domain]
    return filters


def wrap(run_tool):
    """Wrap ``run_tool_with_retries`` so HA actions report verified state."""

    def checked(db, cfg, tool_name, tool_args, *args, **kwargs):
        name = tool_name or ""
        tool = name[len(HA_PREFIX):] if name.startswith(HA_PREFIX) else None
        if tool is not None:
            refusal = _refusal(tool, tool_args or {})
            if refusal:
                return refusal
        started = time.time()
        result = run_tool(db, cfg, tool_name, tool_args, *args, **kwargs)
        if tool is None or tool in _READ_ONLY or not getattr(result, "success", False):
            return result

        ids = _acted_entities(result)
        verdict = _verdict(ids, started) if ids else None
        if verdict is None:
            try:
                time.sleep(SETTLE_SEC)
                state = run_tool(db, cfg, LIVE_CONTEXT, _filters(tool, tool_args or {}), *args, **kwargs)
            except Exception as exc:
                debug_log(f"action check unavailable: {exc}", "server")
                return result
            if not getattr(state, "success", False) or not state.reply_text:
                return result
            verdict = (f"LIVE STATE about {SETTLE_SEC:g} s after the action (compare it with the request):\n"
                       f"{state.reply_text.strip()}")
        print(f"    🔎 Checked {tool}: {verdict.split(':', 1)[0]}", flush=True)
        return replace(result, reply_text=(result.reply_text or "") + f"\n\nVERIFICATION: {verdict}\n{_INSTRUCTIONS}")

    return checked
