"""Edge TTS proxy service — plain HTTP in, MP3 out.

Why: Microsoft Edge's read-aloud voices (free, no account, no card)
only speak over a WebSocket, which n8n cannot call directly. This
service wraps Edge TTS behind a simple POST endpoint that n8n's HTTP
Request node can call.

Endpoints:
  GET  /health            -> {"status": "ok"}
  GET  /voices?lang=en    -> list of available voices (optional filter)
  POST /tts               -> MP3 audio (audio/mpeg)
       body: {"text": "...", "voice": "en-US-ChristopherNeural",
              "rate": "+0%", "volume": "+0%", "pitch": "+0Hz"}
       optional header: X-Api-Key (required only if EDGE_TTS_API_KEY is set)

Transport note (2026-11-20): the plain edge-tts library (aiohttp
transport) gets HTTP 403 from Microsoft's WebSocket endpoint on the
Coolify VPS, even though the same library version worked locally.
The service therefore speaks the protocol itself over websocket-client
(ported from the proven local workaround ws_tts_local.py), reusing
edge-tts's own SSML builder and Sec-MS-GEC token helpers, on
edge-tts 7.3.1 (Sec-MS-GEC-Version 2-143.0.3650.75). If Microsoft
changes the handshake again, the first thing to check is a newer
edge-tts release — its constants carry the current token/version.

Notes:
- Text is capped at 1500 chars per request (Facebook voiceovers are ~150; Leonida YouTube scripts run ~890).
- Edge TTS is an unofficial, undocumented Microsoft endpoint — it can
  break without warning. The caller (n8n) must fail loudly, never
  silently publish a silent reel. This service does the same: every
  failure returns HTTP 502 with a message, never empty audio, and a
  hung synthesis is cut off at 60s.
"""
import asyncio
import os
import urllib.parse
import uuid
from typing import Optional
from xml.sax.saxutils import escape

from fastapi import FastAPI, Header, HTTPException, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

import requests
import websocket
from edge_tts.communicate import (
    TTSConfig, date_to_string, mkssml, ssml_headers_plus_data,
)
from edge_tts.constants import SEC_MS_GEC_VERSION, WSS_HEADERS, WSS_URL
from edge_tts.drm import DRM

app = FastAPI(title="Edge TTS proxy", version="1.1.0")

API_KEY = os.environ.get("EDGE_TTS_API_KEY", "")
MAX_CHARS = 1500
SYNTH_TIMEOUT = 60  # seconds; a hung WebSocket fails loudly, never hangs

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


def _synth_sync(text: str, voice: str, rate: str, volume: str, pitch: str) -> bytes:
    """One Edge TTS synthesis over websocket-client (sync; run in a thread).

    Same protocol the edge-tts library speaks, but over websocket-client,
    whose handshake Microsoft accepts where aiohttp's is refused (403).
    Honours HTTPS_PROXY when set (dev VM); direct connection otherwise
    (Coolify VPS). Raises on any failure — the caller turns it into 502.
    """
    tc = TTSConfig(voice=voice, rate=rate, volume=volume, pitch=pitch,
                   boundary="WordBoundary")
    cid = uuid.uuid4().hex
    url = (f"{WSS_URL}&ConnectionId={cid}"
           f"&Sec-MS-GEC={DRM.generate_sec_ms_gec()}"
           f"&Sec-MS-GEC-Version={SEC_MS_GEC_VERSION}")
    headers = dict(DRM.headers_with_muid(WSS_HEADERS))
    kwargs: dict = {"header": headers, "timeout": 30}
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or ""
    if proxy:
        p = urllib.parse.urlparse(proxy)
        kwargs["http_proxy_host"] = p.hostname
        kwargs["http_proxy_port"] = p.port
        if p.username and p.password:
            kwargs["http_proxy_auth"] = (
                urllib.parse.unquote(p.username),
                urllib.parse.unquote(p.password),
            )
    ws = websocket.create_connection(url, **kwargs)
    try:
        ws.send(
            f"X-Timestamp:{date_to_string()}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"true"},'
            '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
        )
        ws.send(ssml_headers_plus_data(cid, date_to_string(), mkssml(tc, escape(text))))
        audio = bytearray()
        while True:
            msg = ws.recv()
            if isinstance(msg, bytes):
                # Binary audio frame: 2-byte prefix + headers, then the
                # MP3 starts right after "Path:audio\r\n" — there is NO
                # blank line, so do NOT split on "\r\n\r\n".
                marker = b"Path:audio\r\n"
                idx = msg.find(marker)
                if idx != -1:
                    audio += msg[idx + len(marker):]
            elif "Path:turn.end" in msg:
                break
        return bytes(audio)
    finally:
        ws.close()


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/voices")
async def voices(lang: Optional[str] = None):
    # Plain requests (not edge_tts.list_voices, which uses aiohttp): it
    # honours the system CA bundle, so it also works behind a TLS
    # intercepting egress proxy. On Coolify both paths work.
    from edge_tts.constants import VOICE_LIST

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
    try:
        audio = await asyncio.wait_for(
            asyncio.to_thread(_synth_sync, req.text, voice,
                              req.rate, req.volume, req.pitch),
            timeout=SYNTH_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=502,
            detail=f"edge tts timed out after {SYNTH_TIMEOUT}s",
        )
    except Exception as e:
        # Fail loudly — the caller must never silently publish a reel
        # with no voiceover.
        raise HTTPException(status_code=502, detail=f"edge tts failed: {e}")
    if not audio:
        raise HTTPException(status_code=502, detail="edge tts returned no audio")
    return Response(content=audio, media_type="audio/mpeg",
                    headers={"X-Voice": voice, "X-Audio-Bytes": str(len(audio))})
