// Jarvis voice overlay for a wall panel: <script src="https://panel/jarvis/overlay.js"></script>
//
// Streams the page's microphone to Jarvis, plays its speech, and pops up a card while Jarvis is
// listening, thinking or answering. Hidden otherwise, apart from a small mic button that mutes it.
// Paths are relative to this script's URL, so it works wherever the server is mounted.
//
// Behind a proxy that checks the viewer's Home Assistant session (HA-Dash's nginx), the page can
// define window.jarvisAccessToken = async () => '<HA access token>'; the overlay puts it in the
// ha_dash_token cookie for this path before connecting, the way the panel does for go2rtc.
(() => {
  const base = new URL('.', document.currentScript.src);
  const HIDE_AFTER_MS = 12000;

  const worklet = `registerProcessor('pcm', class extends AudioWorkletProcessor {
    constructor() { super(); this.buf = []; }
    process([input]) {
      const ch = input[0];
      if (ch) for (const s of ch) this.buf.push(Math.max(-1, Math.min(1, s)) * 32767);
      if (this.buf.length >= 640) { this.port.postMessage(Int16Array.from(this.buf).buffer, []); this.buf = []; }
      return true;
    }
  });`;

  const MIC = '<svg viewBox="0 0 24 24"><path d="M12 2a3 3 0 0 1 3 3v6a3 3 0 0 1-6 0V5a3 3 0 0 1 3-3m7 9c0 3.53-2.61 6.44-6 6.93V21h-2v-3.07c-3.39-.49-6-3.4-6-6.93h2a5 5 0 0 0 10 0z"/></svg>';
  const MIC_OFF = '<svg viewBox="0 0 24 24"><path d="M19 11c0 1.19-.34 2.3-.9 3.28l-1.23-1.23c.27-.62.43-1.31.43-2.05zm-4 .16L9 5.18V5a3 3 0 0 1 6 0zM4.27 3 21 19.73 19.73 21l-4.19-4.19c-.77.46-1.63.77-2.54.91V21h-2v-3.28c-3.28-.49-6-3.31-6-6.72h1.7c0 3 2.54 5.1 5.3 5.1.81 0 1.6-.19 2.31-.52l-1.66-1.66L12 14a3 3 0 0 1-3-3v-.72l-6-6z"/></svg>';
  const LABEL = { listening: 'Listening…', thinking: 'Thinking…', speaking: 'Speaking' };

  class JarvisOverlay extends HTMLElement {
    connectedCallback() {
      this.attachShadow({ mode: 'open' }).innerHTML = `
        <style>
          :host { position: fixed; inset: auto 0 24px 0; z-index: 100; pointer-events: none;
            font: 400 var(--t-body, 17px) / 1.4 var(--text, system-ui, sans-serif); color: var(--linen, #efe7da); }
          .card { pointer-events: auto; margin: 0 auto; width: min(640px, calc(100vw - 32px)); box-sizing: border-box;
            padding: 16px 20px; border-radius: var(--radius, 10px);
            background: linear-gradient(rgb(31 26 23 / 0.82), rgb(31 26 23 / 0.82)), var(--frost-image, none) center / cover fixed no-repeat var(--bark, #2d2622);
            box-shadow: var(--glass-edge, none), 0 12px 40px rgb(0 0 0 / 0.5);
            transition: opacity .25s, translate .25s; }
          :host(:not([open])) .card { opacity: 0; translate: 0 24px; pointer-events: none; }
          .state { display: flex; align-items: center; gap: 10px; font-size: var(--t-small, 15px); color: var(--linen-2, #aaa); }
          .dot { width: 10px; height: 10px; border-radius: 50%; background: var(--lamp, #f2a23a); }
          [data-state="listening"] .dot, [data-state="speaking"] .dot { animation: pulse 1s ease-in-out infinite; }
          [data-state="thinking"] .dot { animation: pulse .5s ease-in-out infinite alternate; opacity: .5; }
          @keyframes pulse { 50% { scale: 1.6; opacity: .5; } }
          .heard { margin-top: 8px; color: var(--linen-2, #aaa); }
          .heard:empty, .reply:empty { display: none; }
          .reply { margin-top: 8px; font: 400 var(--t-head, 21px) / 1.35 var(--display, Georgia, serif); max-height: 40vh; overflow: auto; }
          .mic { pointer-events: auto; position: fixed; right: 12px; bottom: 12px; width: 36px; height: 36px; padding: 8px;
            box-sizing: border-box; border: 0; border-radius: 50%; background: var(--bark, #2d2622); box-shadow: var(--glass-edge, none);
            fill: var(--linen-3, #888); cursor: pointer; }
          .mic.off { fill: #fff; background: var(--ember, #e4574a); }
          .mic.down { fill: var(--ember, #e4574a); }
          @media (prefers-reduced-motion: reduce) { .card { transition: none; } .dot { animation: none !important; } }
        </style>
        <div class="card" role="status" aria-live="polite">
          <div class="state"><span class="dot"></span><span class="label"></span></div>
          <div class="heard"></div>
          <div class="reply"></div>
        </div>
        <button class="mic" type="button"></button>`;
      this.$ = (s) => this.shadowRoot.querySelector(s);
      this.$('.card').onclick = () => this.hide();
      this.$('.mic').onclick = () => this.setMuted(!this.muted);
      this.muted = localStorage.getItem('jarvis.muted') === '1';
      this.setMuted(this.muted);
      this.connect();
      this.startMic();
    }

    // --- Connection --------------------------------------------------------
    async connect() {
      try {
        const token = await window.jarvisAccessToken?.();
        if (token) document.cookie = `ha_dash_token=${token}; path=${base.pathname}; max-age=3600; samesite=strict; secure`;
      } catch (err) { console.warn('Jarvis: could not get an access token', err); }
      const ws = new WebSocket(new URL('ws', base).href.replace(/^http/, 'ws'));
      ws.binaryType = 'arraybuffer';
      ws.onopen = () => { this.ws = ws; this.updateMic(); };
      ws.onmessage = (e) => (typeof e.data === 'string' ? this.onEvent(JSON.parse(e.data)) : this.play(e.data));
      ws.onclose = () => { this.ws = null; this.updateMic(); setTimeout(() => this.connect(), 3000); };
    }

    onEvent(ev) {
      if (ev.type === 'state') this.setState(ev.state);
      if (ev.type === 'query') { this.$('.heard').textContent = `“${ev.text}”`; this.$('.reply').textContent = ''; this.show(); }
      if (ev.type === 'reply' && ev.text) { this.$('.reply').textContent = ev.text; this.show(); this.hideLater(); }
    }

    setState(state) {
      this.state = state;
      this.$('.card').dataset.state = state;
      if (LABEL[state]) {
        this.$('.label').textContent = LABEL[state];
        if (state === 'listening' && !this.hasAttribute('open')) { this.$('.heard').textContent = ''; this.$('.reply').textContent = ''; }
        this.show();
      } else {
        this.$('.label').textContent = 'Jarvis';
        this.hideLater();
      }
    }

    show() { clearTimeout(this.hideTimer); this.setAttribute('open', ''); }
    hide() { clearTimeout(this.hideTimer); this.removeAttribute('open'); }
    hideLater() {
      clearTimeout(this.hideTimer);
      if (LABEL[this.state]) return;  // still busy: wait for idle
      // Long answers stay up long enough to read.
      const words = this.$('.reply').textContent.split(/\s+/).length;
      this.hideTimer = setTimeout(() => this.hide(), Math.max(HIDE_AFTER_MS, words * 400));
    }

    // --- Speech out: binary frames of [uint32 sample rate][int16 PCM] -----
    play(buf) {
      this.player ??= new AudioContext();
      const rate = new DataView(buf).getUint32(0, true);
      const pcm = new Int16Array(buf, 4);
      if (!pcm.length) return;
      const audio = this.player.createBuffer(1, pcm.length, rate);
      const ch = audio.getChannelData(0);
      for (let i = 0; i < pcm.length; i++) ch[i] = pcm[i] / 32768;
      const src = this.player.createBufferSource();
      src.buffer = audio;
      src.connect(this.player.destination);
      this.playAt = Math.max(this.playAt ?? 0, this.player.currentTime + 0.05);
      src.start(this.playAt);
      this.playAt += audio.duration;
    }

    // --- Mic in: 16 kHz mono int16, ~40 ms chunks --------------------------
    async startMic() {
      try {
        const stream = await navigator.mediaDevices.getUserMedia({
          audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        const ctx = new AudioContext({ sampleRate: 16000 });
        await ctx.audioWorklet.addModule(URL.createObjectURL(new Blob([worklet], { type: 'text/javascript' })));
        const node = new AudioWorkletNode(ctx, 'pcm');
        node.port.onmessage = (e) => { if (!this.muted && this.ws?.readyState === 1) this.ws.send(e.data); };
        ctx.createMediaStreamSource(stream).connect(node);
        // A lost device (unplugged, driver reset) ends the track: start over.
        stream.getAudioTracks()[0].onended = () => { ctx.close(); this.mic = false; this.updateMic(); setTimeout(() => this.startMic(), 5000); };
        this.mic = true;
      } catch (err) {
        console.warn('Jarvis: microphone unavailable, retrying', err);
        this.mic = false;
        setTimeout(() => this.startMic(), 30000);
      }
      this.updateMic();
    }

    setMuted(muted) {
      this.muted = muted;
      try { localStorage.setItem('jarvis.muted', muted ? '1' : '0'); } catch { /* private mode */ }
      this.updateMic();
    }

    updateMic() {
      const b = this.$('.mic');
      const down = !this.mic || !this.ws;
      b.className = 'mic' + (this.muted ? ' off' : down ? ' down' : '');
      b.innerHTML = this.muted ? MIC_OFF : MIC;
      b.setAttribute('aria-label', this.muted ? 'Jarvis is muted. Tap to listen for “Jarvis”'
        : down ? 'Jarvis is not connected' : 'Listening for “Jarvis”. Tap to mute');
    }
  }

  customElements.define('jarvis-overlay', JarvisOverlay);
  document.body.append(document.createElement('jarvis-overlay'));
})();
