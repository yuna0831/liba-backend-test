import logging
import os
import ssl
import certifi
import aiohttp
import asyncio
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.plugins import tavus

# Load environment variables
load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("agent")

TAVUS_PERSONA_ID = os.getenv("TAVUS_PERSONA_ID")
TAVUS_REPLICA_ID = os.getenv("TAVUS_REPLICA_ID") # Optional

class MinimalOutput:
    def __init__(self):
        self.audio = None

class MinimalAgentSession:
    """
    A minimal wrapper around JobContext to satisfy livekit-plugins-tavus
    which expects an AgentSession object with an 'output' attribute.
    """
    def __init__(self, ctx: JobContext):
        self.ctx = ctx
        self.output = MinimalOutput()
        # Proxy other attributes to ctx if needed
        
    @property
    def room(self):
        return self.ctx.room

async def entrypoint(ctx: JobContext):
    """
    Entrypoint for the LiveKit Agent.
    """
    await ctx.connect()
    logger.info(f"Agent connected to room: {ctx.room.name}")

    if not TAVUS_PERSONA_ID:
        logger.error("TAVUS_PERSONA_ID is not set. Cannot start Tavus agent.")
        return
    if not TAVUS_REPLICA_ID:
        logger.error("TAVUS_REPLICA_ID is not set. Cannot start Tavus avatar session.")
        return

    try:
        logger.info(f"Starting avatar session for Persona: {TAVUS_PERSONA_ID} (replica: {TAVUS_REPLICA_ID})...")

        avatar = tavus.AvatarSession(
            persona_id=TAVUS_PERSONA_ID,
            replica_id=TAVUS_REPLICA_ID,
        )

        # Use the wrapper to satisfy the plugin interface
        session_wrapper = MinimalAgentSession(ctx)
        
        await avatar.start(session_wrapper, room=ctx.room)
        
        logger.info("Avatar started successfully.")

        # keep running until job is cancelled
        await asyncio.Future()

    except asyncio.CancelledError:
        logger.info("Agent task cancelled")
    except Exception as e:
        logger.exception(f"Failed to start Tavus Avatar: {e}")
        await asyncio.sleep(5)


if __name__ == "__main__":
    # Fix SSL usage globally for requests/other libs just in case
    os.environ['SSL_CERT_FILE'] = certifi.where()

    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name="avatar-bot",
    ))
