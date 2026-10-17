"""Jarvis as a network service: the daemon plus a web API for browser clients.

Browsers connect to ``/ws``: they stream microphone PCM in and receive
assistant state, what was heard, replies and speech audio. The same Flask app
serves text chat, the memory viewer (``/memory``) and a small test page (``/``).

Run: ``python -m jarvis_server`` (see ``jarvis_server.spec.md``).
"""

from __future__ import annotations

import hmac
import json
import os
import queue
import struct
import sys
import threading
from pathlib import Path

from jarvis_server import browser_audio, face_state, ha_fast_path

# Must happen before anything imports sounddevice or the desktop face widget.
sys.modules["sounddevice"] = browser_audio
import desktop_app  # noqa: E402  (lightweight: exports load lazily)
sys.modules["desktop_app.face_widget"] = face_state

from flask import Response, jsonify, make_response, request  # noqa: E402
from flask_sock import Sock  # noqa: E402

from desktop_app import memory_viewer  # noqa: E402
from jarvis import daemon  # noqa: E402
from jarvis.debug import debug_log  # noqa: E402
from jarvis.reply import engine as reply_engine  # noqa: E402
from jarvis.utils.redact import redact  # noqa: E402

TOKEN = os.environ.get("JARVIS_TOKEN", "")
PORT = int(os.environ.get("JARVIS_PORT", "8765"))
CHAT_TIMEOUT_SEC = 300
STATIC_DIR = Path(__file__).with_name("static")

app = memory_viewer.app
sock = Sock(app)


# --- Client hub -------------------------------------------------------------

class _Client:
    def __init__(self) -> None:
        # Bounded so a stalled browser cannot grow memory; it just misses audio.
        self.outbox: queue.Queue = queue.Queue(maxsize=512)

    def send(self, item) -> None:
        try:
            self.outbox.put_nowait(item)
        except queue.Full:
            pass


_clients: set[_Client] = set()
_clients_lock = threading.Lock()


def broadcast(item) -> None:
    with _clients_lock:
        clients = list(_clients)
    for client in clients:
        client.send(item)


def emit(event_type: str, **data) -> None:
    broadcast(json.dumps({"type": event_type, **data}))


def _on_audio_out(rate, pcm) -> None:
    if pcm is None:
        emit("audio_end")
    else:
        # Binary frame: uint32 LE sample rate, then int16 LE mono PCM.
        broadcast(struct.pack("<I", rate) + pcm)


browser_audio.audio_sink = _on_audio_out
face_state.on_change = lambda state: emit("state", state=state)


# Every voice and text exchange runs through run_reply_engine (both callers
# look it up on the module at call time), so wrapping it here publishes what
# was heard and the reply without touching the listener or the daemon.
_run_reply_engine = reply_engine.run_reply_engine


def _published_run_reply_engine(*args, **kwargs):
    text = kwargs["text"] if "text" in kwargs else (args[3] if len(args) > 3 else "")
    source = "text" if kwargs.get("quiet") else "voice"
    emit("query", text=redact(text or ""), source=source)
    reply = None
    try:
        # Simple home requests: HA's own intent matcher answers in milliseconds.
        reply = ha_fast_path.answer(text)
        if reply:
            print(f"  🏠 Home Assistant answered directly: {reply}", flush=True)
            memory = kwargs["dialogue_memory"] if "dialogue_memory" in kwargs else args[4]
            memory.add_message("user", redact(text))
            memory.add_message("assistant", reply)
            return reply
        reply = _run_reply_engine(*args, **kwargs)
        return reply
    finally:
        emit("reply", text=reply, source=source)


reply_engine.run_reply_engine = _published_run_reply_engine


# --- Auth -------------------------------------------------------------------

def _authorised() -> bool:
    if not TOKEN:
        return True
    auth = request.headers.get("Authorization", "")
    supplied = (
        auth[7:] if auth.startswith("Bearer ") else
        request.args.get("token") or request.cookies.get("jarvis_token") or ""
    )
    return hmac.compare_digest(supplied.encode(), TOKEN.encode())


@app.before_request
def _require_token():
    if request.path == "/healthz":
        return None
    if not _authorised():
        return jsonify({"error": "unauthorised"}), 401
    return None


@app.after_request
def _remember_token(response):
    # A page opened with ?token= keeps working (fetches, WebSocket) via cookie.
    if TOKEN and request.args.get("token"):
        response.set_cookie("jarvis_token", TOKEN, httponly=True, samesite="Strict",
                            secure=request.is_secure, max_age=60 * 60 * 24 * 365)
    return response


# --- Routes -----------------------------------------------------------------

@app.route("/healthz")
def healthz() -> Response:
    return jsonify({"ok": True})


# The memory viewer owns "/" in the desktop app; here it moves to /memory.
app.add_url_rule("/memory", "memory", app.view_functions["index"])
app.view_functions["index"] = lambda: make_response(
    (STATIC_DIR / "index.html").read_text(encoding="utf-8"))


@app.route("/overlay.js")
def overlay_js() -> Response:
    """The wall-panel overlay (see static/overlay.js)."""
    response = make_response((STATIC_DIR / "overlay.js").read_text(encoding="utf-8"))
    response.headers["Content-Type"] = "text/javascript; charset=utf-8"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/api/state")
def get_state() -> Response:
    return jsonify({"state": face_state.get_jarvis_state().state.value})


@app.route("/api/chat", methods=["POST"])
def chat() -> Response:
    """Ask Jarvis something by text and wait for the reply."""
    text = str((request.get_json(silent=True) or {}).get("text") or "").strip()
    if not text:
        return jsonify({"error": "text is required"}), 400
    done = threading.Event()
    result = {"reply": None, "busy": False}

    def on_complete(reply):
        result["reply"] = reply
        done.set()

    def on_busy():
        result["busy"] = True
        done.set()

    daemon.submit_text_query(text, on_complete=on_complete, on_busy=on_busy)
    if not done.wait(CHAT_TIMEOUT_SEC):
        return jsonify({"error": "timed out"}), 504
    if result["busy"]:
        return jsonify({"error": "Jarvis is busy"}), 409
    return jsonify({"reply": result["reply"]})


@app.route("/api/chat/cancel", methods=["POST"])
def chat_cancel() -> Response:
    daemon.cancel_active_chat_query()
    return jsonify({"ok": True})


@app.route("/api/chat/new", methods=["POST"])
def chat_new() -> Response:
    ok = daemon.new_chat_session()
    return jsonify({"ok": ok}), 200 if ok else 409


@app.route("/api/chat/history")
def chat_history() -> Response:
    return jsonify({"messages": daemon.get_hot_window_messages()})


@sock.route("/ws")
def ws(socket) -> None:
    """Mic PCM in (binary, 16 kHz mono int16); events and speech audio out."""
    client = _Client()
    client.send(json.dumps({"type": "state", "state": face_state.get_jarvis_state().state.value}))
    with _clients_lock:
        _clients.add(client)
    debug_log("browser client connected", "server")
    try:
        while True:
            # One thread per socket does both directions, so sends never interleave.
            while True:
                try:
                    socket.send(client.outbox.get_nowait())
                except queue.Empty:
                    break
            message = socket.receive(timeout=0.02)
            if isinstance(message, (bytes, bytearray)):
                browser_audio.feed_microphone(bytes(message), source=client)
            elif message and json.loads(message).get("type") == "claim_mic":
                browser_audio.claim_microphone(client)
    except Exception as exc:
        debug_log(f"browser client disconnected: {exc}", "server")
    finally:
        with _clients_lock:
            _clients.discard(client)


def main() -> None:
    if not TOKEN:
        print("⚠️ JARVIS_TOKEN is not set: anyone who can reach this port can use Jarvis.", flush=True)
    server = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=PORT, debug=False, use_reloader=False, threaded=True),
        name="jarvis-web", daemon=True,
    )
    server.start()
    print(f"🌐 Jarvis server listening on port {PORT}", flush=True)
    daemon.main()


if __name__ == "__main__":
    main()
