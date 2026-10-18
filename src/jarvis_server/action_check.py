"""Check Home Assistant actions against live state before Jarvis reports them.

HA reports success when it accepted a command, not when the device changed:
setting a temperature on a thermostat that is off "succeeds" and does nothing.
After a successful HA action tool, this waits for the device to settle, reads
its live state through HA's own GetLiveContext tool, and appends it to the
tool result with instructions, so the model compares the request with reality
before replying, and says so or tries another tool when they differ.
"""

from __future__ import annotations

import time
from dataclasses import replace

from jarvis.debug import debug_log

# ponytail: server name from this deployment's config.json; derive it from the
# server that offers GetLiveContext if another HA server name is ever used.
HA_PREFIX = "home_assistant__"
LIVE_CONTEXT = HA_PREFIX + "homeassistant__GetLiveContext"
SETTLE_SEC = 2.0
# Cloud thermostats (Google Nest via HA's nest integration) confirm a new target
# in ~6 s and a mode change in 30-40 s, so they get longer before the read.
SETTLE_SEC_BY_DOMAIN = {"climate": 8.0}
_READ_ONLY = {"homeassistant__GetLiveContext", "llm__GetDateTime", "todo__get_items", "media_player__search_media"}

_CHECK_NOTE = (
    "\n\nVERIFICATION: live state about {settle:g} s after the action:\n{state}\n"
    "Compare this with what the user asked for. If it is reflected, confirm it. If it is not, "
    "do not say it worked. Cloud-connected devices such as thermostats can take up to a minute "
    "to report a change: if the device is in the right mode and simply has not updated yet, say "
    "the command was sent but is not confirmed yet. If it plainly cannot have worked (for example "
    "a target set while the device is off), say what actually happened, and use another available "
    "tool if one can achieve the request."
)


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
        result = run_tool(db, cfg, tool_name, tool_args, *args, **kwargs)
        tool = (tool_name or "")[len(HA_PREFIX):] if (tool_name or "").startswith(HA_PREFIX) else None
        if tool is None or tool in _READ_ONLY or not getattr(result, "success", False):
            return result
        filters = _filters(tool, tool_args or {})
        settle = max([SETTLE_SEC] + [SETTLE_SEC_BY_DOMAIN.get(d, 0.0) for d in filters.get("domain", [])])
        try:
            time.sleep(settle)
            state = run_tool(db, cfg, LIVE_CONTEXT, filters, *args, **kwargs)
        except Exception as exc:
            debug_log(f"action check unavailable: {exc}", "server")
            return result
        if not getattr(state, "success", False) or not state.reply_text:
            return result
        print(f"    🔎 Checked live state after {tool}", flush=True)
        note = _CHECK_NOTE.format(settle=settle, state=state.reply_text.strip())
        return replace(result, reply_text=(result.reply_text or "") + note)

    return checked
