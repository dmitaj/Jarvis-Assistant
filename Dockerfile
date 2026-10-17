# Jarvis server: the assistant as a network service for browser clients.
# Build: docker build -t jarvis-assistant .   Run: see docker-compose.yml
FROM python:3.11-slim-bookworm

# webrtcvad builds from source; tesseract backs the screenshot OCR tool.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential tesseract-ocr \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# Desktop-only packages are dropped: Qt UI, PyInstaller, global hotkeys,
# PortAudio (audio goes through the browser), Chatterbox (Piper is used),
# Apple MLX and the test tools.
RUN grep -viE '^(PyQt6|pyinstaller|pynput|sounddevice|chatterbox|pygame|mlx|pytest|nvidia|tzdata)' requirements.txt > requirements-server.txt \
 && pip install --no-cache-dir -r requirements-server.txt flask-sock==0.7.0 \
      nvidia-cublas-cu12==12.4.5.8 nvidia-cudnn-cu12==9.1.0.70

# CTranslate2 (faster-whisper) loads CUDA from the pip-installed NVIDIA wheels.
ENV LD_LIBRARY_PATH=/usr/local/lib/python3.11/site-packages/nvidia/cublas/lib:/usr/local/lib/python3.11/site-packages/nvidia/cudnn/lib

COPY src ./src
ENV PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    HOME=/data \
    XDG_CONFIG_HOME=/data/config \
    JARVIS_PORT=8765
VOLUME /data
EXPOSE 8765
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"JARVIS_PORT\"]}/healthz')"
CMD ["python", "-m", "jarvis_server"]
