"""Edge TTS proxy service — plain HTTP in, MP3 out.

Why: Microsoft Edge's read-aloud voices (free, no account, no card)
only speak over a WebSocket, which n8n cannot call directly. This
service wraps the edge-tts library behind a simple POST endpoint that
n8n's HTTP Request node can call.

Endpoints:
  GET  /health            -> {"status": "ok"}
  GET  /voices?lang=en    -> list of available voices (optional filter)
  POST /tts               -> MP3 audio (audio/mpeg)
       body: {"text": "...", "voice": "en-US-ChristopherNeural",
              "rate": "+0%", "volume": "+0%", "pitch": "+0Hz"}
       optional header: X-Api-Key (required only if EDGE_TTS_API_KEY is set)

Notes:
- On the Coolify VPS (direct egress) the edge-tts library works as-is.
- On THIS dev VM the egress proxy mangles WebSocket upgrades, so the
  aiohttp transport inside edge-tts fails there; see ws_tts_local.py
  for the websocket-client workaround used to make the local samples.
  If the VPS ever sits behind such a proxy, port that workaround here.
- Text is capped at 500 chars per request (our voiceovers are ~150).
  Edge TTS is an unofficial, undocumented Microsoft endpoint — it can
  break without warning. The caller (n8n) must fail loudly, never
  silently publish a silent reel.
"""
import os
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import edge_tts
import requests
from edge_tts.constants import VOICE_LIST

app = FastAPI(title="Edge TTS proxy", version="1.0.0")

API_KEY = os.environ.get("EDGE_TTS_API_KEY", "")
MAX_CHARS = 500

# Default voices per page (user can override per request).
DEFAULT_VOICES = {
    "mojo": "en-US-ChristopherNeural",   # male, reliable/authority
    "dharma": "hi-IN-MadhurNeural",      # male Hindi
    "funny": "en-US-GuyNeural",          # male, passion (humour)
    "home": "en-GB-RyanNeural",          # male, friendly/positive
    "leonida": "en-US-ChristopherNeural",  # YouTube GTA VI news channel
}


class TTSRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_CHARS)
    voice: Optional[str] = None
    page: Optional[str] = None
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"


def _check_key(x_api_key: Optional[str]) -> None:
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/voices")
async def voices(lang: Optional[str] = None):
    # Plain requests (not edge_tts.list_voices, which uses aiohttp): it
    # honours the system CA bundle, so it also works behind a TLS
    # intercepting egress proxy. On Coolify both paths work.
    try:
        resp = requests.get(VOICE_LIST, timeout=15)
        resp.raise_for_status()
        all_voices = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"voice list failed: {e}")
    if lang:
        lang = lang.lower()
        all_voices = [v for v in all_voices
                      if v.get("Locale", "").lower().startswith(lang)]
    return JSONResponse([{"name": v["ShortName"], "locale": v["Locale"],
                          "gender": v["Gender"], "style": v.get("Style", "")}
                         for v in all_voices])


@app.post("/tts")
async def tts(req: TTSRequest, x_api_key: Optional[str] = Header(default=None)):
    _check_key(x_api_key)
    voice = req.voice
    if not voice and req.page:
        voice = DEFAULT_VOICES.get(req.page.lower())
    if not voice:
        voice = DEFAULT_VOICES["mojo"]
    communicate = edge_tts.Communicate(
        req.text, voice, rate=req.rate, volume=req.volume, pitch=req.pitch
    )
    try:
        audio = b""
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                audio += chunk["data"]
    except Exception as e:
        # Fail loudly — the caller must never silently publish a reel
        # with no voiceover.
        raise HTTPException(status_code=502, detail=f"edge tts failed: {e}")
    if not audio:
        raise HTTPException(status_code=502, detail="edge tts returned no audio")
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"X-Voice": voice, "X-Audio-Bytes": str(len(audio))})
