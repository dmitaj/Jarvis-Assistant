# Jarvis server on Unraid

Jarvis runs as a container on Tower. Speech recognition runs on the Tesla P40 and the models on
the existing Ollama container. The wall panel ([HA-Dash](https://github.com/dmitaj/HA-Dash), branch
`jarvis`) streams its microphone to Jarvis and plays the spoken replies. Jarvis controls the house
through Home Assistant's MCP Server integration.

```
Surface (Chrome, https://ha-display.harran.ca)
  └─ /jarvis/  ──HA session check──▶ ha-dash-portal (nginx) ──Bearer JARVIS_TOKEN──▶ jarvis-assistant :8765
                                                                                     ├─▶ Ollama 192.168.111.62
                                                                                     └─▶ HA /api/mcp (mcp-proxy)
```

## Install

```sh
cd /mnt/user/appdata && git clone https://github.com/dmitaj/Jarvis-Assistant.git jarvis-assistant
cd jarvis-assistant
cp .env.sample .env && sed -i "s/^JARVIS_TOKEN=.*/JARVIS_TOKEN=$(openssl rand -hex 32)/" .env && chmod 600 .env
mkdir -p data/config/jarvis && cp deploy/config.sample.json data/config/jarvis/config.json
chmod 600 data/config/jarvis/config.json   # then put the HA long-lived token in it
docker exec ollama ollama pull nomic-embed-text   # memory search; gemma4:26b must be there too
# The JARVIS voice (Piper, MIT): https://huggingface.co/jgkawell/jarvis. Medium renders in ~0.4 s;
# high sounds a little richer but takes ~2 s per reply on Tower's CPU.
V=data/.local/share/jarvis/models/piper && mkdir -p $V && for f in jarvis-medium.onnx jarvis-medium.onnx.json; do
  curl -fL -o $V/$f https://huggingface.co/jgkawell/jarvis/resolve/main/en/en_GB/jarvis/medium/$f; done
../ha-dash/bin/docker-compose up -d --build
```

The first start downloads Whisper `medium` and the Piper voice into `data/`. Check
`docker logs jarvis-assistant` for `✅ home_assistant: N tools available` and `🎙️ Listening!`.

**Wall panel:** put `JARVIS_URL=http://192.168.111.68:8765` and the same `JARVIS_TOKEN` in HA-Dash's
`.env`, then rebuild it. Jarvis only sees entities exposed to Assist (HA: *Settings → Voice
assistants → Expose*).

## Remote chat model (Microsoft Foundry, optional)

Fill `FOUNDRY_ENDPOINT`, `FOUNDRY_API_KEY` and `FOUNDRY_CHAT_DEPLOYMENT` in `.env` (optionally
`FOUNDRY_FAST_DEPLOYMENT` for the intent judge and tool router), then `../ha-dash/bin/docker-compose up -d`.
The endpoint is the resource's OpenAI v1 URL, e.g. `https://<resource>.openai.azure.com/openai/v1`.
Compose turns them into `JARVIS_CFG_*` overrides of `config.json`. Whisper, Piper, embeddings and
the HA fast path stay local. Requests that miss the fast path, with the HA live context and memory
snippets Jarvis attaches, go to Foundry. Blank `FOUNDRY_ENDPOINT` and run `up -d` again to go back
to Ollama.

## Update

```sh
cd /mnt/user/appdata/jarvis-assistant && git pull && ../ha-dash/bin/docker-compose up -d --build
```

`docker restart` keeps the old image. Use `up -d --build` after a pull.

## Try it

- `http://192.168.111.68:8765/?token=<JARVIS_TOKEN>`: test page with mic, typed chat and speech.
  Browsers allow the mic only over HTTPS or localhost, so use typed chat there, or the panel.
- `/memory` on the same address: diary and knowledge-graph viewer.
- `curl -H "Authorization: Bearer $T" -d '{"text":"Which lights are on?"}' -H 'Content-Type: application/json' http://192.168.111.68:8765/api/chat`

## Notes

- **Models:** `gemma4:26b` serves every tier, so one model stays resident (~17 GB), plus Whisper
  (~1.5 GB) and the embedder. Pinned by `JARVIS_OLLAMA_KEEP_ALIVE=-1`. Other Ollama users can
  still evict it when they need the VRAM; the next question then waits ~45 s while it reloads.
- **Mute:** the mic button at the panel's bottom right. Red means muted.
- **Privacy:** audio is transcribed on Tower and never leaves the house. It isn't saved.
