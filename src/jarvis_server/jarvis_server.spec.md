# Jarvis Server Specification

Jarvis as a network service: the unchanged daemon (wake word, Whisper, intent
judge, reply engine, memory, MCP tools, Piper TTS) behind a web API, so a
browser — a wall panel — supplies the microphone and speakers. Run with
`python -m jarvis_server`; deployed as the Docker image in the repo root.

## Core principle: the core does not know

`jarvis` has no knowledge of the server, as it has none of `desktop_app`. The
server adapts at the edges, before the daemon loads:

| Seam | Stand-in | Effect |
|------|----------|--------|
| `sys.modules["sounddevice"]` | `browser_audio` | Input streams are fed by browser mic PCM; output streams (TTS, thinking tune) play in browsers |
| `sys.modules["desktop_app.face_widget"]` | `face_state` | idle/listening/thinking/speaking reach browsers instead of the desktop face |
| `jarvis.reply.engine.run_reply_engine` | wrapper | Every voice and text exchange publishes what was heard (redacted) and the reply |

`desktop_app` exports load lazily so `desktop_app.memory_viewer` imports
without PyQt.

## Home Assistant fast path

With `JARVIS_HA_URL` and `JARVIS_HA_TOKEN` set, the reply-engine wrapper first
sends the request (leading "Jarvis" removed) to HA's built-in conversation agent
(`/api/conversation/process`, `agent_id: conversation.home_assistant`). When HA
reports `action_done` or `query_answer`, its spoken reply is the answer: the
reply engine does not run, and both turns are added to dialogue memory (user
text redacted). Anything else — no intent match, HA unreachable, a 4 s
timeout — falls through to the reply engine unchanged. HA's replies are terse
and not in Jarvis's persona; that is the price of answering in ~0.1 s.

## Audio

- **In:** binary WebSocket frames of 16 kHz mono int16 LE PCM. One client owns
  the mic at a time; another takes over after the owner sends nothing for 1 s
  (closed or muted), or at once when it sends `{"type": "claim_mic"}` (the
  test page does on starting its mic). Interleaving two clients would double
  the apparent rate.
  Input streams accept only 16 kHz mono; anything else raises a PortAudio-shaped
  format error so the listener's format negotiation lands on 16 kHz mono.
- **Out:** each output block is broadcast as `[uint32 LE sample rate][int16 LE
  PCM]`, in chunks of at most 100 ms, paced in real time. Real-time pacing keeps
  `stream.active` true while audio plays, which TTS relies on for echo
  suppression and the hot window. Small chunks keep browsers from queuing audio
  far ahead, so a stopped stream goes quiet at once. `audio_end` follows each
  stream.

## WebSocket `/ws` events (server → browser, JSON text)

```json
{"type": "state", "state": "idle|listening|thinking|speaking|..."}
{"type": "query", "text": "<redacted>", "source": "voice|text"}
{"type": "reply", "text": "<reply or null>", "source": "voice|text"}
{"type": "audio_end"}
```

A new client receives the current state first. Each client has a bounded
outbox; a stalled client misses messages rather than growing memory or
blocking the audio pump.

## HTTP

| Route | Purpose |
|-------|---------|
| `GET /` | Test page: mic toggle, state, typed chat, speech playback |
| `GET /overlay.js` | Wall-panel overlay (`<jarvis-overlay>`), self-contained |
| `GET /memory` | The desktop memory viewer and its `/api/*` routes |
| `POST /api/chat` `{text}` | Text query; waits for `{reply}`. 409 when busy, 504 after 300 s |
| `POST /api/chat/cancel`, `/api/chat/new`, `GET /api/chat/history` | As the desktop chat window |
| `GET /api/state` | Current state |
| `GET /healthz` | Liveness, unauthenticated |

## Auth

With `JARVIS_TOKEN` set, every route but `/healthz` requires it as
`Authorization: Bearer`, `?token=` (which also sets an HttpOnly cookie for the
page's later requests), or that cookie. Without it the server warns at start.
Behind HA-Dash, nginx checks the viewer's Home Assistant session and adds the
token itself; browsers never hold it.

## Overlay

Hidden until Jarvis is listening, thinking or speaking; then a card shows the
state, what was heard and the reply, and hides 12 s (or 400 ms per word, if
longer) after Jarvis returns to idle. Tapping the card hides it. A small mic
button mutes sending (remembered per browser) and shows when the mic or the
connection is down. Paths resolve against the script's own URL. If the page
defines `window.jarvisAccessToken()`, its result goes in the `ha_dash_token`
cookie for the script's path before each connection.

## Deployment settings

`JARVIS_OLLAMA_NUM_CTX` and `JARVIS_OLLAMA_KEEP_ALIVE` (see `llm.spec.md`) pin
one context size and residency for every Ollama call, since a reload from
Unraid's array costs ~45 s. The image has no PortAudio, Qt, hotkeys or
Chatterbox; dictation must be disabled in config. `mcp-proxy` is bundled for
Home Assistant's MCP server.
