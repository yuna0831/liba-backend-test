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
2.  **services/api**: FastAPI backend. Issues access tokens for LiveKit.
3.  **services/agent**: LiveKit Agent. Connects to the room as a participant (skeleton for future Tavus integration).

## Getting Started

### Prerequisites

- Node.js & npm
- Python 3.9+
- LiveKit Cloud Credentials (URL, API Key, API Secret)

### 1. Setup Services/API

```bash
cd services/api
pip install -r requirements.txt
cp .env.example .env
# Edit .env and add your LiveKit credentials
uvicorn main:app --reload
```
API will be running at `http://localhost:8000`.

### 2. Setup Services/Agent

```bash
cd services/agent
pip install -r requirements.txt
cp .env.example .env
# Edit .env (same credentials as API)
python agent.py
```

### 3. Setup Apps/Web

```bash
cd apps/web
npm install
cp .env.example .env.local
npm run dev
```
Open `http://localhost:3000`.

## Status

- [x] Basic project structure
- [x] API Token issuance
- [x] Web frontend (Connect to LiveKit)
- [x] Agent skeleton (Connect to LiveKit)
- [ ] **TODO**: Implement `/say` endpoint in API
- [ ] **TODO**: Implement Tavus integration in Agent
- [ ] **TODO**: Lip-sync handling

## Step 2: Agent Joins the Room

In this step, we've implemented a basic Agent service that strictly connects to a specific room as a participant. It does not yet speak or listen, but it establishes the WebRTC presence required for future steps.

**How to run the Agent:**
1. Navigate to `services/agent`.
2. Ensure `.env` is set up with `LIVEKIT_URL`, `API_KEY`, `API_SECRET`, and `DEFAULT_ROOM` (e.g., "demo").
3. Run `python agent.py`.
4. The logs will show "Agent joined room 'demo'".
5. In the LiveKit Cloud dashboard (or your frontend), you will see "avatar-bot" has joined.

