"""Answer simple home requests with Home Assistant's own intent matcher.

HA's built-in conversation agent recognises phrases like "turn off the
kitchen lights" or "is the front door light on" in milliseconds, without an
LLM. The server asks it first; only what HA does not understand goes through
Jarvis's full reply engine. Enabled by ``JARVIS_HA_URL`` and ``JARVIS_HA_TOKEN``.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from datetime import datetime
from typing import Optional

from jarvis.debug import debug_log
from jarvis_server import action_check

TIMEOUT_SEC = 4.0
# An action counts only once a targeted entity actually updates.
CHECK_SEC = 3.0
# Thermostat requests go to the model path, where action_check refuses switching
# the thermostat on/off (HA's own matcher would act on it directly) and replies
# report the setpoint rather than HA's terse "Thermostat is heat".
# ponytail: English heating words plus the climate entities' names from HA.
_THERMOSTAT_WORDS = re.compile(r"\b(thermostat|heat|heating|heater|furnace|cool|cooling|a/?c|air conditioning|hvac|temperature)\b", re.IGNORECASE)
_ANSWERED = {"action_done", "query_answer"}
# "Jarvis, ..." — HA would read the name as part of the request.
# ponytail: literal "jarvis"; a misheard alias reaches HA unstripped, fails to
# match and falls back to the LLM. Use the configured wake words if that bites.
_LEADING_NAME = re.compile(r"^\W*jarvis\b[\s,.:;!?]*", re.IGNORECASE)
# HA answers only one part of a compound request ("is anyone in the nursery and
# are its lights on?" -> "No"), so those go to the LLM, which handles every part.
# ponytail: English joiners only; other languages fall through to HA as before.
_MULTI_PART = re.compile(r"\b(and|then|also)\b", re.IGNORECASE)


def answer(text: str) -> Optional[str]:
    """HA's spoken reply if it handled the request, else None (use the LLM)."""
    url = os.environ.get("JARVIS_HA_URL", "").rstrip("/")
    token = os.environ.get("JARVIS_HA_TOKEN", "")
    request_text = _LEADING_NAME.sub("", text or "").strip()
    if not url or not token or not request_text or _MULTI_PART.search(request_text):
        return None
    if _THERMOSTAT_WORDS.search(request_text) or any(
            name in request_text.casefold() for name in action_check.climate_names()):
        debug_log("HA fast path: thermostat request, using the model path", "server")
        return None
    started = time.time()
    req = urllib.request.Request(
        f"{url}/api/conversation/process",
        data=json.dumps({"text": request_text, "language": "en",
                         "agent_id": "conversation.home_assistant"}).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
            response = json.load(resp)["response"]
    except Exception as exc:
        debug_log(f"HA fast path unavailable: {exc}", "server")
        return None
    if response.get("response_type") not in _ANSWERED:
        debug_log(f"HA fast path: no match ({response.get('data', {}).get('code')})", "server")
        return None
    if response.get("response_type") == "action_done" and not _changed(url, token, response, started):
        debug_log("HA fast path: action reported done but no target updated; using the LLM", "server")
        print("  🏠 Home Assistant reported success but nothing changed; asking the model", flush=True)
        return None
    speech = response.get("speech", {}).get("plain", {}).get("speech", "").strip()
    return speech or None


def _changed(url: str, token: str, response: dict, started: float) -> bool:
    """True once any entity HA says it acted on reports an update since ``started``."""
    ids = [t["id"] for t in response.get("data", {}).get("success", []) if t.get("type") == "entity"]
    deadline = time.time() + CHECK_SEC
    while ids:
        for entity_id in ids:
            req = urllib.request.Request(f"{url}/api/states/{entity_id}",
                                         headers={"Authorization": f"Bearer {token}"})
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                    updated = datetime.fromisoformat(json.load(resp)["last_updated"]).timestamp()
            except Exception as exc:
                debug_log(f"HA fast path: state check failed for {entity_id}: {exc}", "server")
                return False
            if updated >= started - 2.0:  # allow for clock skew between hosts
                return True
        if time.time() >= deadline:
            return False
        time.sleep(0.5)
    return False
