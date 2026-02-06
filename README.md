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

## Step 5: Latency Metrics

The agent now logs detailed timing for every spoken utterance to help tune performance.

**Log Format:**
`METRICS | uid=... | route=tavus | ms_tts_start=... | ms_first_audio=... | ms_total_approx=... | frames=...`

- **ms_tts_start**: Time from `say` command receipt -> First audio frame synthesized.
- **ms_first_audio**: Time from `say` command receipt -> First audio frame delivered to sink (Avatar or Fallback).
- **ms_total_approx**: Time until playback finishes (including tail padding).


## Latency Optimization

### Problem
Real-time digital humans must feel responsive. A noticeable delay between text input and the avatar speaking breaks immersion. The goal was to minimize end-to-end latency ("time to first word") and make performance measurable.

### Instrumentation (T0~T3)
To optimize effectively, I moved from guessing to measuring. A `MetricsStore` was implemented to track timestamps for every utterance (`uid`):

- **T0 (Receipt):** `say` data packet received by Agent.
- **T1 (First Audio):** First PCM frame produced by OpenAI TTS stream.
- **T2 (Sink Delivery):** First PCM frame pushed to Tavus `capture_frame()`.
- **T3 (Playback Done):** `playback_finished` event received (correlated via FIFO).

### Changes Made
1. **TTS Warm-up:** Implemented a silent `synthesize("warmup")` call on startup. This pre-heats the connection, shifting the initial ~1.7s cold-start delay to boot time rather than the first user interaction.
2. **Text Chunking:** Added logic (`MAX_TEXT_CHUNK=120`) to split long text on punctuation. This forces the TTS engine to return the first audio chunk sooner, rather than waiting to process a full paragraph.
3. **Silence Tail Tuning:** Reduced tail padding (`SILENCE_TAIL_MS`) from 300ms to 120ms to reduce perceived "hanging" at the end of speech while preventing cutoff.
4. **Tavus Readiness Gating:** Wait up to 5s for `tavus_ready` event to avoid losing the first few seconds of speech during initialization.

### Results
Logs from a live session demonstrate the pipeline latency:

- **Run #1 (Standard):** `d01` (T0→T1) ≈ **666ms**. `d12` (T1→T2) ≈ **7ms**.
  - Total time to first audio delivered: **~673ms**.
- **Run #2 (Variable):** `d01` ≈ 1503ms (Network/Model variance).
- **Run #3 (Stabilized):** `d01` ≈ 978ms.

**Key Finding:** The sink delivery time (`d12`) is consistently negligible (~2-7ms). The primary bottleneck is `d01` (TTS generation time), confirming that our streaming optimization and warm-up strategies are targeting the right area.

### Tradeoffs
- **Granularity vs. Context:** Chunking long text improves latency but can slightly affect prosody (intonation) across chunk boundaries.
- **Tail Padding:** Too little padding risks cutting off the final syllable; 120ms was found to be a safe sweet spot.
- **Correlation:** Without explicit utterance IDs from the Tavus player event, T3 mapping relies on a FIFO assumption, which is accurate for sequential speech but theoretical for overlapping commands.

### Next Steps
- **Frame Slicing:** Manually slice 200ms TTS frames into 50-100ms chunks to push audio to the sink even faster.
- **T3 Correlation:** Parse detailed Tavus `app_messages` to find a robust per-utterance identifier.
- **Jitter Buffer:** Implement an adaptive jitter buffer if A/V sync drift is observed under poor network conditions.

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
