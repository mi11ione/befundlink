"""Configuration from env vars, with infra/state.json as the source for room id / token."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

BOT_DIR = Path(__file__).resolve().parent
REPO = BOT_DIR.parent


def _find(obj, keys: tuple[str, ...]):
    """Depth-first search for the first matching key (the state.json layout is infra/setup.py's)."""
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and isinstance(obj[k], str) and obj[k]:
                return obj[k]
        for v in obj.values():
            r = _find(v, keys)
            if r:
                return r
    return None


def load_state(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return {}


def state_room_id(state: dict) -> str | None:
    return _find(state, ("room_id", "roomId", "room"))


def state_bot_token(state: dict) -> str | None:
    bot = None
    for k in ("bot", "bridge-bot", "bridge_bot", "@bridge-bot:klinikum.example"):
        if isinstance(state.get(k), dict):
            bot = state[k]
    if bot:
        t = _find(bot, ("access_token", "token"))
        if t:
            return t
    tokens = state.get("tokens")
    if isinstance(tokens, dict):
        for k in ("bot", "bridge-bot", "bridge_bot"):
            if isinstance(tokens.get(k), str) and tokens[k]:
                return tokens[k]
    for k in ("bot_access_token", "bot_token", "bridge_bot_token", "bridge_bot_access_token"):
        if isinstance(state.get(k), str) and state[k]:
            return state[k]
    return None


def dotenv(key: str) -> str | None:
    """Env var, else the repo's .env (written by `make .env`), so `uv run python -m bot` works too."""
    if os.environ.get(key):
        return os.environ[key]
    try:
        for line in (REPO / ".env").read_text().splitlines():
            k, _, v = line.partition("=")
            if k.strip() == key and v.strip():
                return v.strip()
    except OSError:
        pass
    return None


def state_members(state: dict) -> set[str]:
    """PGK member directory as far as the infra knows it: users in state.json except the bot."""
    users = state.get("users") if isinstance(state.get("users"), dict) else {}
    return {u["user_id"] for k, u in users.items()
            if isinstance(u, dict) and u.get("user_id") and k != "bot" and "bot" not in u["user_id"]}


@dataclass
class Config:
    homeserver: str = os.environ.get("MATRIX_HOMESERVER", "http://localhost:8008")
    user_id: str = os.environ.get("BOT_USER", "@bridge-bot:klinikum.example")
    password: str = os.environ.get("BOT_PASSWORD", "bot-demo-pw")
    access_token: str | None = os.environ.get("BOT_ACCESS_TOKEN") or None
    device_id: str = os.environ.get("BOT_DEVICE_ID", "PGKBRIDGEBOT")
    service_url: str = os.environ.get("SERVICE_URL", "http://localhost:8000")
    notify_host: str = os.environ.get("NOTIFY_HOST", "127.0.0.1")
    notify_port: int = int(os.environ.get("NOTIFY_PORT", "8001"))
    state_json: Path = Path(os.environ.get("INFRA_STATE", str(REPO / "infra" / "state.json")))
    data_dir: Path = Path(os.environ.get("BOT_DATA_DIR", str(BOT_DIR / ".data")))
    default_room: str | None = os.environ.get("BOT_ROOM_ID") or None
    sync_timeout_ms: int = 30000
    # Shared secret the service must send as X-PGK-Notify-Key on POST /notify (fail closed if unset).
    notify_key: str | None = None
    # PGK member directory: who may invite the bot and give commands. Comma-separated MXIDs.
    members: frozenset = frozenset(
        m.strip() for m in os.environ.get("BOT_MEMBERS", "@lena.hofer:klinikum.example").split(",") if m.strip())
    allowed_rooms: frozenset = frozenset()     # rooms named in state.json

    def refresh_from_state(self) -> None:
        if self.notify_key is None:
            self.notify_key = dotenv("PGK_NOTIFY_KEY")
        st = load_state(self.state_json)
        self.members = frozenset(self.members | state_members(st))
        rooms = {state_room_id(st), self.default_room, st.get("audit_room_id")}
        rooms |= {v for k, v in st.items() if k.endswith("room_id") and isinstance(v, str)}
        self.allowed_rooms = frozenset(r for r in rooms if isinstance(r, str) and r.startswith("!"))
        if not self.access_token:
            self.access_token = state_bot_token(st)
        if not self.default_room:
            self.default_room = state_room_id(st)
