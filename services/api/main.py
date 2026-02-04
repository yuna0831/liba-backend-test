import os
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
