import asyncio
import os
import logging
from dotenv import load_dotenv
from livekit import api, rtc

# Load environment variables
load_dotenv()

LIVEKIT_URL = os.getenv("LIVEKIT_URL")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
DEFAULT_ROOM = os.getenv("DEFAULT_ROOM", "demo")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("agent")

async def main():
    logger.info("Starting Agent Service...")

    if not all([LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET]):
        logger.error("Missing required environment variables (LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)")
        return

    # 1. Create a token for the agent
    logger.info(f"Generating token for identity: avatar-bot in room: {DEFAULT_ROOM}")
    token = api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET) \
        .with_identity("avatar-bot") \
        .with_name("Avatar Bot") \
        .with_grants(api.VideoGrants(room_join=True, room=DEFAULT_ROOM))
    
    jwt_token = token.to_jwt()

    # 2. Connect to the room
    room = rtc.Room()

    @room.on("connected")
    def on_connected():
        logger.info("Events: Agent successfully connected to the room!")

    @room.on("disconnected")
    def on_disconnected(reason=None):
        logger.info(f"Events: Agent disconnected. Reason: {reason}")

    logger.info(f"Connecting to LiveKit server at {LIVEKIT_URL}...")
    
    try:
        await room.connect(LIVEKIT_URL, jwt_token)
        logger.info(f"Agent joined room '{DEFAULT_ROOM}'")
        
        # 3. Keep the process alive
        # In a real agent, we would handle events here (listening to tracks, etc.)
        # For Step 2, we just stay connected.
        while True:
            await asyncio.sleep(1)
            
    except Exception as e:
        logger.error(f"Failed to connect: {e}")
    finally:
        await room.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
