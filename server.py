import asyncio
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Set


WebSocketServerProtocol = Any


logging.basicConfig(level=logging.INFO, format='[server] %(message)s')

HOST = '0.0.0.0'
PORT = int(os.getenv('PORT', '8765'))
MAX_ROOM_SIZE = 2
SNAPSHOT_RATE = float(os.getenv('SNAPSHOT_RATE', '20'))
MAX_MOVE_PER_INPUT = float(os.getenv('MAX_MOVE_PER_INPUT', '8'))
PUBLIC_QUEUE = 'public'


@dataclass
class PlayerState:
    x: float = 100.0
    y: float = 100.0
    hp: int = 100
    mana: int = 100


@dataclass
class PlayerConn:
    ws: WebSocketServerProtocol
    player_id: str
    room_id: str
    joined_at: float = field(default_factory=time.time)


@dataclass
class Room:
    room_id: str
    players: Dict[str, PlayerConn] = field(default_factory=dict)
    state: Dict = field(default_factory=lambda: {'tick': 0, 'players': {}})


rooms: Dict[str, Room] = {}
queue: Set[WebSocketServerProtocol] = set()
player_index: Dict[WebSocketServerProtocol, PlayerConn] = {}


def make_room_id() -> str:
    return f'room-{secrets.token_hex(3)}'


def now_ms() -> int:
    return int(time.time() * 1000)


async def safe_send(ws: WebSocketServerProtocol, payload: Dict):
    try:
        await ws.send(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


async def broadcast(room: Room, payload: Dict):
    if not room.players:
        return
    message = json.dumps(payload, ensure_ascii=False)
    coros = []
    for pc in room.players.values():
        coros.append(pc.ws.send(message))
    await asyncio.gather(*coros, return_exceptions=True)


def create_room_with_pair(a: WebSocketServerProtocol, b: WebSocketServerProtocol) -> Room:
    room_id = make_room_id()
    room = Room(room_id=room_id)
    rooms[room_id] = room

    p1 = PlayerConn(ws=a, player_id='p1', room_id=room_id)
    p2 = PlayerConn(ws=b, player_id='p2', room_id=room_id)

    room.players[p1.player_id] = p1
    room.players[p2.player_id] = p2

    room.state['players'][p1.player_id] = PlayerState(x=180, y=300).__dict__.copy()
    room.state['players'][p2.player_id] = PlayerState(x=620, y=300).__dict__.copy()

    player_index[a] = p1
    player_index[b] = p2
    return room


async def try_matchmaking():
    if len(queue) < 2:
        return
    a = queue.pop()
    b = queue.pop()
    room = create_room_with_pair(a, b)
    logging.info(f'match created: {room.room_id}')

    await safe_send(a, {
        'type': 'welcome',
        'playerId': 'p1',
        'roomId': room.room_id,
        'serverTime': now_ms(),
    })
    await safe_send(b, {
        'type': 'welcome',
        'playerId': 'p2',
        'roomId': room.room_id,
        'serverTime': now_ms(),
    })

    await broadcast(room, {
        'type': 'match_found',
        'roomId': room.room_id,
        'players': ['p1', 'p2'],
    })


async def remove_client(ws: WebSocketServerProtocol):
    if ws in queue:
        queue.discard(ws)

    conn = player_index.pop(ws, None)
    if not conn:
        return

    room = rooms.get(conn.room_id)
    if not room:
        return

    room.players.pop(conn.player_id, None)
    room.state.get('players', {}).pop(conn.player_id, None)

    await broadcast(room, {
        'type': 'player_left',
        'playerId': conn.player_id,
        'roomId': room.room_id,
    })

    if not room.players:
        rooms.pop(room.room_id, None)


def apply_input(room: Room, player_id: str, msg: Dict):
    p = room.state.get('players', {}).get(player_id)
    if not p:
        return

    try:
        dx = float(msg.get('dx', 0.0))
        dy = float(msg.get('dy', 0.0))
    except Exception:
        dx, dy = 0.0, 0.0

    dx = max(-MAX_MOVE_PER_INPUT, min(MAX_MOVE_PER_INPUT, dx))
    dy = max(-MAX_MOVE_PER_INPUT, min(MAX_MOVE_PER_INPUT, dy))

    p['x'] = max(0.0, min(5000.0, float(p.get('x', 0.0)) + dx))
    p['y'] = max(0.0, min(5000.0, float(p.get('y', 0.0)) + dy))


async def ws_handler(ws: WebSocketServerProtocol):
    await safe_send(ws, {
        'type': 'queue_joined',
        'queue': PUBLIC_QUEUE,
        'serverTime': now_ms(),
    })

    queue.add(ws)
    await try_matchmaking()

    try:
        async for raw in ws:
            try:
                msg = json.loads(raw)
            except Exception:
                continue

            t = msg.get('type')
            if t == 'ping':
                await safe_send(ws, {'type': 'pong', 'ts': msg.get('ts'), 'serverTime': now_ms()})
                continue

            conn = player_index.get(ws)
            if not conn:
                continue

            room = rooms.get(conn.room_id)
            if not room:
                continue

            if t == 'input':
                apply_input(room, conn.player_id, msg)
    finally:
        await remove_client(ws)


async def snapshot_loop():
    tick_dt = 1.0 / max(1.0, SNAPSHOT_RATE)
    while True:
        await asyncio.sleep(tick_dt)
        for room in list(rooms.values()):
            room.state['tick'] = int(room.state.get('tick', 0)) + 1
            await broadcast(room, {
                'type': 'snapshot',
                'roomId': room.room_id,
                'state': room.state,
                'serverTime': now_ms(),
            })


async def main():
    try:
        import websockets
    except Exception as e:
        raise RuntimeError('Missing dependency: websockets. Install with pip install -r requirements.txt') from e
    asyncio.create_task(snapshot_loop())
    async with websockets.serve(ws_handler, HOST, PORT, max_size=2_000_000):
        logging.info(f'listening on {HOST}:{PORT}')
        await asyncio.Future()


if __name__ == '__main__':
    asyncio.run(main())
