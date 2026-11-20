# Edge TTS proxy — deploy on Coolify

Free voiceover for the reels (and the Leonida Daily YouTube Shorts):
Microsoft Edge read-aloud voices, no account, no card, no Google
billing. A tiny FastAPI service that turns a POST into an MP3.

## Deploy (Coolify)

Coolify v4.7.6 at https://deploy.prasiddha.top (1 server: localhost).
Sign-in works with the saved login (corrected via Secure Vault
2026-11-20). No Git sources connected.

Two routes (user picks):

**A. GitHub repo (recommended).** Put `app.py`, `requirements.txt`,
`Dockerfile` in a repo, then Coolify → New Resource → Public Git
Repository → paste the repo URL. Coolify detects the Dockerfile and
builds with the full file context; future updates are just a push.

**B. Self-contained Dockerfile paste (no repo).** `Dockerfile.self-contained`
in this folder embeds the whole app.py via heredoc (verified
byte-identical round-trip, compiles OK). Coolify → New Resource →
Dockerfile → paste the entire file. Note: the browser cannot paste,
so this route means typing a 128-line file or the user pasting it
himself; updates mean pasting again. Prefer route A.

In both routes: set the port (Coolify exposes the container on
`$PORT`, default 8000), health check path `/health`, and optionally
set env `EDGE_TTS_API_KEY` to a random string (send the same value as
`X-Api-Key` from n8n). Give the service a domain, e.g.
`https://tts.prasiddha.top` (or use Coolify's internal hostname from
n8n if both run on the same server).

## Test it

```bash
curl -s https://tts.prasiddha.top/health
# {"status":"ok"}

curl -s -X POST https://tts.prasiddha.top/tts \
  -H "Content-Type: application/json" \
  -H "X-Api-Key: <key if set>" \
  -d '{"text":"Empty plot to golden sky. Your hope ascends now.","page":"mojo"}' \
  -o test.mp3
ffprobe test.mp3   # expect mp3, 24kHz mono, a few seconds
```

## How n8n calls it (Mojo-only workflow)

After Parse, add an HTTP Request node (POST to the /tts URL, body =
`{"text": <hook + one line from Parse>, "page": "mojo"}`), then upload
the returned MP3 to Cloudinary as an audio/video resource
(public_id `voiceover-$execution.id`), then mix it into the video with
the music bed dropped to -18dB and the voice at +3dB (per
v4-audio-design-2026-11-07.md). The voiceover is ~8s for ~21 words, so
it sits in the first ~10s of the 50s reel. The node must FAIL LOUDLY
on any non-200 / empty audio — never publish a silent reel.

## Voices (defaults in app.py)

- Mojo: `en-US-ChristopherNeural` (male, reliable) — sample:
  `mojo-sample-christopher.mp3` (8.4s, -19.9 dB mean)
- Dharma: `hi-IN-MadhurNeural` (male Hindi) — sample:
  `dharma-sample-madhur.mp3` (7.4s, -16.9 dB mean)
- Funny: `en-US-GuyNeural` · Home: `en-GB-RyanNeural` ·
  Leonida (YouTube): `en-US-ChristopherNeural`
- Override per request with `"voice": "<name>"`. Full list:
  `GET /voices?lang=en` (or `?lang=hi`).

## Known limits

- Unofficial Microsoft endpoint — it can break without warning. If it
  does, the fallback order is: Google Cloud TTS (blocked on billing
  OR-BACR2_59) or Google Translate TTS (test-only, flat voice).
  **2026-11-20: the plain edge-tts library (aiohttp) got HTTP 403 from
  Microsoft on the Coolify VPS** even though /health and /voices worked.
  Fix applied in app.py v1.1: edge-tts 7.3.1 (Sec-MS-GEC-Version
  2-143.0.3650.75) + the protocol spoken over websocket-client instead
  of aiohttp (ported from the proven local `ws_tts_local.py`), with a
  60s timeout so a hung synthesis fails loudly as 502. If it 403s
  again, check for a newer edge-tts release first — its constants carry
  the current token/version.
- The `mojo-music` track is Content ID registered — never reuse on
  YouTube; the Leonida Shorts are voice-only, no music.
- Local dev VM caveat: this VM's egress proxy mangles WebSocket
  upgrades, so the edge-tts library (aiohttp) cannot connect from
  here — that is a VM limitation, NOT a service bug. `ws_tts_local.py`
  in this folder is the websocket-client workaround used to make the
  samples; the service itself runs on Coolify with direct egress.

## Files

- `app.py` — the service · `requirements.txt` · `Dockerfile`
- `Dockerfile.self-contained` — route B: whole app.py embedded via
  heredoc (128 lines; round-trip verified byte-identical, compiles OK)
- `ws_tts_local.py` — local sample generator (proxy workaround)
- `mojo-sample-christopher.mp3`, `dharma-sample-madhur.mp3` — voice samples
