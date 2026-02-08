import logging
import os
import ssl
import certifi
import json
import aiohttp
import asyncio
import time
import math
import struct
import uuid
import signal
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import JobContext, WorkerOptions, cli
from livekit.plugins import tavus
from livekit.plugins import openai
from livekit import rtc

from dataclasses import dataclass, field
from typing import Optional, Dict, Deque
from collections import deque


# ---------------- Metrics (t0~t3) ----------------

def now_ms() -> int:
    return int(time.perf_counter() * 1000)

@dataclass
class UtteranceMetrics:
    uid: str
    text: str = ""
    route: str = "tavus"
    t0_say_received: Optional[int] = None
    t1_first_audio: Optional[int] = None
    t2_first_sink_sent: Optional[int] = None
    t3_playback_finished: Optional[int] = None
    frames_sent: int = 0
    extra: Dict[str, int] = field(default_factory=dict)

class MetricsStore:
    def __init__(self):
        self.by_uid: Dict[str, UtteranceMetrics] = {}
        self.inflight_fifo: Deque[str] = deque()
        self._lock = asyncio.Lock()

    async def start(self, uid: str, text: str, route: str = "tavus", t0: Optional[int] = None) -> UtteranceMetrics:
        async with self._lock:
            m = UtteranceMetrics(uid=uid, text=text, route=route, t0_say_received=(t0 if t0 is not None else now_ms()))
            self.by_uid[uid] = m
            self.inflight_fifo.append(uid)
            return m

    async def mark_t1(self, uid: str):
        async with self._lock:
            m = self.by_uid.get(uid)
            if m and m.t1_first_audio is None:
                m.t1_first_audio = now_ms()

    async def mark_t2(self, uid: str):
        async with self._lock:
            m = self.by_uid.get(uid)
            if m and m.t2_first_sink_sent is None:
                m.t2_first_sink_sent = now_ms()

    async def inc_frames(self, uid: str, n: int = 1):
        async with self._lock:
            m = self.by_uid.get(uid)
            if m:
                m.frames_sent += n

    async def mark_t3_from_fifo(self) -> Optional[UtteranceMetrics]:
        async with self._lock:
            if not self.inflight_fifo:
                return None
            uid = self.inflight_fifo.popleft()
            m = self.by_uid.get(uid)
            if m:
                m.t3_playback_finished = now_ms()
            return m

    def summary_line(self, m: UtteranceMetrics) -> str:
        def d(a, b):
            return None if a is None or b is None else (b - a)

        t0, t1, t2, t3 = m.t0_say_received, m.t1_first_audio, m.t2_first_sink_sent, m.t3_playback_finished
        return (
            f"METRICS_T0T3 | uid={m.uid} | route={m.route} | "
            f"t0={t0} | t1={t1} | t2={t2} | t3={t3} | "
            f"d01={d(t0,t1)}ms | d02={d(t0,t2)}ms | d03={d(t0,t3)}ms | "
            f"d12={d(t1,t2)}ms | d23={d(t2,t3)}ms | "
            f"frames={m.frames_sent} | text='{m.text[:80]}'"
        )

metrics_store = MetricsStore()

# --------------------------------------------------


# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger("agent")

# Load environment variables
env_path = Path(__file__).resolve().parent / ".env"
logger.info(f"Loading environment from: {env_path}")
load_dotenv(dotenv_path=env_path)

TAVUS_PERSONA_ID = os.getenv("TAVUS_PERSONA_ID")
TAVUS_REPLICA_ID = os.getenv("TAVUS_REPLICA_ID")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# --- Tuning knobs ---
TTS_WARMUP_PHRASES = ["h", "system ready"]  # 1st=connection, 2nd=inference
SILENCE_TAIL_MS = 120   # 필요하면 80까지도 테스트
MAX_TEXT_CHUNK = 120    # 너무 긴 텍스트면 문장 단위로 쪼개기
DUP_SAY_WINDOW_SEC = 1.5  # 같은 say(같은 pid/job/room/text)가 1.5초 내 반복되면 드롭
FRAME_SLICE_SAMPLES = 2400  # 2400=100ms @ 24kHz (1200=50ms도 가능)
# -------------------


class MinimalOutput:
    def __init__(self):
        self.audio = None

class MinimalAgentSession:
    """
    livekit-plugins-tavus 가 기대하는 AgentSession shape 중 'output'만 최소 제공
    """
    def __init__(self, ctx: JobContext):
        self.ctx = ctx
        self.output = MinimalOutput()

    @property
    def room(self):
        return self.ctx.room


async def publish_debug_video(ctx: JobContext):
    width, height = 640, 480
    source = rtc.VideoSource(width, height)
    track = rtc.LocalVideoTrack.create_video_track("debug_agent_video", source)
    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_CAMERA)

    try:
        publication = await ctx.room.local_participant.publish_track(track, options)
        logger.info(f"Published DEBUG video track: {publication.sid}")
    except Exception as e:
        logger.error(f"Failed to publish debug video: {e}")
        return

    while True:
        await asyncio.sleep(10)


async def publish_beep(ctx: JobContext):
    logger.info("Generating BEEP test tone...")
    sample_rate = 44100
    duration = 0.5
    num_samples = int(sample_rate * duration)
    frequency = 440.0
    amplitude = 32767 // 2

    pcm_data = bytearray()
    for i in range(num_samples):
        sample = int(amplitude * math.sin(2 * math.pi * frequency * i / sample_rate))
        pcm_data.extend(struct.pack('<h', sample))

    source = rtc.AudioSource(sample_rate, 1)
    track = rtc.LocalAudioTrack.create_audio_track("beep_test", source)
    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)

    try:
        publication = await ctx.room.local_participant.publish_track(track, options)
        logger.info(f"Published BEEP track: {publication.sid}")

        samples_per_10ms = sample_rate // 100
        bytes_per_sample = 2
        chunk_size = samples_per_10ms * bytes_per_sample

        offset = 0
        while offset < len(pcm_data):
            chunk = pcm_data[offset:offset+chunk_size]
            if len(chunk) < chunk_size:
                break

            frame = rtc.AudioFrame(
                data=chunk,
                sample_rate=sample_rate,
                num_channels=1,
                samples_per_channel=samples_per_10ms
            )
            await source.capture_frame(frame)
            offset += chunk_size
            await asyncio.sleep(0.01)

        logger.info("Finished BEEP.")
    except Exception as e:
        logger.exception(f"Failed to publish beep: {e}")


async def robust_warmup_tts(tts, state_dict):
    """
    Multi-stage warmup:
    1. Short phrase (network connection)
    2. Longer phrase (inference context)
    Runs in background; updates state_dict.
    """
    state_dict["status"] = "warming"
    logger.info("🔥 Starting Multi-Stage TTS Warmup...")

    for i, text in enumerate(TTS_WARMUP_PHRASES):
        start = time.perf_counter()
        try:
            # Check if cancelled externally (though we run as task, explicit check helps)
            if state_dict.get("cancelled"):
                logger.info("TTS Warmup cancelled by user speech.")
                return

            logger.info(f"  - Warmup stage {i+1}/{len(TTS_WARMUP_PHRASES)}: '{text}'")
            stream = tts.synthesize(text)
            async for _ in stream:
                pass # Consume stream to force processing
            
            dur = (time.perf_counter() - start) * 1000
            logger.info(f"  - Warmup stage {i+1} complete: {dur:.2f}ms")
            
        except Exception as e:
            logger.warning(f"  - Warmup stage {i+1} failed (non-fatal): {e}")

    if not state_dict.get("cancelled"):
        state_dict["status"] = "warm"
        logger.info(f"🔥 TTS Warmup Finished (Ready). Total time: {(time.perf_counter()-state_dict['start_t'])*1000:.2f}ms")


def split_text_for_latency(text: str, max_len: int = MAX_TEXT_CHUNK):
    """
    first_audio 줄이는 목적: 너무 길면 문장/구두점 기준으로 쪼개서 첫 chunk를 빨리 받게 유도
    """
    t = (text or "").strip()
    if len(t) <= max_len:
        return [t]

    seps = [". ", "? ", "! ", "\n", ", "]
    chunks = []
    buf = t
    for sep in seps:
        if len(buf) <= max_len:
            break
        parts = buf.split(sep)
        tmp = []
        cur = ""
        for p in parts:
            candidate = (cur + (sep if cur else "") + p).strip()
            if len(candidate) <= max_len:
                cur = candidate
            else:
                if cur:
                    tmp.append(cur)
                cur = p.strip()
        if cur:
            tmp.append(cur)
        if len(tmp) >= 2:
            chunks = tmp
            break

    if not chunks:
        chunks = [t[i:i+max_len] for i in range(0, len(t), max_len)]

    return [c.strip() for c in chunks if c.strip()]


def slice_audio_frame(frame: rtc.AudioFrame, target_samples: int = FRAME_SLICE_SAMPLES):
    """
    큰 프레임(예: 4800=200ms@24kHz)을 더 작은 프레임(예: 2400=100ms)으로 슬라이스해서
    Tavus sink에 더 자주 보내 lip-sync/반응성을 개선.
    (16-bit PCM 가정: 2 bytes/sample/channel)
    """
    try:
        spc = int(frame.samples_per_channel)
        if spc <= target_samples:
            yield frame
            return

        num_ch = int(frame.num_channels)
        if num_ch <= 0:
            yield frame
            return

        bytes_per_sample = 2 * num_ch
        step_bytes = target_samples * bytes_per_sample
        data = frame.data

        offset = 0
        while offset + step_bytes <= len(data):
            chunk = data[offset:offset + step_bytes]
            yield rtc.AudioFrame(
                data=chunk,
                sample_rate=frame.sample_rate,
                num_channels=num_ch,
                samples_per_channel=target_samples,
            )
            offset += step_bytes
    except Exception:
        yield frame


def is_playback_finished_app_message(obj: dict) -> bool:
    """
    Tavus/LiveKit app_messages 포맷이 다를 수 있어서 넓게 탐지.
    """
    if not isinstance(obj, dict):
        return False

    for k in ["event", "type", "message", "name", "action", "status"]:
        v = obj.get(k)
        if isinstance(v, str):
            s = v.lower()
            if "playback" in s and ("finish" in s or "finished" in s or "done" in s):
                return True
            if s in ("playback_finished", "playbackfinished", "playback_done"):
                return True

    nested = obj.get("data") or obj.get("payload") or obj.get("detail")
    if isinstance(nested, dict):
        return is_playback_finished_app_message(nested)

    return False


async def entrypoint(ctx: JobContext):
    await ctx.connect()
    logger.info(f"Agent connected to room: {ctx.room.name}")

    if OPENAI_API_KEY:
        logger.info("OPENAI_API_KEY state: FOUND (Loaded from env)")
    else:
        logger.error(f"OPENAI_API_KEY state: MISSING! Check {env_path}")
        logger.error("TTS will NOT function. 'beep' fallback is available.")

    @ctx.room.on("participant_connected")
    def on_participant_connected(participant):
        logger.info(f"Participant connected: {participant.identity} ({participant.sid})")

    @ctx.room.on("track_published")
    def on_track_published(publication, participant):
        logger.info(f"Track published by {participant.identity}: {publication.sid} ({publication.kind})")

    # 1) TTS init & Warmup
    tts_plugin = None
    tts_state = {"status": "cold", "start_t": time.perf_counter()}
    warmup_task = None

    if OPENAI_API_KEY:
        try:
            logger.info("Initializing OpenAI TTS...")
            tts_plugin = openai.TTS(model="tts-1", voice="ash")
            # Run warmup in background (non-blocking)
            warmup_task = asyncio.create_task(robust_warmup_tts(tts_plugin, tts_state))
        except Exception as e:
            logger.error(f"Failed to init TTS: {e}")

    session_wrapper = MinimalAgentSession(ctx)
    speak_lock = asyncio.Lock()
    tavus_ready = asyncio.Event()
    stop_event = asyncio.Event()

    # ---- Duplicate SAY suppression ----
    last_say_key = {"key": None, "t": 0.0}

    def should_drop_duplicate_say(payload: dict, text: str) -> bool:
        now_t = time.perf_counter()
        key = (
            payload.get("pid"),
            payload.get("job_id"),
            payload.get("room_id"),
            (text or "").strip(),
        )
        if last_say_key["key"] == key and (now_t - last_say_key["t"]) < DUP_SAY_WINDOW_SEC:
            return True
        last_say_key["key"] = key
        last_say_key["t"] = now_t
        return False

    def make_silence_frame(sample_rate=24000, ms=SILENCE_TAIL_MS):
        samples = int(sample_rate * ms / 1000)
        data = b"\x00\x00" * samples
        return rtc.AudioFrame(
            data=data,
            sample_rate=sample_rate,
            num_channels=1,
            samples_per_channel=samples
        )

    async def speak_text(text: str, uid: str):
        """
        uid는 say packet 생성 시점에 고정.
        t0은 metrics_store.start에서 찍힘.
        """
        async with speak_lock:
            if not tts_plugin:
                logger.warning(f"Cannot speak '{text}': TTS not initialized.")
                return

            # Cancel warmup if still running (Prioritize real user speech)
            if warmup_task and not warmup_task.done():
                logger.info("⚠️ User speech racing with Warmup! Cancelling warmup to free resources.")
                tts_state["cancelled"] = True
                warmup_task.cancel()
                tts_state["status"] = "forced_warm"

            # Log current TTS state for debugging variance
            if tts_state["status"] != "warm":
                logger.info(f"Speaking while TTS state is '{tts_state['status']}'")

            # Tavus 준비되기 전 첫 발화가 느려지는 케이스 방지
            try:
                await asyncio.wait_for(tavus_ready.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

            try:
                tavus_sink = session_wrapper.output.audio

                route_name = "tavus" if tavus_sink else "fallback"
                sink = None

                if tavus_sink:
                    sink = tavus_sink
                    logger.info("ROUTE=tavus | Target=tavus-avatar-agent | Method=tavus_sink.capture_frame")
                else:
                    source = rtc.AudioSource(24000, 1)
                    track = rtc.LocalAudioTrack.create_audio_track("agent_speech", source)
                    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
                    publication = await ctx.room.local_participant.publish_track(track, options)
                    logger.info(f"Published audio track for speech: {publication.sid}")
                    sink = source

                chunks = split_text_for_latency(text)
                logger.info(f"Synthesizing speech (uid={uid}) chunks={len(chunks)} text='{text[:80]}'")

                sent_first_to_sink = False
                first_audio_marked = False

                for chunk in chunks:
                    audio_stream = tts_plugin.synthesize(chunk)

                    async for synthesized_audio in audio_stream:
                        if not first_audio_marked:
                            await metrics_store.mark_t1(uid)
                            logger.info(f"T1 | first audio from TTS | uid={uid}")
                            first_audio_marked = True

                        frame = synthesized_audio.frame

                        if not sent_first_to_sink:
                            logger.info(
                                f"Frame Format: SampleRate={frame.sample_rate}, "
                                f"Channels={frame.num_channels}, "
                                f"SamplesPerChannel={frame.samples_per_channel}"
                            )

                        # ✅ 더 작은 프레임으로 쪼개서 전송
                        for out_frame in slice_audio_frame(frame, target_samples=FRAME_SLICE_SAMPLES):
                            await sink.capture_frame(out_frame)
                            await metrics_store.inc_frames(uid, 1)

                            if not sent_first_to_sink:
                                await metrics_store.mark_t2(uid)
                                logger.info(f"T2 | first frame sent to sink | uid={uid}")
                                sent_first_to_sink = True

                silence = make_silence_frame(24000, SILENCE_TAIL_MS)
                await sink.capture_frame(silence)
                logger.info(f"Sent silence tail: {SILENCE_TAIL_MS}ms")

                # Optional flush for Tavus
                if route_name == "tavus" and hasattr(sink, "flush"):
                    try:
                        maybe = sink.flush()
                        if asyncio.iscoroutine(maybe):
                            await maybe
                    except Exception:
                        pass

                logger.info(f"Finished sending audio. (uid={uid})")

            except Exception as e:
                logger.exception(f"Error during TTS/Publishing (uid={uid}): {e}")

    # ---------------- Data packets ----------------

    @ctx.room.on("data_received")
    def on_data_received(packet):
        sender_id = packet.participant.identity if packet.participant else 'server'
        logger.info(f"Received data packet from {sender_id}: topic='{packet.topic}'")

        # 1) say
        if packet.topic == "say":
            try:
                payload = json.loads(packet.data.decode("utf-8"))
                text = payload.get("text")
                if not text:
                    return

                logger.info(f"Processing command: {text}")

                # ✅ 중복 say 드롭
                if should_drop_duplicate_say(payload, text):
                    logger.info("Dropping duplicate 'say' within window")
                    return

                if text.strip().lower() == "beep":
                    asyncio.create_task(publish_beep(ctx))
                    return

                uid = str(uuid.uuid4())[:8]
                t0 = now_ms()

                # ✅ t0를 즉시 찍어서 metrics에 반영
                async def _start_metrics():
                    await metrics_store.start(uid=uid, text=text, route="tavus", t0=t0)
                asyncio.create_task(_start_metrics())

                logger.info(f"T0 | say received | uid={uid}")

                asyncio.create_task(speak_text(text, uid))

            except Exception as e:
                logger.error(f"Failed to decode 'say' packet: {e}")

        # 2) app_messages (Tavus에서 오는 playback finished 등)
        elif packet.topic == "app_messages":
            try:
                raw = packet.data.decode("utf-8", errors="ignore")
                obj = json.loads(raw) if raw else None

                if is_playback_finished_app_message(obj):
                    async def _mark():
                        m = await metrics_store.mark_t3_from_fifo()
                        if m:
                            logger.info(f"T3 | playback finished | uid={m.uid}")
                            logger.info(metrics_store.summary_line(m))
                        else:
                            logger.warning("T3 | playback finished but no inflight uid to match")
                    asyncio.create_task(_mark())

            except Exception:
                pass

    # Debug video on
    debug_task = asyncio.create_task(publish_debug_video(ctx))

    # Tavus start
    tavus_task = None
    if TAVUS_PERSONA_ID and TAVUS_REPLICA_ID:
        try:
            logger.info("Attempting to start Tavus AvatarSession...")
            avatar = tavus.AvatarSession(persona_id=TAVUS_PERSONA_ID, replica_id=TAVUS_REPLICA_ID)
            tavus_task = asyncio.create_task(avatar.start(session_wrapper, room=ctx.room))

            def handle_tavus_result(task):
                try:
                    task.result()
                    logger.info("Tavus started successfully. Disabling debug video.")
                    tavus_ready.set()
                    debug_task.cancel()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    err_str = f"{str(e)} {getattr(e, 'body', '')}".lower()
                    if "402" in err_str or "credits" in err_str:
                        logger.warning("Tavus disabled due to 402 credits; continuing with audio-only mode.")
                        tavus_ready.set()
                    else:
                        logger.error(f"Tavus session ended with error: {e}")
                        tavus_ready.set()

            tavus_task.add_done_callback(handle_tavus_result)

        except Exception as e:
            logger.warning(f"Failed to initiate Tavus object: {e}")
            tavus_ready.set()
    else:
        tavus_ready.set()

    # ---- graceful shutdown ----
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            # 일부 환경(Windows 등)
            pass
        except Exception:
            pass

    try:
        await stop_event.wait()
    except asyncio.CancelledError:
        logger.info("Agent shutting down (cancelled)")
    finally:
        logger.info("Agent shutting down (cleanup)")
        tasks = []
        if tavus_task:
            tavus_task.cancel()
            tasks.append(tavus_task)
        if debug_task:
            debug_task.cancel()
            tasks.append(debug_task)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


if __name__ == "__main__":
    os.environ['SSL_CERT_FILE'] = certifi.where()

    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name="avatar-bot",
    ))
