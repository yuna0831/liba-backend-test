'use strict';
'use client';

import { useState, useRef, useEffect } from 'react';
import { Room, RoomEvent, ConnectionState } from 'livekit-client';

export default function Home() {
  const [roomName, setRoomName] = useState('test-room');
  const [identity, setIdentity] = useState('user-1');
  const [token, setToken] = useState('');
  const [wsUrl, setWsUrl] = useState('');
  const [status, setStatus] = useState('disconnected');
  const [participants, setParticipants] = useState(0);
  const roomRef = useRef<Room | null>(null);

  useEffect(() => {
    // Cleanup on unmount
    return () => {
      if (roomRef.current) {
        roomRef.current.disconnect();
      }
    };
  }, []);

  const getToken = async () => {
    try {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000'}/token`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ room: roomName, identity: identity }),
      });

      if (!response.ok) {
        throw new Error(`Error: ${response.status}`);
      }

      const data = await response.json();
      setToken(data.token);
      setWsUrl(data.url);
      console.log('Token received:', data.token);
      alert('Token received!');
    } catch (error) {
      console.error(error);
      alert('Failed to get token');
    }
  };

  const joinRoom = async () => {
    if (!token || !wsUrl) {
      alert('Get token first');
      return;
    }

    try {
      const room = new Room();
      roomRef.current = room;

      room
        .on(RoomEvent.ConnectionStateChanged, (state) => {
          setStatus(state);
        })
        .on(RoomEvent.Connected, () => {
          console.log('Connected to room');
          setParticipants(room.remoteParticipants.size + 1); // +1 for self
        })
        .on(RoomEvent.ParticipantConnected, () => {
          setParticipants(room.remoteParticipants.size + 1);
        })
        .on(RoomEvent.ParticipantDisconnected, () => {
          setParticipants(room.remoteParticipants.size + 1);
        })
        .on(RoomEvent.Disconnected, () => {
          console.log('Disconnected');
          setParticipants(0);
        });

      await room.connect(wsUrl, token);
      console.log('Connected!');
    } catch (error) {
      console.error(error);
      alert('Failed to connect');
    }
  };

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
            className="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded"
            onClick={getToken}
          >
            Get Token
          </button>

          <button
            className="bg-green-500 hover:bg-green-600 text-white px-4 py-2 rounded"
            onClick={joinRoom}
          >
            Join Room
          </button>
        </div>
      </div>

      <div className="mt-8 border p-4 rounded w-full max-w-md">
        <p><strong>Status:</strong> {status}</p>
        <p><strong>Participants:</strong> {participants}</p>
        <p className="break-all mt-2 text-xs text-gray-500"><strong>Token:</strong> {token ? token.slice(0, 20) + '...' : 'None'}</p>
      </div>
    </div>
  );
}
