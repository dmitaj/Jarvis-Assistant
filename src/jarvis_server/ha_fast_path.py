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
import urllib.request
from typing import Optional

from jarvis.debug import debug_log

TIMEOUT_SEC = 4.0
_ANSWERED = {"action_done", "query_answer"}
# "Jarvis, ..." — HA would read the name as part of the request.
# ponytail: literal "jarvis"; a misheard alias reaches HA unstripped, fails to
# match and falls back to the LLM. Use the configured wake words if that bites.
_LEADING_NAME = re.compile(r"^\W*jarvis\b[\s,.:;!?]*", re.IGNORECASE)


def answer(text: str) -> Optional[str]:
    """HA's spoken reply if it handled the request, else None (use the LLM)."""
    url = os.environ.get("JARVIS_HA_URL", "").rstrip("/")
    token = os.environ.get("JARVIS_HA_TOKEN", "")
    request_text = _LEADING_NAME.sub("", text or "").strip()
    if not url or not token or not request_text:
        return None
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
    speech = response.get("speech", {}).get("plain", {}).get("speech", "").strip()
    return speech or None
