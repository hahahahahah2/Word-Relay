from fastapi.responses import HTMLResponse
from fastapi.responses import FileResponse
from pathlib import Path
from fastapi.staticfiles import StaticFiles
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
import json, time, secrets
from dataclasses import dataclass, field
from typing import Dict, List, Set, Optional

app = FastAPI()

BASE_DIR= Path(__file__).resolve().parent
    
TURN_SECONDS = 15

# =========================
#  한글 두음법칙 유틸
# =========================
L = ["ㄱ","ㄲ","ㄴ","ㄷ","ㄸ","ㄹ","ㅁ","ㅂ","ㅃ","ㅅ","ㅆ","ㅇ","ㅈ","ㅉ","ㅊ","ㅋ","ㅌ","ㅍ","ㅎ"]
V = ["ㅏ","ㅐ","ㅑ","ㅒ","ㅓ","ㅔ","ㅕ","ㅖ","ㅗ","ㅘ","ㅙ","ㅚ","ㅛ","ㅜ","ㅝ","ㅞ","ㅟ","ㅠ","ㅡ","ㅢ","ㅣ"]
T = ["", "ㄱ","ㄲ","ㄳ","ㄴ","ㄵ","ㄶ","ㄷ","ㄹ","ㄺ","ㄻ","ㄼ","ㄽ","ㄾ","ㄿ","ㅀ","ㅁ","ㅂ","ㅄ","ㅅ","ㅆ","ㅇ","ㅈ","ㅊ","ㅋ","ㅌ","ㅍ","ㅎ"]

IOTIZED = {"ㅣ","ㅑ","ㅕ","ㅛ","ㅠ","ㅒ","ㅖ"}

def is_hangul_syllable(ch: str) -> bool:
    return len(ch) == 1 and 0xAC00 <= ord(ch) <= 0xD7A3

def decompose(ch: str):
    code = ord(ch) - 0xAC00
    l = code // 588
    v = (code % 588) // 28
    t = code % 28
    return L[l], V[v], T[t]

def compose(lc: str, vc: str, tc: str) -> str:
    return chr(0xAC00 + (L.index(lc) * 588) + (V.index(vc) * 28) + T.index(tc))

def dueum_forward(first_syl: str) -> str:
    if not is_hangul_syllable(first_syl):
        return first_syl
    lc, vc, tc = decompose(first_syl)

    # ㄴ + (ㅣ/ㅑ/ㅕ/ㅛ/ㅠ/ㅒ/ㅖ) -> ㅇ
    if lc == "ㄴ" and vc in IOTIZED:
        return compose("ㅇ", vc, tc)

    # ㄹ + (ㅣ/ㅑ/ㅕ/ㅛ/ㅠ/ㅒ/ㅖ) -> ㅇ
    # ㄹ + (그 외) -> ㄴ
    if lc == "ㄹ":
        if vc in IOTIZED:
            return compose("ㅇ", vc, tc)
        return compose("ㄴ", vc, tc)

    return first_syl

def dueum_equivalents_for_start(first_syl: str) -> set[str]:
    """
    '다음 단어 첫 음절' 기준으로, 이전 끝 음절과 매칭에 허용할 동치집합.
    (forward + 역방향 후보까지)
    """
    eq = {first_syl}
    if not is_hangul_syllable(first_syl):
        return eq

    lc, vc, tc = decompose(first_syl)

    # forward도 동치
    eq.add(dueum_forward(first_syl))

    # reverse 후보: ㅇ + IOTIZED -> (ㄴ or ㄹ) 가능
    if lc == "ㅇ" and vc in IOTIZED:
        eq.add(compose("ㄴ", vc, tc))
        eq.add(compose("ㄹ", vc, tc))

    # reverse 후보: ㄴ + (비 IOTIZED) -> ㄹ 가능 (낙 <-> 락)
    if lc == "ㄴ" and vc not in IOTIZED:
        eq.add(compose("ㄹ", vc, tc))

    return eq

def chain_ok(prev_last: str, next_first: str) -> bool:
    return prev_last == next_first or prev_last in dueum_equivalents_for_start(next_first)


# =========================
#  게임 상태
# =========================
@dataclass
class Player:
    pid: str
    name: str
    alive: bool = True

@dataclass
class Room:
    rid: str
    players: List[Player] = field(default_factory=list)
    sockets: Dict[str, WebSocket] = field(default_factory=dict)  # pid -> ws
    started: bool = False
    turn_idx: int = 0
    current_word: Optional[str] = None
    used_words: Set[str] = field(default_factory=set)
    deadline: float = 0.0

rooms: Dict[str, Room] = {}

def now() -> float:
    return time.time()

def normalize(word: str) -> str:
    return word.strip()

def first_char(word: str) -> str:
    return word[0]

def last_char(word: str) -> str:
    return word[-1]

def get_room(rid: str) -> Room:
    if rid not in rooms:
        rooms[rid] = Room(rid=rid)
    return rooms[rid]

def alive_count(room: Room) -> int:
    return sum(1 for p in room.players if p.alive)

def next_alive_idx(room: Room, cur_idx: int) -> int:
    n = len(room.players)
    for k in range(1, n + 1):
        i = (cur_idx + k) % n
        if room.players[i].alive:
            return i
    return cur_idx

def find_player(room: Room, pid: str) -> Player:
    for p in room.players:
        if p.pid == pid:
            return p
    raise KeyError("player not found")

def state_payload(room: Room) -> dict:
    turn_pid = room.players[room.turn_idx].pid if room.started and room.players else None
    turn_name = room.players[room.turn_idx].name if room.started and room.players else None

    next_pid = None
    next_name = None
    if room.started and room.players and alive_count(room) > 1:
        ni = next_alive_idx(room, room.turn_idx)
        next_pid = room.players[ni].pid
        next_name = room.players[ni].name

    return {
        "type": "state",
        "rid": room.rid,
        "started": room.started,
        "players": [{"pid": p.pid, "name": p.name, "alive": p.alive} for p in room.players],
        "turn_pid": turn_pid,
        "turn_name": turn_name,
        "next_pid": next_pid,
        "next_name": next_name,
        "current_word": room.current_word,
        "used_count": len(room.used_words),
        "deadline": room.deadline,
        "turn_seconds": TURN_SECONDS,
    }

async def broadcast(room: Room, payload: dict):
    dead = []
    text = json.dumps(payload, ensure_ascii=False)
    for pid, ws in list(room.sockets.items()):
        try:
            await ws.send_text(text)
        except Exception:
            dead.append(pid)
    for pid in dead:
        room.sockets.pop(pid, None)

async def system(room: Room, msg: str):
    await broadcast(room, {"type": "system", "msg": msg})

async def error(ws: WebSocket, msg: str):
    await ws.send_text(json.dumps({"type": "error", "msg": msg}, ensure_ascii=False))

async def start_game(room: Room):
    if room.started:
        return
    if alive_count(room) < 2:
        # 1명이어도 시작은 되지만 재미 없으니 막아도 되고, 여기선 안내만
        await system(room, "인원이 2명 이상이면 더 재밌어요 🙂")
    room.started = True
    room.turn_idx = 0
    room.current_word = None
    room.used_words.clear()
    room.deadline = now() + TURN_SECONDS
    await system(room, f"게임 시작! 제한시간 {TURN_SECONDS}초 (누구나 Start 가능)")
    await broadcast(room, state_payload(room))

async def end_game(room: Room, msg: str):
    await system(room, msg)
    room.started = False
    room.current_word = None
    room.used_words.clear()
    room.deadline = 0.0
    await broadcast(room, state_payload(room))

async def eliminate_current_player(room: Room, reason: str):
    cur = room.players[room.turn_idx]
    cur.alive = False
    await system(room, f"{cur.name} 탈락 ({reason})")

    if alive_count(room) <= 1:
        winner = next((p for p in room.players if p.alive), None)
        await end_game(room, f"게임 종료! 승자: {winner.name if winner else '없음'}")
        return

    room.turn_idx = next_alive_idx(room, room.turn_idx)
    room.deadline = now() + TURN_SECONDS
    await broadcast(room, state_payload(room))

async def handle_submit(room: Room, pid: str, word: str, ws: WebSocket):
    if not room.started:
        return await error(ws, "게임이 시작되지 않았습니다.")
    if room.players[room.turn_idx].pid != pid:
        return await error(ws, "당신의 턴이 아닙니다.")
    if now() > room.deadline:
        return await eliminate_current_player(room, "시간초과")

        return await error(ws, "당신의 턴이 아닙니다.")
    if now() > room.deadline:
        return await eliminate_current_player(room, "시간초과")

    w = normalize(word)
    if len(w) < 2:
        return await error(ws, "단어가 너무 짧습니다.")
    if w in room.used_words:
        return await eliminate_current_player(room, "중복 단어")

    # 첫 단어는 아무 글자나 OK
    if room.current_word is not None:
        prev_last = last_char(room.current_word)
        nxt_first = first_char(w)
        if not chain_ok(prev_last, nxt_first):
            return await eliminate_current_player(room, "끝말잇기(두음) 규칙 위반")

    room.current_word = w
    room.used_words.add(w)
    await system(room, f"{find_player(room, pid).name}: {w}")

    room.turn_idx = next_alive_idx(room, room.turn_idx)
    room.deadline = now() + TURN_SECONDS
    await broadcast(room, state_payload(room))


# =========================
#  HTTP / WebSocket
# =========================
@app.get("/create_room")
def create_room():
    rid = secrets.token_urlsafe(4)  # 짧은 방 코드
    rooms[rid] = Room(rid=rid)
    return {"rid": rid}

@app.websocket("/ws/{rid}")
async def ws_room(ws: WebSocket, rid: str):
    await ws.accept()
    room = get_room(rid)

    pid = secrets.token_urlsafe(8)
  

    try:
        # 첫 메시지 join
        raw = await ws.receive_text()
        data = json.loads(raw)
        if data.get("type") != "join":
            await error(ws, "첫 메시지는 join이어야 합니다.")
            await ws.close()
            return

        name = (data.get("name") or "").strip()[:20]
        if not name:
            await error(ws, "이름이 필요합니다.")
            await ws.close()
            return

        room.players.append(Player(pid=pid, name=name))
        room.sockets[pid] = ws

        await system(room, f"{name} 입장")
        await ws.send_text(json.dumps({"type": "joined", "pid": pid, "rid": rid}, ensure_ascii=False))
        await broadcast(room, state_payload(room))

        while True:
            raw = await ws.receive_text()
            data = json.loads(raw)
            t = data.get("type")

            if t == "start":
                # ✅ 누구나 start 가능
                await start_game(room)

            elif t == "submit":
                await handle_submit(room, pid, data.get("word", ""), ws)

            elif t == "ping":
                await ws.send_text(json.dumps({"type": "pong"}, ensure_ascii=False))

            else:
                await error(ws, "알 수 없는 메시지 타입입니다.")

    except WebSocketDisconnect:
        room.sockets.pop(pid, None)
        try:
            p = find_player(room, pid)
            await system(room, f"{p.name} 연결 끊김")
        except Exception:
            pass

@app.get("/")
def serve_index():
        return FileResponse(BASE_DIR / "index.html") 
