# main.py
import os
import json
import time
import ssl
import certifi
import hashlib
import asyncio
from typing import Dict, Tuple, Optional

import aiohttp
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from livekit import api as lk_api

load_dotenv()

# ----------------------------
# App + CORS
# ----------------------------
app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # TODO: production에서는 정확한 origin으로 제한
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------
# Env
# ----------------------------
LIVEKIT_URL = os.getenv("LIVEKIT_URL")  # 보통 wss://...
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")

# Optional knobs (safe defaults)
LIVEKIT_INSECURE_SKIP_VERIFY = os.getenv("LIVEKIT_INSECURE_SKIP_VERIFY", "false").lower() == "true"
SAY_DEDUPE_WINDOW_SEC = float(os.getenv("SAY_DEDUPE_WINDOW_SEC", "1.5"))
SAY_TIMEOUT_SEC = float(os.getenv("SAY_TIMEOUT_SEC", "5.0"))  # LiveKit API call timeout
AIOHTTP_TOTAL_TIMEOUT_SEC = float(os.getenv("AIOHTTP_TOTAL_TIMEOUT_SEC", "8.0"))

# ----------------------------
# Models
# ----------------------------
class TokenRequest(BaseModel):
    room: str
    identity: str

class SayRequest(BaseModel):
    room: str
    text: str

# ----------------------------
# Helpers
# ----------------------------
def _require_livekit():
    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        raise HTTPException(status_code=500, detail="Server misconfigured: missing LiveKit credentials")

def _to_api_url(url: str) -> str:
    # LiveKitAPI expects http(s), but LIVEKIT_URL is often ws(s)
    if url.startswith("wss://"):
        return url.replace("wss://", "https://", 1)
    if url.startswith("ws://"):
        return url.replace("ws://", "http://", 1)
    return url

def _hash_key(room: str, text: str) -> str:
    h = hashlib.sha256()
    h.update(room.encode("utf-8"))
    h.update(b"|")
    h.update(text.strip().encode("utf-8"))
    return h.hexdigest()

# ----------------------------
# Global state (startup/shutdown에서 세팅)
# ----------------------------
class AppState:
    session: Optional[aiohttp.ClientSession] = None
    connector: Optional[aiohttp.TCPConnector] = None
    dedupe_lock: asyncio.Lock = asyncio.Lock()
    # key -> last_seen_monotonic
    dedupe_cache: Dict[str, float] = {}

state = AppState()

# ----------------------------
# Startup / Shutdown
# ----------------------------
@app.on_event("startup")
async def on_startup():
    _require_livekit()

    # SSL / Connector
    if LIVEKIT_INSECURE_SKIP_VERIFY:
        connector = aiohttp.TCPConnector(ssl=False)
    else:
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)

    timeout = aiohttp.ClientTimeout(total=AIOHTTP_TOTAL_TIMEOUT_SEC)

    state.connector = connector
    state.session = aiohttp.ClientSession(connector=connector, timeout=timeout)

@app.on_event("shutdown")
async def on_shutdown():
    if state.session and not state.session.closed:
        await state.session.close()
    state.session = None
    # connector는 session close로 같이 정리되지만 명시적으로 None 처리
    state.connector = None

# ----------------------------
# Dedupe
# ----------------------------
async def should_drop_duplicate(room: str, text: str) -> bool:
    """
    (room,text) 기준으로 SAY_DEDUPE_WINDOW_SEC 내 중복 요청이면 drop.
    """
    key = _hash_key(room, text)
    now = time.monotonic()

    async with state.dedupe_lock:
        # purge old
        cutoff = now - SAY_DEDUPE_WINDOW_SEC
        # 작은 dict라 간단 purge
        to_delete = [k for k, t0 in state.dedupe_cache.items() if t0 < cutoff]
        for k in to_delete:
            del state.dedupe_cache[k]

        last = state.dedupe_cache.get(key)
        if last is not None and (now - last) < SAY_DEDUPE_WINDOW_SEC:
            return True

        state.dedupe_cache[key] = now
        return False

# ----------------------------
# Routes
# ----------------------------
@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.post("/token")
async def create_token(req: TokenRequest):
    _require_livekit()

    token = (
        lk_api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(req.identity)
        .with_name(req.identity)
        .with_grants(lk_api.VideoGrants(room_join=True, room=req.room))
    )
    return {"token": token.to_jwt(), "url": LIVEKIT_URL}

@app.post("/say")
async def say(req: SayRequest):
    _require_livekit()

    text = (req.text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text is required")

    # Server-side dedupe (protects against double click / retries)
    if await should_drop_duplicate(req.room, text):
        # 200으로 “이미 처리됨” 처리 (클라에서 에러로 안 보이게)
        return {"ok": True, "deduped": True}

    api_url = _to_api_url(LIVEKIT_URL)

    if not state.session:
        raise HTTPException(status_code=500, detail="HTTP session not initialized (startup not run?)")

    # Prepare payload
    data = json.dumps(
        {
            "type": "say",
            "text": text,
            "ts": int(time.time() * 1000),
        }
    ).encode("utf-8")

    lk = None
    try:
        lk = lk_api.LiveKitAPI(api_url, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, session=state.session)

        # Timeout wrapper (LiveKit API call)
        await asyncio.wait_for(
            lk.room.send_data(
                lk_api.SendDataRequest(
                    room=req.room,
                    data=data,
                    kind=1,  # RELIABLE
                    topic="say",
                )
            ),
            timeout=SAY_TIMEOUT_SEC,
        )

        # LiveKitAPI는 close로 내부 리소스 정리 (session은 재사용)
        await lk.aclose()
        return {"ok": True, "deduped": False}

    except asyncio.TimeoutError:
        # lk가 살아있으면 정리
        try:
            if lk:
                await lk.aclose()
        except Exception:
            pass
        raise HTTPException(status_code=504, detail=f"/say timed out after {SAY_TIMEOUT_SEC}s")

    except Exception as e:
        # lk 정리
        try:
            if lk:
                await lk.aclose()
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=f"Failed to send data: {str(e)}")
