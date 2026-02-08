'use client';

import { useEffect, useMemo, useRef, useState, useCallback } from 'react';
import { Room, RoomEvent } from 'livekit-client';

/**
 * Small helpers
 */
function sleep(ms: number) {
  return new Promise((r) => setTimeout(r, ms));
}

function normalizeText(s: string) {
  return (s || '').replace(/\s+/g, ' ').trim();
}

async function fetchWithTimeout(
  input: RequestInfo | URL,
  init: RequestInit,
  timeoutMs: number
) {
  const controller = new AbortController();
  const id = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(input, { ...init, signal: controller.signal });
    return res;
  } finally {
    clearTimeout(id);
  }
}

/**
 * Simple client-side dedupe cache: (room + text) within window => drop
 */
class DedupeCache {
  private map = new Map<string, number>();
  constructor(private windowMs: number) { }

  key(room: string, text: string) {
    return `${room}||${text}`;
  }

  shouldDrop(room: string, text: string) {
    const now = performance.now();
    const k = this.key(room, text);
    // purge old (small map so simple)
    for (const [kk, t] of this.map.entries()) {
      if (now - t > this.windowMs) this.map.delete(kk);
    }
    const last = this.map.get(k);
    if (last != null && now - last <= this.windowMs) return true;
    this.map.set(k, now);
    return false;
  }
}

export default function Home() {
  const API_BASE = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

  const [roomName, setRoomName] = useState('demo');
  const [identity, setIdentity] = useState('user-1');
  const [token, setToken] = useState('');
  const [wsUrl, setWsUrl] = useState('');
  const [status, setStatus] = useState('disconnected');
  const [participants, setParticipants] = useState(0);

  const [speechText, setSpeechText] = useState('Hello, Tavus!');
  const [isSpeaking, setIsSpeaking] = useState(false);
  const [lastSentText, setLastSentText] = useState('');

  const roomRef = useRef<Room | null>(null);

  /**
   * ---- "방탄" refs ----
   */
  // hard mutex: true면 어떤 경로로도 send 못함
  const speakLockRef = useRef(false);

  // IME composition state (한국어 입력 중 Enter는 send로 치면 안됨)
  const isComposingRef = useRef(false);

  // last click timestamp (double click 방지)
  const lastClickMsRef = useRef(0);

  // inflight request aborter (선택: 새 요청 시 이전 cancel)
  const inflightAbortRef = useRef<AbortController | null>(null);

  // local dedupe window (backend dedupe와 별개로 UX 방탄)
  const dedupe = useMemo(() => new DedupeCache(1500), []);

  useEffect(() => {
    return () => {
      if (roomRef.current) roomRef.current.disconnect();
      inflightAbortRef.current?.abort();
    };
  }, []);

  const getToken = useCallback(async () => {
    try {
      const res = await fetchWithTimeout(
        `${API_BASE}/token`,
        {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ room: roomName, identity }),
        },
        8000
      );

      if (!res.ok) throw new Error(`Error: ${res.status}`);
      const data = await res.json();

      setToken(data.token);
      setWsUrl(data.url);
    } catch (e) {
      console.error(e);
      alert('Failed to get token');
    }
  }, [API_BASE, roomName, identity]);

  const joinRoom = useCallback(async () => {
    if (!token || !wsUrl) {
      alert('Get token first');
      return;
    }
    try {
      const room = new Room();
      roomRef.current = room;

      room
        .on(RoomEvent.ConnectionStateChanged, (st) => setStatus(st))
        .on(RoomEvent.Connected, () => setParticipants(room.remoteParticipants.size + 1))
        .on(RoomEvent.ParticipantConnected, () => setParticipants(room.remoteParticipants.size + 1))
        .on(RoomEvent.ParticipantDisconnected, () => setParticipants(room.remoteParticipants.size + 1))
        .on(RoomEvent.Disconnected, () => setParticipants(0));

      await room.connect(wsUrl, token);
    } catch (e) {
      console.error(e);
      alert('Failed to connect');
    }
  }, [token, wsUrl]);

  /**
   * Main send function (클릭/엔터 모두 여기로)
   */
  const sendSpeak = useCallback(async () => {
    // HARD LOCK (연타/엔터/더블클릭/키다운 어떤 경로든 다 여기서 막힘)
    if (speakLockRef.current) return;

    const text = normalizeText(speechText);
    if (!text) return;

    // local dedupe
    if (dedupe.shouldDrop(roomName, text)) {
      // 조용히 무시 (원하면 toast)
      return;
    }

    // engage lock immediately (state보다 ref가 즉시성 좋음)
    speakLockRef.current = true;
    setIsSpeaking(true);
    setLastSentText(text);

    // optionally abort previous inflight (안전)
    inflightAbortRef.current?.abort();
    const controller = new AbortController();
    inflightAbortRef.current = controller;

    const startedAt = performance.now();

    try {
      const res = await fetch(`${API_BASE}/say`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ room: roomName, text }),
        signal: controller.signal,
      });

      if (!res.ok) {
        let detail = 'Failed to send speech';
        try {
          const err = await res.json();
          detail = err?.detail || detail;
        } catch { }
        throw new Error(detail);
      }

      // 너무 빠르게 풀리면 “연타 방지”가 체감 안돼서
      // 최소 250ms는 잠궈두기(선택)
      const elapsed = performance.now() - startedAt;
      if (elapsed < 250) await sleep(250 - elapsed);
    } catch (e: any) {
      // Abort는 조용히 처리
      if (e?.name !== 'AbortError') {
        console.error(e);
        alert(e?.message || 'Failed to send speech');
      }
    } finally {
      // release lock
      speakLockRef.current = false;
      setIsSpeaking(false);
      // controller는 그대로 두면 나중 abort 호출될 수 있으니 정리
      if (inflightAbortRef.current === controller) inflightAbortRef.current = null;
    }
  }, [API_BASE, roomName, speechText, dedupe]);

  /**
   * Button click handler with double-click guard
   */
  const handleSpeakClick = useCallback(() => {
    const now = performance.now();
    // double click / rapid click guard (100~250ms 추천)
    if (now - lastClickMsRef.current < 250) return;
    lastClickMsRef.current = now;
    void sendSpeak();
  }, [sendSpeak]);

  /**
   * Keydown handler (Enter send, Shift+Enter newline)
   * - IME 조합 중 Enter는 무시
   * - repeat keydown(키 누르고 있을 때) 무시
   */
  const handleTextareaKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key !== 'Enter') return;

      // IME composing이면 엔터로 제출 금지
      if (isComposingRef.current) return;

      // 길게 눌러 반복되는 keydown 방지
      if ((e as any).repeat) {
        e.preventDefault();
        return;
      }

      // Shift+Enter는 줄바꿈 허용
      if (e.shiftKey) return;

      // Enter => send
      e.preventDefault();
      void sendSpeak();
    },
    [sendSpeak]
  );

  const handleCompositionStart = useCallback(() => {
    isComposingRef.current = true;
  }, []);

  const handleCompositionEnd = useCallback(() => {
    // compositionend 직후에도 엔터 이벤트가 따라오는 경우가 있어서
    // 아주 짧게 풀어주는게 안전
    setTimeout(() => {
      isComposingRef.current = false;
    }, 0);
  }, []);

  return (
    <div className="flex flex-col items-center justify-center min-h-screen p-8 gap-4 font-[family-name:var(--font-geist-sans)]">
      <h1 className="text-2xl font-bold">Liba Backend Test</h1>

      <div className="flex flex-col gap-2 w-full max-w-md">
        <label>Room Name</label>
        <input
          className="border p-2 rounded text-black"
          value={roomName}
          onChange={(e) => setRoomName(e.target.value)}
        />

        <label>Identity</label>
        <input
          className="border p-2 rounded text-black"
          value={identity}
          onChange={(e) => setIdentity(e.target.value)}
        />

        <div className="flex gap-4 mt-4">
          <button
            type="button"
            className="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded"
            onClick={getToken}
          >
            Get Token
          </button>

          <button
            type="button"
            className="bg-green-500 hover:bg-green-600 text-white px-4 py-2 rounded"
            onClick={joinRoom}
          >
            Join Room
          </button>
        </div>
      </div>

      <div className="mt-8 border p-4 rounded w-full max-w-md">
        <p>
          <strong>Status:</strong> {status}
        </p>
        <p>
          <strong>Participants:</strong> {participants}
        </p>
        <p className="break-all mt-2 text-xs text-gray-500">
          <strong>Token:</strong> {token ? token.slice(0, 20) + '...' : 'None'}
        </p>
      </div>

      <div className="mt-8 border p-4 rounded w-full max-w-md flex flex-col gap-2">
        <h2 className="font-bold">Step 4: Speak</h2>

        <textarea
          className="border p-2 rounded text-black"
          value={speechText}
          onChange={(e) => setSpeechText(e.target.value)}
          disabled={isSpeaking}
          onKeyDown={handleTextareaKeyDown}
          onCompositionStart={handleCompositionStart}
          onCompositionEnd={handleCompositionEnd}
          rows={4}
          placeholder="Type… (Enter to send, Shift+Enter for newline)"
        />

        <button
          type="button"
          className={`${isSpeaking ? 'bg-gray-400' : 'bg-purple-500 hover:bg-purple-600'
            } text-white px-4 py-2 rounded`}
          onClick={handleSpeakClick}
          disabled={isSpeaking}
        >
          {isSpeaking ? 'Sending…' : 'Speak via Tavus'}
        </button>

        {lastSentText && (
          <p className="text-xs text-gray-500 mt-2">Last sent: "{lastSentText}"</p>
        )}
      </div>
    </div>
  );
}
