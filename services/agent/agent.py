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
from pathlib import Path

from dotenv import load_dotenv
from livekit.agents import JobContext, WorkerOptions, cli, AgentSession
from livekit.plugins import tavus
from livekit.plugins import openai
from livekit import rtc

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
        
    @property
    def room(self):
        return self.ctx.room

async def publish_debug_video(ctx: JobContext):
    """
    Publishes a dummy video track (solid color) for debugging.
    """
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
    
    # Just keep it alive
    while True:
        await asyncio.sleep(10)

async def publish_beep(ctx: JobContext):
    """
    Generates and publishes a 440Hz sine wave beep (0.5s) to test audio path.
    """
    logger.info("Generating BEEP test tone...")
    sample_rate = 44100
    duration = 0.5 # seconds
    num_samples = int(sample_rate * duration)
    frequency = 440.0
    amplitude = 32767 // 2 

    # Generate PCM data (16-bit mono)
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
            
            frame = rtc.AudioFrame(data=chunk, sample_rate=sample_rate, num_channels=1, samples_per_channel=samples_per_10ms)
            await source.capture_frame(frame)
            offset += chunk_size
            await asyncio.sleep(0.01) 

        logger.info("Finished BEEP.")
        
    except Exception as e:
        logger.exception(f"Failed to publish beep: {e}")


async def entrypoint(ctx: JobContext):
    """
    Entrypoint for the LiveKit Agent.
    """
    await ctx.connect()
    logger.info(f"Agent connected to room: {ctx.room.name}")
    
    # Environment Check
    if OPENAI_API_KEY:
        logger.info("OPENAI_API_KEY state: FOUND (Loaded from env)")
    else:
        logger.error(f"OPENAI_API_KEY state: MISSING! Check {env_path}")
        logger.error("TTS will NOT function. 'beep' fallback is available.")
    
    # LOGGING: Remote participants
    @ctx.room.on("participant_connected")
    def on_participant_connected(participant):
        logger.info(f"Participant connected: {participant.identity} ({participant.sid})")
        
    @ctx.room.on("track_published")
    def on_track_published(publication, participant):
        logger.info(f"Track published by {participant.identity}: {publication.sid} ({publication.kind})")

    # 1. Initialize OpenAI TTS
    tts_plugin = None
    if OPENAI_API_KEY:
        try:
            logger.info("Initializing OpenAI TTS...")
            tts_plugin = openai.TTS(model="tts-1", voice="ash")
        except Exception as e:
            logger.error(f"Failed to init TTS: {e}")
            
    # Function scope variables for state
    session_wrapper = MinimalAgentSession(ctx)
    tavus_started = False
    speak_lock = asyncio.Lock()

    def make_silence_frame(sample_rate=24000, ms=300):
        samples = int(sample_rate * ms / 1000)
        data = b"\x00\x00" * samples  # int16 mono silence
        return rtc.AudioFrame(
            data=data,
            sample_rate=sample_rate,
            num_channels=1,
            samples_per_channel=samples
        )
    
    async def speak_text(text: str):
        """
        Synthesizes speech and publishes audio.
        Routes to Tavus Avatar if active, otherwise publishes to Room directly.
        Serialized to prevent overlap.
        """
        async with speak_lock:
            if not tts_plugin:
                logger.warning(f"Cannot speak '{text}': TTS not initialized (Check OPENAI_API_KEY).")
                return

            try:
                tavus_sink = session_wrapper.output.audio

                logger.info(f"Synthesizing speech: '{text}'")
                audio_stream = tts_plugin.synthesize(text)

                if tavus_sink:
                    logger.info("ROUTE=tavus | Target=tavus-avatar-agent | Method=tavus_sink.capture_frame")
                    time_start = time.time()
                    frames_count = 0

                    async for synthesized_audio in audio_stream:
                        frame = synthesized_audio.frame
                        if frames_count == 0:
                            logger.info(
                                f"Frame Format: SampleRate={frame.sample_rate}, "
                                f"Channels={frame.num_channels}, "
                                f"SamplesPerChannel={frame.samples_per_channel}"
                            )
                        await tavus_sink.capture_frame(frame)
                        frames_count += 1

                    # Tail padding (prevents truncation of last syllable)
                    silence_ms = 300
                    silence = make_silence_frame(24000, silence_ms)
                    await tavus_sink.capture_frame(silence)
                    logger.info(f"Sent silence tail: {silence_ms}ms")

                    # Optional flush if sink supports it
                    if hasattr(tavus_sink, "flush"):
                        try:
                            maybe = tavus_sink.flush()
                            if asyncio.iscoroutine(maybe):
                                await maybe
                        except Exception:
                            logger.warning("tavus_sink.flush() failed/ignored")

                    logger.info(
                        f"Finished sending audio to Tavus. (Sent {frames_count} frames, "
                        f"took {time.time()-time_start:.2f}s)"
                    )

                else:
                    logger.info("ROUTE=fallback | Target=agent_speech (LocalTrack) | Reason=Tavus Not Ready")

                    source = rtc.AudioSource(24000, 1)
                    track = rtc.LocalAudioTrack.create_audio_track("agent_speech", source)
                    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)

                    publication = await ctx.room.local_participant.publish_track(track, options)
                    logger.info(f"Published audio track for speech: {publication.sid} ({publication.kind})")

                    time_start = time.time()
                    frames_count = 0
                    total_samples = 0

                    async for synthesized_audio in audio_stream:
                        frame = synthesized_audio.frame
                        if frames_count == 0:
                            logger.info(
                                f"Frame Format: SampleRate={frame.sample_rate}, "
                                f"Channels={frame.num_channels}, "
                                f"SamplesPerChannel={frame.samples_per_channel}"
                            )
                        await source.capture_frame(frame)
                        frames_count += 1
                        total_samples += frame.samples_per_channel

                    duration = time.time() - time_start
                    logger.info(
                        f"Finished speaking. (Sent {frames_count} frames, {total_samples} samples, "
                        f"took {duration:.2f}s)"
                    )

            except Exception as e:
                logger.exception(f"Error during TTS/Publishing: {e}")

    # Handling Data Packets for TTS
    @ctx.room.on("data_received")
    def on_data_received(packet):
        sender_id = packet.participant.identity if packet.participant else 'server'
        logger.info(f"Received data packet from {sender_id}: topic='{packet.topic}'")
        
        if packet.topic == "say":
             try:
                 payload = json.loads(packet.data.decode("utf-8"))
                 text = payload.get("text")
                 if text:
                     logger.info(f"Processing command: {text}")
                     if text.strip().lower() == "beep":
                         asyncio.create_task(publish_beep(ctx))
                     else:
                         asyncio.create_task(speak_text(text))
             except Exception as e:
                 logger.error(f"Failed to decode 'say' packet: {e}")

    # Start Debug Video (initially)
    debug_task = asyncio.create_task(publish_debug_video(ctx))

    # Start Tavus if credentials exist
    tavus_task = None
    if TAVUS_PERSONA_ID and TAVUS_REPLICA_ID:
        try:
            logger.info("Attempting to start Tavus AvatarSession...")
            avatar = tavus.AvatarSession(persona_id=TAVUS_PERSONA_ID, replica_id=TAVUS_REPLICA_ID)
            
            # Start Tavus in background
            tavus_task = asyncio.create_task(avatar.start(session_wrapper, room=ctx.room))
            
            # Use callback to handle immediate failures (like 402 credits) non-fatally
            def handle_tavus_result(task):
                try:
                    task.result()
                    # If successful, we can disable debug video
                    logger.info("Tavus started successfully. Disabling debug video.")
                    nonlocal tavus_started
                    tavus_started = True
                    debug_task.cancel()
                except asyncio.CancelledError:
                    pass
                except Exception as e:
                    # Check exception and its cause for 402/credits or generic retry failure
                    err_str = f"{str(e)} {getattr(e, 'body', '')}"
                    lower_err = err_str.lower()
                    
                    if "402" in lower_err or "credits" in lower_err or "after all retries" in lower_err:
                         logger.warning("Tavus disabled due to 402 credits (or verify failed); continuing with audio-only mode.")
                    elif hasattr(e, "status_code") and e.status_code == 402:
                         logger.warning("Tavus disabled due to 402 credits; continuing with audio-only mode.")
                    else:
                        logger.error(f"Tavus session ended with error: {e}")
            
            tavus_task.add_done_callback(handle_tavus_result)
        except Exception as e:
             logger.warning(f"Failed to initiate Tavus object: {e}")

    try:
        await asyncio.Future()
    except asyncio.CancelledError:
        logger.info("Agent shutting down")
        if tavus_task: tavus_task.cancel()
        debug_task.cancel()

if __name__ == "__main__":
    # Fix SSL usage globally for requests/other libs just in case
    os.environ['SSL_CERT_FILE'] = certifi.where()

    cli.run_app(WorkerOptions(
        entrypoint_fnc=entrypoint,
        agent_name="avatar-bot",
    ))
