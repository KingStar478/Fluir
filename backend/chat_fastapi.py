# Importacao das bibliotecas usadas pelo chat em tempo real
import asyncio
import hashlib
import hmac
import html
import json
import os
import secrets
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

try:
    import redis.asyncio as redis
except ImportError:  # pragma: no cover - used only when dependencies are missing.
    redis = None


# Configuracao dos caminhos e da conexao com Redis
BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
CHAT_DB = INSTANCE_DIR / "chat.db"
USER_DB = INSTANCE_DIR / "usuarios.db"
CHAT_TEMPLATE = BASE_DIR / "templates" / "chat_live.html"
CHAT_ROOM_AVATAR_DIR = BASE_DIR / "static" / "uploads" / "chat-room-avatars"
MAX_CHAT_ROOM_AVATAR_SIZE = 5 * 1024 * 1024
CHAT_BUILD_ID = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16]
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")


def load_chat_secret_key():
    """Use the same persistent signing key as Flask, even when Chat starts alone."""
    configured = os.environ.get("FLUIR_SECRET_KEY")
    if configured:
        return configured

    secret_path = INSTANCE_DIR / "fluir.secret"
    try:
        return secret_path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        secret = secrets.token_hex(32)
        try:
            with secret_path.open("x", encoding="utf-8") as secret_file:
                secret_file.write(secret)
            return secret
        except FileExistsError:
            return secret_path.read_text(encoding="utf-8").strip()


CHAT_SECRET_KEY = load_chat_secret_key()

# Criacao da aplicacao FastAPI do chat
app = FastAPI(title="Fluir Chat em Tempo Real")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/service-worker.js")
async def service_worker():
    return FileResponse(
        BASE_DIR / "static" / "service-worker.js",
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"},
    )


def avatar_public_url(avatar_path):
    if not avatar_path:
        return ""
    path = Path(avatar_path)
    if path.parent.as_posix() != "uploads/avatars" or not path.name:
        return ""
    file_path = BASE_DIR / "static" / path
    version = file_path.stat().st_mtime_ns if file_path.is_file() else ""
    return f"/static/uploads/avatars/{path.name}?v={version}"


def room_avatar_public_url(avatar_path):
    if not avatar_path:
        return ""
    path = Path(avatar_path)
    if path.parent.as_posix() != "uploads/chat-room-avatars" or not path.name:
        return ""
    file_path = BASE_DIR / "static" / path
    version = file_path.stat().st_mtime_ns if file_path.is_file() else ""
    return f"/static/uploads/chat-room-avatars/{path.name}?v={version}"


def group_room_allowed(room, porto, setor):
    return room in {
        sector_room_name(porto, setor),
        representatives_room_name(porto, setor),
        all_representatives_room_name(porto),
    }


def room_profile_porto(room, porto):
    # O grupo geral dos representantes e compartilhado entre todos os portos.
    return "*" if room == all_representatives_room_name(porto) else porto


def load_room_avatar(room, porto):
    profile_porto = room_profile_porto(room, porto)
    with sqlite3.connect(CHAT_DB) as conn:
        row = conn.execute(
            "SELECT avatar_path FROM chat_room_profiles WHERE room = ? AND porto = ?",
            (room, profile_porto),
        ).fetchone()
    return row[0] if row else ""


def load_room_display_name(room, porto):
    profile_porto = room_profile_porto(room, porto)
    with sqlite3.connect(CHAT_DB) as conn:
        row = conn.execute(
            "SELECT display_name FROM chat_room_profiles WHERE room = ? AND porto = ?",
            (room, profile_porto),
        ).fetchone()
    return row[0] if row else ""


def display_sector(value):
    labels = {
        "Operacoes Portuarias": "Operações Portuárias",
        "Navegacao e Trafego Fluvial": "Navegação e Tráfego Fluvial",
        "Seguranca do Trabalho": "Segurança do Trabalho",
        "Logistica": "Logística",
        "Fiscalizacao e Controle": "Fiscalização e Controle",
        "Administracao": "Administração",
    }
    return labels.get(value or "", value or "Setor não informado")


# Criacao da tabela de historico de mensagens
def setup_database():
    INSTANCE_DIR.mkdir(exist_ok=True)
    with sqlite3.connect(CHAT_DB) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                room TEXT NOT NULL,
                username TEXT NOT NULL,
                porto TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chat_conversations (
                room TEXT PRIMARY KEY,
                porto TEXT NOT NULL,
                participant_one TEXT NOT NULL,
                participant_two TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS chat_conversation_closed (
                room TEXT NOT NULL, porto TEXT NOT NULL, username TEXT NOT NULL,
                PRIMARY KEY (room, porto, username)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS chat_room_reads (
                room TEXT NOT NULL, porto TEXT NOT NULL, username TEXT NOT NULL,
                last_read_id INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (room, porto, username)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS chat_messages_hidden (
                room TEXT NOT NULL, username TEXT NOT NULL, message_id INTEGER NOT NULL,
                PRIMARY KEY (room, username, message_id)
            )"""
        )
        conn.execute(
            """CREATE TABLE IF NOT EXISTS chat_room_profiles (
                room TEXT NOT NULL, porto TEXT NOT NULL, avatar_path TEXT NOT NULL,
                updated_by TEXT NOT NULL, updated_at TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (room, porto)
            )"""
        )
        profile_columns = {row[1] for row in conn.execute("PRAGMA table_info(chat_room_profiles)")}
        if "display_name" not in profile_columns:
            conn.execute("ALTER TABLE chat_room_profiles ADD COLUMN display_name TEXT NOT NULL DEFAULT ''")


# Salvamento de mensagem no banco SQLite
def save_message(room, username, porto, message, created_at):
    with sqlite3.connect(CHAT_DB) as conn:
        cursor = conn.execute(
            """
            INSERT INTO chat_messages (room, username, porto, message, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (room, username, porto, message, created_at),
        )
        conn.execute(
            "UPDATE chat_conversations SET updated_at = ? WHERE room = ? AND porto = ?",
            (created_at, room, porto),
        )
        return cursor.lastrowid


def register_private_conversation(room, porto, first_user, second_user, created_at):
    participants = sorted((first_user, second_user))
    with sqlite3.connect(CHAT_DB) as conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO chat_conversations
                (room, porto, participant_one, participant_two, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (room, porto, participants[0], participants[1], created_at, created_at),
        )
        conn.execute("DELETE FROM chat_conversation_closed WHERE room = ? AND porto = ? AND username IN (?, ?)", (room, porto, first_user, second_user))


def close_private_conversation(room, porto, current_user):
    with sqlite3.connect(CHAT_DB) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO chat_conversation_closed (room, porto, username) VALUES (?, ?, ?)",
            (room, porto, current_user),
        )


def list_private_conversation_rooms(current_user, porto, possible_rooms):
    if not possible_rooms:
        return []
    placeholders = ",".join("?" for _ in possible_rooms)
    with sqlite3.connect(CHAT_DB) as conn:
        rows = conn.execute(
            f"""
            SELECT room FROM chat_conversations
            WHERE porto = ? AND (participant_one = ? OR participant_two = ?)
            ORDER BY updated_at DESC
            """,
            (porto, current_user, current_user),
        ).fetchall()
        known = {row[0] for row in rows}
        closed_rows = conn.execute("SELECT room FROM chat_conversation_closed WHERE porto = ? AND username = ?", (porto, current_user)).fetchall()
        closed = {row[0] for row in closed_rows}
        legacy_rows = conn.execute(
            f"""
            SELECT room, MAX(created_at) AS last_message
            FROM chat_messages
            WHERE porto = ? AND room IN ({placeholders})
            GROUP BY room
            ORDER BY last_message DESC
            """,
            (porto, *possible_rooms),
        ).fetchall()
    return [row[0] for row in rows if row[0] not in closed] + [row[0] for row in legacy_rows if row[0] not in known and row[0] not in closed]


# Consulta do historico recente de uma sala
def list_messages(room, limit=50, username=None):
    with sqlite3.connect(CHAT_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, username, porto, message, created_at
            FROM chat_messages
            WHERE room = ?
              AND (? IS NULL OR NOT EXISTS (
                  SELECT 1 FROM chat_messages_hidden hidden
                  WHERE hidden.room = chat_messages.room
                    AND hidden.username = ?
                    AND hidden.message_id = chat_messages.id
              ))
            ORDER BY id DESC
            LIMIT ?
            """,
            (room, username, username, limit),
        ).fetchall()
    messages = [dict(row) for row in reversed(rows)]
    if not messages or not USER_DB.exists():
        return messages
    usernames = {item["username"] for item in messages}
    placeholders = ",".join("?" for _ in usernames)
    with sqlite3.connect(USER_DB) as conn:
        conn.row_factory = sqlite3.Row
        records = conn.execute(
            f"SELECT username, nome_completo, cargo, poder_maior, avatar_path FROM users WHERE username IN ({placeholders})",
            tuple(usernames),
        ).fetchall()
    people = {row["username"]: dict(row) for row in records}
    for item in messages:
        person = people.get(item["username"], {})
        item["display_name"] = person.get("nome_completo") or "Usuário"
        item["role"] = "Representante do Setor" if person.get("poder_maior") else display_sector(person.get("cargo"))
        item["avatar_url"] = avatar_public_url(person.get("avatar_path"))
    return messages


# Oculta o historico apenas para a pessoa que solicitou a limpeza
def hide_messages(room, username):
    with sqlite3.connect(CHAT_DB) as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO chat_messages_hidden (room, username, message_id)
            SELECT room, ?, id FROM chat_messages WHERE room = ?
            """,
            (username, room),
        )
        return cursor.rowcount


def mark_room_read(room, porto, username, last_read_id=None):
    with sqlite3.connect(CHAT_DB) as conn:
        if last_read_id is None:
            last_read_id = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM chat_messages WHERE room = ?",
                (room,),
            ).fetchone()[0]
        conn.execute(
            """INSERT INTO chat_room_reads (room, porto, username, last_read_id)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(room, porto, username) DO UPDATE SET
                   last_read_id = MAX(chat_room_reads.last_read_id, excluded.last_read_id)""",
            (room, porto, username, last_read_id),
        )
    return last_read_id


def unread_count(room, porto, username):
    with sqlite3.connect(CHAT_DB) as conn:
        marker = conn.execute(
            "SELECT last_read_id FROM chat_room_reads WHERE room = ? AND porto = ? AND username = ?",
            (room, porto, username),
        ).fetchone()
        if marker is None:
            latest_id = conn.execute(
                "SELECT COALESCE(MAX(id), 0) FROM chat_messages WHERE room = ?",
                (room,),
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO chat_room_reads (room, porto, username, last_read_id) VALUES (?, ?, ?, ?)",
                (room, porto, username, latest_id),
            )
            return 0
        return conn.execute(
            "SELECT COUNT(*) FROM chat_messages WHERE room = ? AND id > ? AND username != ?",
            (room, marker[0], username),
        ).fetchone()[0]


# Consulta dos funcionarios do mesmo porto para abrir chats privados
def list_chat_users(current_user, porto):
    if not USER_DB.exists():
        return []
    with sqlite3.connect(USER_DB) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT username, nome_completo, codigo_porto, cargo, poder_maior, avatar_path
            FROM users
            WHERE username != ? AND codigo_porto = ? AND status_aprovacao = 'aprovada'
            ORDER BY nome_completo, username
            """,
            (current_user, porto),
        ).fetchall()
    users = []
    for row in rows:
        item = dict(row)
        item["display_name"] = item.get("nome_completo") or "Usuário"
        item["role"] = "Representante do Setor" if item.get("poder_maior") else display_sector(item.get("cargo"))
        item["avatar_url"] = avatar_public_url(item.get("avatar_path"))
        users.append(item)
    return users


# Consulta dos representantes do mesmo setor para o grupo setorial
def list_representatives(porto=None, setor=None):
    if not USER_DB.exists():
        return []
    with sqlite3.connect(USER_DB) as conn:
        conn.row_factory = sqlite3.Row
        query = """
            SELECT username, nome_completo, codigo_porto, cargo
            FROM users
            WHERE poder_maior = 1 AND status_aprovacao = 'aprovada'
            """
        params = []
        if porto is not None:
            query += " AND codigo_porto = ?"
            params.append(porto)
        if setor is not None:
            query += " AND cargo = ?"
            params.append(setor)
        query += " ORDER BY cargo, nome_completo, username"
        rows = conn.execute(query, params).fetchall()
    return [dict(row) for row in rows]


# Consulta do representante autenticado por parametros vindos do Flask
def get_chat_user(username, porto):
    if not USER_DB.exists():
        return None
    with sqlite3.connect(USER_DB) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """
            SELECT username, nome_completo, codigo_porto, cargo, poder_maior, avatar_path
            FROM users
            WHERE username = ? AND codigo_porto = ? AND status_aprovacao = 'aprovada'
            """,
            (username, porto),
        ).fetchone()
    return dict(row) if row else None


# Gerenciador local de conexoes WebSocket sem Redis
class LocalConnectionManager:
    def __init__(self):
        self.rooms = {}

    async def connect(self, room, websocket):
        await websocket.accept()
        self.rooms.setdefault(room, set()).add(websocket)

    def disconnect(self, room, websocket):
        sockets = self.rooms.get(room)
        if not sockets:
            return
        sockets.discard(websocket)
        if not sockets:
            self.rooms.pop(room, None)

    async def broadcast(self, room, payload):
        sockets = list(self.rooms.get(room, set()))
        for socket in sockets:
            try:
                await socket.send_json(payload)
            except RuntimeError:
                self.disconnect(room, socket)


# Estado compartilhado do chat em execucao
manager = LocalConnectionManager()
redis_client = None


# Inicializacao do banco e tentativa de conexao com Redis
@app.on_event("startup")
async def startup():
    global redis_client
    setup_database()
    if redis is None:
        return
    try:
        client = redis.from_url(REDIS_URL, decode_responses=True)
        await client.ping()
        redis_client = client
    except Exception:
        redis_client = None


# Encerramento limpo da conexao Redis
@app.on_event("shutdown")
async def shutdown():
    if redis_client is not None:
        await redis_client.aclose()


# Limpeza e limitacao de textos recebidos do usuario
def clean_text(value, fallback, limit):
    value = (value or "").strip()
    if not value:
        return fallback
    return value[:limit]


# Nome padronizado do canal Pub/Sub do Redis
def channel_name(room):
    safe_room = "".join(char for char in room if char.isalnum() or char in "-_")[:80]
    return f"fluir-chat:{safe_room or 'geral'}"


# Normalizacao do nome de sala privada entre dois usuarios
def private_room_name(first_user, second_user):
    users = sorted([clean_text(first_user, "usuario", 80), clean_text(second_user, "usuario", 80)])
    safe_users = [
        "".join(char for char in user if char.isalnum() or char in "-_")[:40] or "usuario"
        for user in users
    ]
    return f"privado-{safe_users[0]}-{safe_users[1]}"


# Normalizacao do nome do setor para sala isolada
def sector_slug(setor):
    normalized = unicodedata.normalize("NFKD", setor or "geral")
    without_accents = "".join(char for char in normalized if not unicodedata.combining(char))
    safe = "".join(char.lower() for char in without_accents if char.isalnum())
    return safe or "geral"


# Nome oficial da sala setorial
def sector_room_name(porto, setor):
    return f"{clean_text(porto, 'BR123', 20)}-{sector_slug(setor)}"


# Nome oficial do grupo de representantes do mesmo setor
def representatives_room_name(porto, setor):
    return f"{clean_text(porto, 'BR123', 20)}-representantes-{sector_slug(setor)}"


def all_representatives_room_name(porto):
    return "todos-representantes-fluir"


# Validacao de acesso ao chat por setor
def validate_chat_token(user, porto, setor, token):
    payload = f"{user}|{porto}|{setor}".encode("utf-8")
    expected = hmac.new(CHAT_SECRET_KEY.encode("utf-8"), payload, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, token or "")


def validate_user(user, porto, setor, token):
    if not validate_chat_token(user, porto, setor, token):
        return None
    record = get_chat_user(user, porto)
    if not record or record["cargo"] != setor:
        return None
    return record


# Validacao se a sala solicitada pertence ao setor do representante
def room_is_allowed(room, user, porto, setor):
    record = get_chat_user(user, porto)
    if not record or record["cargo"] != setor:
        return False
    if room == sector_room_name(porto, setor):
        return True
    if room == representatives_room_name(porto, setor):
        return bool(record["poder_maior"]) and len(list_representatives(porto, setor)) >= 1
    if room == all_representatives_room_name(porto):
        return bool(record["poder_maior"]) and len(list_representatives()) >= 1
    if not room.startswith("privado-"):
        return False
    for item in list_chat_users(user, porto):
        if room == private_room_name(user, item["username"]):
            return True
    return False


# Redirecionamento inicial para a tela do chat
@app.get("/")
async def index():
    return RedirectResponse(url="/chat")


@app.get("/health")
async def health():
    return {"status": "ok", "build": CHAT_BUILD_ID}


# Renderizacao da pagina HTML do chat
@app.get("/chat", response_class=HTMLResponse)
async def chat_page(
    user: str = Query("Funcionario"),
    porto: str = Query("BR123"),
    setor: str = Query("Setor nao informado"),
    room: str = Query("geral"),
    token: str = Query(""),
):
    user = clean_text(user, "Funcionario", 80)
    porto = clean_text(porto, "BR123", 20)
    setor = clean_text(setor, "Setor nao informado", 120)
    room = clean_text(room, sector_room_name(porto, setor), 80)
    token = clean_text(token, "", 128)
    user_record = validate_user(user, porto, setor, token)
    if not user_record or not room_is_allowed(room, user, porto, setor):
        return HTMLResponse(
            """<!doctype html><html lang="pt-BR"><meta charset="utf-8">
            <meta name="viewport" content="width=device-width,initial-scale=1">
            <title>Acesso ao chat</title><style>
            *{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;
            padding:24px;background:#0b1220;color:#e5edf8;font:16px Arial,sans-serif}
            main{width:min(100%,480px);padding:28px;border:1px solid #354861;border-radius:16px;
            background:#17263a}h1{margin:0 0 12px;font-size:24px}p{line-height:1.55;color:#b8c8dd}
            a{display:inline-block;margin-top:12px;padding:11px 16px;border-radius:8px;background:#2563eb;
            color:white;text-decoration:none;font-weight:700}</style><main>
            <h1>Não foi possível abrir o chat</h1>
            <p>Volte ao Fluir e abra o Chat novamente para atualizar seu acesso.</p>
            <a href="http://127.0.0.1:5000/chat">Voltar ao Fluir</a></main></html>""",
            status_code=403,
        )
    template = CHAT_TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "{{ user }}": html.escape(user),
        "{{ nome_completo }}": html.escape(user_record["nome_completo"] or "Usuário"),
        "{{ avatar_url }}": html.escape(avatar_public_url(user_record.get("avatar_path")) or ""),
        "{{ setor_exibicao }}": html.escape("Representante do Setor" if user_record["poder_maior"] else display_sector(setor)),
        "{{ porto }}": html.escape(porto),
        "{{ setor }}": html.escape(setor),
        "{{ room }}": html.escape(room),
        "{{ token }}": html.escape(token),
        "{{ is_representative }}": "true" if user_record["poder_maior"] else "false",
        "{{ representatives_room }}": html.escape(representatives_room_name(porto, setor)),
        "{{ all_representatives_room }}": html.escape(all_representatives_room_name(porto)),
        "{{ link_funcionarios }}": '<a href="http://127.0.0.1:5000/funcionarios">Funcionários</a>' if setor == "Fiscalizacao e Controle" else "",
    }
    for token, value in replacements.items():
        template = template.replace(token, value)
    return HTMLResponse(template)


# API de historico de mensagens da sala
@app.get("/history/{room}")
async def history(
    room: str,
    user: str = Query("Funcionario"),
    porto: str = Query("BR123"),
    setor: str = Query("Setor nao informado"),
    token: str = Query(""),
    limit: int = Query(50, ge=1, le=100),
):
    room = clean_text(room, "geral", 80)
    user = clean_text(user, "Funcionario", 80)
    porto = clean_text(porto, "BR123", 20)
    setor = clean_text(setor, "Setor nao informado", 120)
    token = clean_text(token, "", 128)
    user_record = validate_user(user, porto, setor, token)
    if not user_record or not room_is_allowed(room, user, porto, setor):
        return {"messages": []}
    messages = await asyncio.to_thread(list_messages, room, limit, user)
    latest_id = messages[-1]["id"] if messages else 0
    await asyncio.to_thread(mark_room_read, room, porto, user, latest_id)
    return {"messages": messages}


@app.get("/unread")
async def unread_messages(
    user: str = Query("Funcionario"),
    porto: str = Query("BR123"),
    setor: str = Query("Setor nao informado"),
    token: str = Query(""),
):
    user = clean_text(user, "Funcionario", 80)
    porto = clean_text(porto, "BR123", 20)
    setor = clean_text(setor, "Setor nao informado", 120)
    token = clean_text(token, "", 128)
    user_record = validate_user(user, porto, setor, token)
    if not user_record:
        # Uma falha de autenticação não significa que não há mensagens novas.
        # O 401 faz os clientes manterem o último contador conhecido.
        raise HTTPException(status_code=401, detail="Sessão do chat inválida")

    rooms = [sector_room_name(porto, setor)]
    if user_record["poder_maior"]:
        rooms.extend((representatives_room_name(porto, setor), all_representatives_room_name(porto)))

    directory = await asyncio.to_thread(list_chat_users, user, porto)
    possible_private_rooms = [private_room_name(user, item["username"]) for item in directory]
    opened_private_rooms = await asyncio.to_thread(
        list_private_conversation_rooms, user, porto, possible_private_rooms
    )
    rooms.extend(opened_private_rooms)

    unread = {}
    for room in dict.fromkeys(rooms):
        if room_is_allowed(room, user, porto, setor):
            unread[room] = await asyncio.to_thread(unread_count, room, porto, user)
    return {"unread": unread}


@app.post("/read/{room}")
async def mark_read(
    room: str,
    user: str = Query("Funcionario"),
    porto: str = Query("BR123"),
    setor: str = Query("Setor nao informado"),
    token: str = Query(""),
):
    room = clean_text(room, "geral", 80)
    user = clean_text(user, "Funcionario", 80)
    porto = clean_text(porto, "BR123", 20)
    setor = clean_text(setor, "Setor nao informado", 120)
    token = clean_text(token, "", 128)
    if not validate_user(user, porto, setor, token) or not room_is_allowed(room, user, porto, setor):
        return {"read": False}
    last_read_id = await asyncio.to_thread(mark_room_read, room, porto, user)
    return {"read": True, "last_read_id": last_read_id}


@app.get("/room-avatar/{room}")
async def get_room_avatar(
    room: str,
    user: str = Query("Funcionario"),
    porto: str = Query("BR123"),
    setor: str = Query("Setor nao informado"),
    token: str = Query(""),
):
    room = clean_text(room, "", 80)
    user = clean_text(user, "Funcionario", 80)
    porto = clean_text(porto, "BR123", 20)
    setor = clean_text(setor, "Setor nao informado", 120)
    token = clean_text(token, "", 128)
    if not validate_user(user, porto, setor, token) or not room_is_allowed(room, user, porto, setor):
        return JSONResponse({"error": "Acesso não autorizado."}, status_code=403)
    if not group_room_allowed(room, porto, setor):
        return {"avatar_url": "", "display_name": ""}
    avatar_path = await asyncio.to_thread(load_room_avatar, room, porto)
    display_name = await asyncio.to_thread(load_room_display_name, room, porto)
    return {"avatar_url": room_avatar_public_url(avatar_path), "display_name": display_name}


@app.post("/room-name/{room}")
async def update_room_name(
    room: str,
    request: Request,
    user: str = Query("Funcionario"),
    porto: str = Query("BR123"),
    setor: str = Query("Setor nao informado"),
    token: str = Query(""),
):
    room = clean_text(room, "", 80)
    user = clean_text(user, "Funcionario", 80)
    porto = clean_text(porto, "BR123", 20)
    setor = clean_text(setor, "Setor nao informado", 120)
    token = clean_text(token, "", 128)
    user_record = validate_user(user, porto, setor, token)
    if (
        not user_record
        or not user_record["poder_maior"]
        or not room_is_allowed(room, user, porto, setor)
        or not group_room_allowed(room, porto, setor)
    ):
        return JSONResponse({"error": "Somente representantes podem alterar o nome deste grupo."}, status_code=403)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Digite um nome para o grupo."}, status_code=400)
    raw_name = payload.get("name") if isinstance(payload, dict) else None
    if not isinstance(raw_name, str):
        return JSONResponse({"error": "Digite um nome para o grupo."}, status_code=400)
    display_name = "".join(char for char in raw_name if char >= " " and char != "\x7f").strip()
    if not display_name:
        return JSONResponse({"error": "Digite um nome para o grupo."}, status_code=400)
    if len(display_name) > 50:
        return JSONResponse({"error": "O nome do grupo deve ter até 50 caracteres."}, status_code=400)

    profile_porto = room_profile_porto(room, porto)
    updated_at = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(CHAT_DB) as conn:
        existing = conn.execute(
            "SELECT avatar_path FROM chat_room_profiles WHERE room = ? AND porto = ?",
            (room, profile_porto),
        ).fetchone()
        avatar_path = existing[0] if existing else ""
        conn.execute(
            """INSERT INTO chat_room_profiles
                   (room, porto, avatar_path, updated_by, updated_at, display_name)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(room, porto) DO UPDATE SET
                   updated_by=excluded.updated_by,
                   updated_at=excluded.updated_at,
                   display_name=excluded.display_name""",
            (room, profile_porto, avatar_path, user, updated_at, display_name),
        )
    return {"updated": True, "display_name": display_name}


@app.post("/room-avatar/{room}")
async def update_room_avatar(
    room: str,
    request: Request,
    user: str = Query("Funcionario"),
    porto: str = Query("BR123"),
    setor: str = Query("Setor nao informado"),
    token: str = Query(""),
):
    room = clean_text(room, "", 80)
    user = clean_text(user, "Funcionario", 80)
    porto = clean_text(porto, "BR123", 20)
    setor = clean_text(setor, "Setor nao informado", 120)
    token = clean_text(token, "", 128)
    user_record = validate_user(user, porto, setor, token)
    if (
        not user_record
        or not user_record["poder_maior"]
        or not room_is_allowed(room, user, porto, setor)
        or not group_room_allowed(room, porto, setor)
    ):
        return JSONResponse({"error": "Somente representantes podem alterar a foto deste grupo."}, status_code=403)

    content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    extensions = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}
    extension = extensions.get(content_type)
    if not extension:
        return JSONResponse({"error": "Escolha uma imagem JPG, PNG ou WebP."}, status_code=415)
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > MAX_CHAT_ROOM_AVATAR_SIZE:
        return JSONResponse({"error": "A imagem precisa ter até 5 MB."}, status_code=413)
    image_bytes = await request.body()
    if not image_bytes or len(image_bytes) > MAX_CHAT_ROOM_AVATAR_SIZE:
        return JSONResponse({"error": "A imagem precisa ter até 5 MB."}, status_code=413)
    valid_signature = (
        (extension == ".jpg" and image_bytes.startswith(b"\xff\xd8\xff"))
        or (extension == ".png" and image_bytes.startswith(b"\x89PNG\r\n\x1a\n"))
        or (extension == ".webp" and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP")
    )
    if not valid_signature:
        return JSONResponse({"error": "O arquivo selecionado não parece ser uma imagem válida."}, status_code=415)

    CHAT_ROOM_AVATAR_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{secrets.token_hex(16)}{extension}"
    image_file = CHAT_ROOM_AVATAR_DIR / filename
    image_file.write_bytes(image_bytes)
    relative_path = f"uploads/chat-room-avatars/{filename}"
    updated_at = datetime.now(timezone.utc).isoformat()
    profile_porto = room_profile_porto(room, porto)
    try:
        with sqlite3.connect(CHAT_DB) as conn:
            previous = conn.execute(
                "SELECT avatar_path FROM chat_room_profiles WHERE room = ? AND porto = ?",
                (room, profile_porto),
            ).fetchone()
            conn.execute(
                """INSERT INTO chat_room_profiles (room, porto, avatar_path, updated_by, updated_at)
                   VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(room, porto) DO UPDATE SET
                       avatar_path=excluded.avatar_path,
                       updated_by=excluded.updated_by,
                       updated_at=excluded.updated_at""",
                (room, profile_porto, relative_path, user, updated_at),
            )
        old_path = previous[0] if previous else ""
        if old_path and old_path != relative_path:
            old_file = BASE_DIR / "static" / old_path
            try:
                old_file.resolve().relative_to(CHAT_ROOM_AVATAR_DIR.resolve())
                old_file.unlink(missing_ok=True)
            except (ValueError, OSError):
                pass
    except Exception:
        image_file.unlink(missing_ok=True)
        raise
    return {"updated": True, "avatar_url": room_avatar_public_url(relative_path)}


# Exclusao do historico de uma conversa
@app.delete("/history/{room}")
async def delete_history(
    room: str,
    user: str = Query("Funcionario"),
    porto: str = Query("BR123"),
    setor: str = Query("Setor nao informado"),
    token: str = Query(""),
):
    room = clean_text(room, "geral", 80)
    user = clean_text(user, "Funcionario", 80)
    porto = clean_text(porto, "BR123", 20)
    setor = clean_text(setor, "Setor nao informado", 120)
    token = clean_text(token, "", 128)
    if not validate_user(user, porto, setor, token) or not room_is_allowed(room, user, porto, setor):
        return {"deleted": 0}
    hidden = await asyncio.to_thread(hide_messages, room, user)
    return {"hidden": hidden}


# API que lista usuarios disponiveis para chat privado
@app.get("/users")
async def users(
    user: str = Query("Funcionario"),
    porto: str = Query("BR123"),
    setor: str = Query("Setor nao informado"),
    token: str = Query(""),
):
    clean_user = clean_text(user, "Funcionario", 80)
    clean_porto = clean_text(porto, "BR123", 20)
    clean_setor = clean_text(setor, "Setor nao informado", 120)
    clean_token = clean_text(token, "", 128)
    user_record = validate_user(clean_user, clean_porto, clean_setor, clean_token)
    if not user_record:
        return {"users": []}
    chat_users = await asyncio.to_thread(list_chat_users, clean_user, clean_porto)
    representatives = []
    all_representatives = []
    if user_record["poder_maior"]:
        representatives = await asyncio.to_thread(list_representatives, clean_porto, clean_setor)
        all_representatives = await asyncio.to_thread(list_representatives)
    return {
        "users": [
            {
                **item,
                "private_room": private_room_name(clean_user, item["username"]),
            }
            for item in chat_users
        ],
        "representatives_count": len(representatives),
        "all_representatives_count": len(all_representatives),
    }


@app.get("/conversations")
async def conversations(
    user: str = Query("Funcionario"),
    porto: str = Query("BR123"),
    setor: str = Query("Setor nao informado"),
    token: str = Query(""),
):
    clean_user = clean_text(user, "Funcionario", 80)
    clean_porto = clean_text(porto, "BR123", 20)
    clean_setor = clean_text(setor, "Setor nao informado", 120)
    clean_token = clean_text(token, "", 128)
    if not validate_user(clean_user, clean_porto, clean_setor, clean_token):
        return {"conversations": []}
    directory = await asyncio.to_thread(list_chat_users, clean_user, clean_porto)
    room_to_contact = {item["private_room"]: item for item in (
        {**contact, "private_room": private_room_name(clean_user, contact["username"])}
        for contact in directory
    )}
    opened_rooms = await asyncio.to_thread(
        list_private_conversation_rooms,
        clean_user,
        clean_porto,
        list(room_to_contact),
    )
    return {"conversations": [
        {**room_to_contact[room], "room": room}
        for room in room_to_contact if room in opened_rooms
    ]}


@app.post("/conversations/{contact_username}")
async def open_private_conversation(
    contact_username: str,
    user: str = Query("Funcionario"),
    porto: str = Query("BR123"),
    setor: str = Query("Setor nao informado"),
    token: str = Query(""),
):
    clean_user = clean_text(user, "Funcionario", 80)
    clean_porto = clean_text(porto, "BR123", 20)
    clean_setor = clean_text(setor, "Setor nao informado", 120)
    clean_token = clean_text(token, "", 128)
    if not validate_user(clean_user, clean_porto, clean_setor, clean_token):
        return {"error": "Acesso não autorizado."}
    target = get_chat_user(clean_text(contact_username, "", 80), clean_porto)
    if not target or target["username"] == clean_user:
        return {"error": "Funcionário não encontrado."}
    room = private_room_name(clean_user, target["username"])
    created_at = datetime.now(timezone.utc).isoformat()
    await asyncio.to_thread(
        register_private_conversation,
        room,
        clean_porto,
        clean_user,
        target["username"],
        created_at,
    )
    return {
        "room": room,
        "display_name": target["nome_completo"] or "Usuário",
        "role": "Representante do Setor" if target["poder_maior"] else display_sector(target["cargo"]),
        "avatar_url": avatar_public_url(target.get("avatar_path")),
    }


@app.delete("/conversations/{contact_username}")
async def close_conversation(
    contact_username: str,
    user: str = Query("Funcionario"),
    porto: str = Query("BR123"),
    setor: str = Query("Setor nao informado"),
    token: str = Query(""),
):
    clean_user = clean_text(user, "Funcionario", 80)
    clean_porto = clean_text(porto, "BR123", 20)
    clean_setor = clean_text(setor, "Setor nao informado", 120)
    clean_token = clean_text(token, "", 128)
    if not validate_user(clean_user, clean_porto, clean_setor, clean_token):
        return {"error": "Acesso não autorizado."}
    target = get_chat_user(clean_text(contact_username, "", 80), clean_porto)
    if not target or target["username"] == clean_user:
        return {"error": "Funcionário não encontrado."}
    room = private_room_name(clean_user, target["username"])
    await asyncio.to_thread(close_private_conversation, room, clean_porto, clean_user)
    return {"closed": True}


# Escuta das mensagens publicadas no Redis
async def relay_redis_messages(websocket, room):
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(channel_name(room))
    try:
        async for item in pubsub.listen():
            if item.get("type") == "message":
                await websocket.send_text(item["data"])
    finally:
        await pubsub.unsubscribe(channel_name(room))
        await pubsub.aclose()


# WebSocket principal para envio e recebimento ao vivo
@app.websocket("/ws/{room}")
async def websocket_chat(
    websocket: WebSocket,
    room: str,
    user: str = Query("Funcionario"),
    porto: str = Query("BR123"),
    setor: str = Query("Setor nao informado"),
    token: str = Query(""),
):
    room = clean_text(room, "geral", 80)
    user = clean_text(user, "Funcionario", 80)
    porto = clean_text(porto, "BR123", 20)
    setor = clean_text(setor, "Setor nao informado", 120)
    token = clean_text(token, "", 128)
    user_record = validate_user(user, porto, setor, token)
    if not user_record or not room_is_allowed(room, user, porto, setor):
        await websocket.close(code=1008)
        return
    redis_task = None

    if redis_client is None:
        await manager.connect(room, websocket)
    else:
        await websocket.accept()
        redis_task = asyncio.create_task(relay_redis_messages(websocket, room))

    try:
        while True:
            raw_message = await websocket.receive_text()
            message = clean_text(raw_message, "", 700)
            if not message:
                continue

            payload = {
                "room": room,
                "username": user,
                "display_name": user_record["nome_completo"] or "Usuário",
                "role": "Representante do Setor" if user_record["poder_maior"] else display_sector(setor),
                "avatar_url": avatar_public_url(user_record.get("avatar_path")),
                "porto": porto,
                "message": message,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            payload["id"] = await asyncio.to_thread(
                save_message,
                payload["room"],
                payload["username"],
                payload["porto"],
                payload["message"],
                payload["created_at"],
            )

            if redis_client is None:
                await manager.broadcast(room, payload)
            else:
                await redis_client.publish(channel_name(room), json.dumps(payload))
    except WebSocketDisconnect:
        pass
    finally:
        if redis_task is not None:
            redis_task.cancel()
        manager.disconnect(room, websocket)
