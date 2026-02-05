import asyncio
import os
import ssl
import certifi
from pathlib import Path
from dotenv import load_dotenv
from livekit import api
import aiohttp

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

ROOM = os.getenv("DEFAULT_ROOM", "demo")
AGENT_NAME = "avatar-bot"

async def main():
    # ✅ Force aiohttp to use certifi CA bundle
    ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_ctx)) as session:
        lkapi = api.LiveKitAPI(session=session)  # use our session
        dispatch = await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(room=ROOM, agent_name=AGENT_NAME)
        )
        print("Dispatch created:", dispatch)

if __name__ == "__main__":
    asyncio.run(main())
