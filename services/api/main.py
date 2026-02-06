import os
import json
import time
import aiohttp
import ssl
import certifi
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from livekit import api
from dotenv import load_dotenv


load_dotenv()

app = FastAPI()

# Allow CORS for development (especially from the Next.js dev server)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # In production, specify the exact origin
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")

class TokenRequest(BaseModel):
    room: str
    identity: str

class SayRequest(BaseModel):
    room: str
    text: str

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.post("/token")
async def create_token(req: TokenRequest):
    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
         raise HTTPException(status_code=500, detail="Server misconfigured: missing LiveKit credentials")

    token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET) \
        .with_identity(req.identity) \
        .with_name(req.identity) \
        .with_grants(api.VideoGrants(room_join=True, room=req.room))
    
    return {"token": token.to_jwt(), "url": LIVEKIT_URL}

@app.post("/say")
async def say(req: SayRequest):
    if not LIVEKIT_URL or not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
         raise HTTPException(status_code=500, detail="Server misconfigured: missing LiveKit credentials")

    # 1. Protocol Conversion for API Context
    # LiveKitAPI expects HTTP/HTTPS, but LIVEKIT_URL is usually WSS
    api_url = LIVEKIT_URL
    if api_url.startswith("wss://"):
        api_url = api_url.replace("wss://", "https://")
    elif api_url.startswith("ws://"):
        api_url = api_url.replace("ws://", "http://")

    # 2. SSL Verification (Default to secure with Certifi, allow bypass)
    insecure = os.getenv("LIVEKIT_INSECURE_SKIP_VERIFY", "false").lower() == "true"
    
    if insecure:
        # Bypass SLL
        connector = aiohttp.TCPConnector(ssl=False)
    else:
        # Use Certifi for reliable SSL (fixes MacOS issues)
        ssl_ctx = ssl.create_default_context(cafile=certifi.where())
        connector = aiohttp.TCPConnector(ssl=ssl_ctx)
        
    session = aiohttp.ClientSession(connector=connector)

    try:
        # Initialize LiveKit API with corrected URL and optional custom session
        lkapi = api.LiveKitAPI(api_url, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, session=session)
    
        data = json.dumps({
            "type": "say",
            "text": req.text,
            "ts": int(time.time() * 1000)
        }).encode("utf-8")

        await lkapi.room.send_data(
            api.SendDataRequest(
                room=req.room,
                data=data,
                kind=1, # RELIABLE
                topic="say"
            )
        )
        await lkapi.aclose()
        
    except Exception as e:
        # 3. Enhanced Error Reporting
        import traceback
        traceback.print_exc()
        # Clean up session if it wasn't closed by lkapi (safe to call twice)
        if session:
            await session.close()
        raise HTTPException(status_code=500, detail=f"Failed to send data: {str(e)}")

    # Clean up session if successful
    if session:
        await session.close()
        
    return {"ok": True}
