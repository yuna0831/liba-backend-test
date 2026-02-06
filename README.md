# Liba Backend Test

A real-time digital human demo integration skeleton. This project demonstrates the coordination between a Next.js frontend, a FastAPI backend, and a LiveKit Agent (Python) to drive a Tavus Persona.

## Architecture

    +-----------+        +-------------+        +-----------------+
    |           |  REST  |             |  GRPC  |                 |
    | apps/web  +--------> services/api+--------> LiveKit Cloud   |
    | (Next.js) |        |  (FastAPI)  |        |                 |
    |           |        |             |        |                 |
    +---+-------+        +-------------+        +--------+--------+
        |                                                ^
        | WebRTC                                         | WebRTC
        |                                                |
        v                                                v
    (User Browser) <---------------------------> (services/agent)
                                                  (LiveKit Agent)

## Components

1.  **apps/web**: Next.js frontend. Handles user input, requests tokens, and connects to the LiveKit room.
2.  **services/api**: FastAPI backend. Issues access tokens for LiveKit and provides `/say` endpoint.
3.  **services/agent**: LiveKit Agent (Python). Manages Tavus integration, OpenAI TTS, and audio routing.

## Status

- [x] Basic project structure
- [x] API Token issuance
- [x] Web frontend connects to LiveKit
- [x] Agent connects to LiveKit
- [x] /say endpoint (server sends data packet topic="say")
- [x] OpenAI TTS speaking (button triggers audio output)
- [x] Tavus Persona integration (AvatarSession starts and remote participant `tavus-avatar-agent` joins)
- [x] Lip-sync routing (audio is routed to Tavus sink so avatar lip-sync works)
- [x] Speech truncation fix (silence tail padding)
- [x] Overlap prevention (speech serialized using asyncio.Lock)
- [ ] Remaining TODOs: latency tuning/metrics, UI polish

## Step 3: Make the Agent Speak (OpenAI TTS)

The agent uses OpenAI TTS to synthesize speech.

1.  **Trigger**: The web UI "Speak" button sends a data packet with `topic="say"` containing `{text: "..."}`.
2.  **Synthesis**: The agent listens for `data_received`, parses the text, and calls `speak_text(text)`.
3.  **Fallback**: In the initial implementation (or when Tavus fails), the agent publishes a local audio track (`agent_speech`) to the room.

**Relevant logs:**
- `Received data packet from server: topic='say'`
- `Synthesizing speech: 'Hello...'`
- `Published audio track for speech: ...`

## Step 4: Tavus Integration + Lip Sync

The actual implementation routes the synthesized audio to Tavus to drive the lip-sync.

1.  **Tavus Session**: We start a `tavus.AvatarSession` with `persona_id` and `replica_id`.
2.  **Remote Participant**: The Tavus plugin spawns a remote participant (`tavus-avatar-agent`) which publishes video/audio tracks.
3.  **Audio Routing**:
    - The Tavus plugin operates in an "Echo/Voicebox" style. To drive lip-sync, we pipe audio frames into the Tavus audio sink.
    - **Logic**: If Tavus is ready, we route frames to `session_wrapper.output.audio.capture_frame(frame)`.
    - **Fallback**: If Tavus is NOT ready (or 402 Out of Credits), we fallback to the local audio track so the user still hears speech.

**Routing Behavior (Logs):**
- **If Tavus ready**: `ROUTE=tavus | Target=tavus-avatar-agent` (Audio comes from Avatar, lips move).
- **If Fallback**: `ROUTE=fallback | Reason=Tavus Not Ready` (Audio comes from Agent, no video).

**Note**:
- `TAVUS_PERSONA_ID` and `TAVUS_REPLICA_ID` (and API Key) are required for Tavus.
- `OPENAI_API_KEY` is required for TTS.

## Bug Fixes

### Fix: Last word cut off / speech truncation
**Issue**: Lip-sync worked, but the last word often got cut off (e.g., "microphone" -> "micro...").
**Cause**: Tavus requires a silence tail to finish processing the final phonemes of a stream.
**Fix**: We append a short silent PCM tail (300ms) after each utterance.

```python
# agent.py helper
def make_silence_frame(sample_rate=24000, ms=300):
   ...
# In speak_text loop:
await tavus_sink.capture_frame(silence)
```

### Fix: Overlapping utterances
**Issue**: Rapid "Speak" requests caused audio segments to overlap (e.g., "Two... Hello...").
**Fix**: Serialized speech using an `asyncio.Lock`. Only one sentence is spoken at a time.

```python
speak_lock = asyncio.Lock()
async with speak_lock:
    # synthesize and stream...
```

## How to run

### 1. Requirements & Credentials
Ensure `services/agent/.env` contains:
```env
LIVEKIT_URL=...
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
OPENAI_API_KEY=...
TAVUS_API_KEY=...
TAVUS_PERSONA_ID=...
TAVUS_REPLICA_ID=...
```

### 2. Start Services
1.  **API**:
    ```bash
    cd services/api
    uvicorn main:app --reload
    ```
2.  **Agent**:
    ```bash
    cd services/agent
    python agent.py dev
    ```
3.  **Web**:
    ```bash
    cd apps/web
    npm run dev
    ```

### 3. Verification
1.  Open LiveKit Meet or Web App: `http://localhost:3000` or `https://meet.livekit.io/?tab=custom`.
2.  Join room `demo`.
3.  **Press Speak** via the Web UI.
4.  **Expected**:
    - `tavus-avatar-agent` video is visible.
    - Mouth moves in sync with the spoken text.
    - Audio comes from the Tavus participant.
    - Logs confirm `ROUTE=tavus`.
