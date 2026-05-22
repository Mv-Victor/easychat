#!/usr/bin/env python3
import base64
import hashlib
import hmac
import json
import os
import re
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional, Union
from urllib.parse import urlparse


ROOT = Path(__file__).resolve().parent
DIST_DIR = ROOT / "frontend" / "dist"
UPLOAD_DIR = ROOT / "uploads"
DB_PATH = Path(os.environ.get("EASYCHAT_DB", ROOT / "easychat.sqlite3"))
HOST = os.environ.get("EASYCHAT_HOST", "127.0.0.1")
PORT = int(os.environ.get("EASYCHAT_PORT", "7860"))
DEFAULT_OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5.5")
DEFAULT_CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-7")
DEFAULT_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2")
OPENAI_BASE_URL = os.environ.get("OPENAI_BASE_URL", "https://tkcc.cloud").rstrip("/")
CLAUDE_BASE_URL = os.environ.get("CLAUDE_BASE_URL", "https://tkcc.cloud").rstrip("/")
DEVICE_TOKEN_SECRET = os.environ.get("EASYCHAT_DEVICE_TOKEN_SECRET", "easychat-dev-secret-change-me")
IMAGE_REQUEST_TIMEOUT = int(os.environ.get("OPENAI_IMAGE_TIMEOUT", "600"))
MAX_UPLOAD_IMAGE_BYTES = int(os.environ.get("EASYCHAT_MAX_UPLOAD_IMAGE_BYTES", str(8 * 1024 * 1024)))


MIME_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}

IMAGE_EXTENSIONS = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
}


def now() -> int:
    return int(time.time())


def make_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def json_dumps(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def mask_secrets(text: str) -> str:
    return re.sub(r"sk-[A-Za-z0-9_-]{12,}", "sk-***", text)


def is_valid_device_id(device_id: str) -> bool:
    return bool(re.fullmatch(r"device_[a-f0-9]{32}", device_id))


def sign_device_id(device_id: str) -> str:
    return hmac.new(DEVICE_TOKEN_SECRET.encode("utf-8"), device_id.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_device_token() -> str:
    device_id = make_id("device")
    return f"{device_id}.{sign_device_id(device_id)}"


def verify_device_token(token: Optional[str]) -> Optional[str]:
    if not token or "." not in token:
        return None
    device_id, signature = token.rsplit(".", 1)
    if not is_valid_device_id(device_id):
        return None
    expected = sign_device_id(device_id)
    if hmac.compare_digest(signature, expected):
        return device_id
    return None


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                device_id TEXT NOT NULL UNIQUE,
                mac_address TEXT,
                provider TEXT NOT NULL CHECK(provider IN ('openai', 'claude')),
                api_key TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                provider TEXT NOT NULL CHECK(provider IN ('openai', 'claude')),
                model TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                updated_at INTEGER NOT NULL,
                FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                conversation_id TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('user', 'assistant', 'system')),
                content TEXT NOT NULL,
                image_url TEXT,
                created_at INTEGER NOT NULL,
                FOREIGN KEY(conversation_id) REFERENCES conversations(id) ON DELETE CASCADE
            );
            """
        )


def detect_mac(ip: Optional[str]) -> Optional[str]:
    if not ip or ip in {"127.0.0.1", "::1", "localhost"}:
        return None
    try:
        output = subprocess.check_output(["arp", "-n", ip], text=True, timeout=1.5)
    except (OSError, subprocess.SubprocessError):
        return None
    for token in output.replace("(", " ").replace(")", " ").split():
        normalized = token.lower()
        parts = normalized.split(":")
        if len(parts) == 6 and all(len(part) == 2 for part in parts):
            return normalized
    return None


def title_from_prompt(prompt: str) -> str:
    compact = " ".join(prompt.strip().split())
    return compact[:32] or "新会话"


def normalize_device_id(device_id: Optional[str]) -> str:
    if device_id and is_valid_device_id(device_id):
        return device_id
    return make_id("device")


def get_user(device_id: Optional[str]) -> Optional[sqlite3.Row]:
    device_id = normalize_device_id(device_id)
    with db() as conn:
        return conn.execute("SELECT * FROM users WHERE device_id=?", (device_id,)).fetchone()


def list_conversations(user_id: int) -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            """
            SELECT id,title,provider,model,created_at,updated_at
            FROM conversations
            WHERE user_id=?
            ORDER BY updated_at DESC
            """,
            (user_id,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "title": row["title"],
            "provider": row["provider"],
            "model": row["model"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
        for row in rows
    ]


def create_conversation(user_id: int, provider: str, title: str) -> str:
    model = DEFAULT_OPENAI_MODEL if provider == "openai" else DEFAULT_CLAUDE_MODEL
    conversation_id = make_id("chat")
    ts = now()
    with db() as conn:
        conn.execute(
            """
            INSERT INTO conversations(id,user_id,title,provider,model,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?)
            """,
            (conversation_id, user_id, title, provider, model, ts, ts),
        )
    return conversation_id


def update_conversation_title(user_id: int, conversation_id: str, title: str) -> Optional[Dict[str, Any]]:
    title = title.strip()[:80]
    if not title:
        title = "新会话"
    ts = now()
    with db() as conn:
        row = conn.execute(
            "SELECT id FROM conversations WHERE id=? AND user_id=?",
            (conversation_id, user_id),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE conversations SET title=?, updated_at=? WHERE id=? AND user_id=?",
            (title, ts, conversation_id, user_id),
        )
        conversation = conn.execute(
            "SELECT id,title,provider,model,created_at,updated_at FROM conversations WHERE id=? AND user_id=?",
            (conversation_id, user_id),
        ).fetchone()
    return {
        "id": conversation["id"],
        "title": conversation["title"],
        "provider": conversation["provider"],
        "model": conversation["model"],
        "createdAt": conversation["created_at"],
        "updatedAt": conversation["updated_at"],
    }


def delete_conversation(user_id: int, conversation_id: str) -> bool:
    with db() as conn:
        cursor = conn.execute(
            "DELETE FROM conversations WHERE id=? AND user_id=?",
            (conversation_id, user_id),
        )
    return cursor.rowcount > 0


def ensure_conversation(user: sqlite3.Row, conversation_id: Optional[str], first_message: str) -> str:
    if conversation_id:
        with db() as conn:
            found = conn.execute(
                "SELECT id FROM conversations WHERE id=? AND user_id=?",
                (conversation_id, user["id"]),
            ).fetchone()
        if found:
            return conversation_id
    return create_conversation(user["id"], user["provider"], title_from_prompt(first_message))


def persist_message(conversation_id: str, role: str, content: str, image_url: Optional[str] = None) -> None:
    ts = now()
    with db() as conn:
        conn.execute(
            "INSERT INTO messages(id,conversation_id,role,content,image_url,created_at) VALUES(?,?,?,?,?,?)",
            (make_id("msg"), conversation_id, role, content, image_url, ts),
        )
        conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (ts, conversation_id))


def load_messages(conversation_id: str) -> List[Dict[str, Any]]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE conversation_id=? ORDER BY created_at ASC",
            (conversation_id,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "role": row["role"],
            "content": row["content"],
            "imageUrl": row["image_url"],
            "createdAt": row["created_at"],
        }
        for row in rows
    ]


def markdown_image_urls(content: str) -> List[str]:
    return re.findall(r"!\[[^\]]*\]\((/uploads/[^)]+)\)", content or "")


def upload_url_to_base64(url: str) -> Optional[Dict[str, str]]:
    relative = url.removeprefix("/uploads/").lstrip("/")
    candidate = (UPLOAD_DIR / relative).resolve()
    if not UPLOAD_DIR.exists() or (UPLOAD_DIR not in candidate.parents and candidate != UPLOAD_DIR) or not candidate.is_file():
        return None
    content_type = MIME_TYPES.get(candidate.suffix.lower(), "image/png").split(";", 1)[0]
    return {
        "type": content_type,
        "data": base64.b64encode(candidate.read_bytes()).decode("ascii"),
    }


def strip_image_reference_block(content: str) -> str:
    return re.sub(r"\n{0,2}参考图：(?:\n!\[[^\]]*\]\([^)]+\))+", "", content or "").strip()


def history_for_provider(conversation_id: str) -> List[Dict[str, Union[str, List[Dict[str, str]]]]]:
    history: List[Dict[str, Union[str, List[Dict[str, str]]]]] = []
    for row in load_messages(conversation_id):
        if row["role"] not in {"user", "assistant"}:
            continue
        image_urls = markdown_image_urls(row["content"]) if row["role"] == "user" else []
        images = [image for image in (upload_url_to_base64(url) for url in image_urls) if image]
        history.append(
            {
                "role": row["role"],
                "content": strip_image_reference_block(row["content"]) if images else row["content"],
                "images": images,
            }
        )
    return history


def request_json(url: str, headers: Dict[str, str], body: Dict[str, Any], timeout: int = 180) -> Dict[str, Any]:
    req = urllib.request.Request(
        url,
        data=json_dumps(body),
        headers={"Content-Type": "application/json", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(mask_secrets(f"{exc.code} {exc.reason}: {detail}")) from exc
    except TimeoutError as exc:
        raise RuntimeError("请求超时，请稍后重试") from exc


def request_multipart(url: str, headers: Dict[str, str], fields: Dict[str, str], files: List[Dict[str, str]], timeout: int = 180) -> Dict[str, Any]:
    boundary = f"----EasyChatBoundary{uuid.uuid4().hex}"
    body = bytearray()
    for name, value in fields.items():
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        body.extend(str(value).encode("utf-8"))
        body.extend(b"\r\n")
    for file_item in files:
        body.extend(f"--{boundary}\r\n".encode("utf-8"))
        body.extend(
            (
                f'Content-Disposition: form-data; name="{file_item["field"]}"; '
                f'filename="{file_item["filename"]}"\r\n'
                f'Content-Type: {file_item["contentType"]}\r\n\r\n'
            ).encode("utf-8")
        )
        body.extend(base64.b64decode(file_item["base64"]))
        body.extend(b"\r\n")
    body.extend(f"--{boundary}--\r\n".encode("utf-8"))
    req = urllib.request.Request(
        url,
        data=bytes(body),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(mask_secrets(f"{exc.code} {exc.reason}: {detail}")) from exc
    except TimeoutError as exc:
        raise RuntimeError("请求超时，请稍后重试") from exc


def stream_openai(api_key: str, messages: List[Dict[str, Union[str, List[Dict[str, str]]]]]) -> Generator[str, None, None]:
    def content_parts(msg: Dict[str, Union[str, List[Dict[str, str]]]]) -> List[Dict[str, str]]:
        text_type = "input_text" if msg["role"] == "user" else "output_text"
        parts = [{"type": text_type, "text": str(msg["content"])}]
        if msg["role"] == "user":
            for image in msg.get("images") or []:
                if isinstance(image, dict) and image.get("data"):
                    parts.append({"type": "input_image", "image_url": f"data:{image.get('type') or 'image/png'};base64,{image['data']}"})
        return parts

    body = {
        "model": DEFAULT_OPENAI_MODEL,
        "stream": True,
        "input": [
            {
                "role": msg["role"],
                "content": content_parts(msg),
            }
            for msg in messages
        ],
    }
    req = urllib.request.Request(
        f"{OPENAI_BASE_URL}/v1/responses",
        data=json_dumps(body),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data == "[DONE]":
                    break
                try:
                    event = json.loads(data)
                except json.JSONDecodeError:
                    continue
                event_type = event.get("type")
                if event_type in {"response.output_text.delta", "response.refusal.delta"}:
                    yield event.get("delta", "")
                elif event_type == "response.completed":
                    break
                elif event_type == "response.failed":
                    error = event.get("response", {}).get("error") or event.get("error") or {}
                    raise RuntimeError(error.get("message") or "OpenAI request failed")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(mask_secrets(f"OpenAI {exc.code}: {detail}")) from exc


def stream_claude(api_key: str, messages: List[Dict[str, Union[str, List[Dict[str, str]]]]]) -> Generator[str, None, None]:
    def content_parts(msg: Dict[str, Union[str, List[Dict[str, str]]]]) -> Union[str, List[Dict[str, Any]]]:
        images = msg.get("images") or []
        if msg["role"] != "user" or not images:
            return str(msg["content"])
        parts: List[Dict[str, Any]] = [{"type": "text", "text": str(msg["content"])}]
        for image in images:
            if isinstance(image, dict) and image.get("data"):
                parts.append(
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": image.get("type") or "image/png",
                            "data": image["data"],
                        },
                    }
                )
        return parts

    body = {
        "model": DEFAULT_CLAUDE_MODEL,
        "max_tokens": 4096,
        "stream": True,
        "messages": [
            {"role": msg["role"], "content": content_parts(msg)}
            for msg in messages
            if msg["role"] in {"user", "assistant"}
        ],
    }
    req = urllib.request.Request(
        f"{CLAUDE_BASE_URL}/v1/messages",
        data=json_dumps(body),
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            for raw in resp:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data: "):
                    continue
                try:
                    event = json.loads(line[6:])
                except json.JSONDecodeError:
                    continue
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield delta.get("text", "")
                elif event.get("type") == "message_stop":
                    break
                elif event.get("type") == "error":
                    raise RuntimeError(event.get("error", {}).get("message") or "Claude request failed")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(mask_secrets(f"Claude {exc.code}: {detail}")) from exc


def image_from_response(data: Dict[str, Any]) -> str:
    first = (data.get("data") or [{}])[0]
    if first.get("url"):
        return first["url"]
    if first.get("b64_json"):
        return "data:image/png;base64," + first["b64_json"]
    raise RuntimeError("OpenAI image response did not include an image")


def save_reference_images(images: List[Dict[str, str]]) -> List[str]:
    saved_urls: List[str] = []
    for image in images[:4]:
        content_type = (image.get("type") or "image/png").split(";", 1)[0].lower()
        extension = IMAGE_EXTENSIONS.get(content_type)
        raw_data = image.get("data") or ""
        if not extension or not raw_data:
            continue
        try:
            decoded = base64.b64decode(raw_data, validate=True)
        except (ValueError, base64.binascii.Error):
            continue
        if len(decoded) > MAX_UPLOAD_IMAGE_BYTES:
            raise RuntimeError("上传图片过大，请压缩后再试")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{make_id('upload')}{extension}"
        target = UPLOAD_DIR / filename
        target.write_bytes(decoded)
        saved_urls.append(f"/uploads/{filename}")
    return saved_urls


def image_reference_markdown(urls: List[str]) -> str:
    if not urls:
        return ""
    lines = ["", "", "参考图："]
    lines.extend(f"![参考图 {index + 1}]({url})" for index, url in enumerate(urls))
    return "\n".join(lines)


def normalize_image_quality(value: Optional[str]) -> str:
    return value if value in {"auto", "low", "medium", "high"} else "auto"


def generate_openai_image(api_key: str, prompt: str, images: List[Dict[str, str]], quality: str = "auto") -> str:
    fields = {"model": DEFAULT_IMAGE_MODEL, "prompt": prompt, "size": "1024x1024", "quality": normalize_image_quality(quality)}
    if images:
        data = request_multipart(
            f"{OPENAI_BASE_URL}/v1/images/edits",
            {"Authorization": f"Bearer {api_key}"},
            fields,
            [
                {
                    "field": "image[]",
                    "filename": image.get("name") or f"reference-{index + 1}.png",
                    "contentType": image.get("type") or "image/png",
                    "base64": image["data"],
                }
                for index, image in enumerate(images)
                if image.get("data")
            ],
            timeout=IMAGE_REQUEST_TIMEOUT,
        )
        return image_from_response(data)
    data = request_json(
        f"{OPENAI_BASE_URL}/v1/images/generations",
        {"Authorization": f"Bearer {api_key}"},
        fields,
        timeout=IMAGE_REQUEST_TIMEOUT,
    )
    return image_from_response(data)


class Handler(BaseHTTPRequestHandler):
    server_version = "EasyChat/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        print(f"{self.address_string()} - {fmt % args}")

    def device_id(self) -> Optional[str]:
        return verify_device_token(self.headers.get("X-Device-Token"))

    def current_user(self) -> Optional[sqlite3.Row]:
        device_id = self.device_id()
        return get_user(device_id) if device_id else None

    def read_json(self) -> Dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def send_json(self, data: Any, status: int = 200) -> None:
        payload = json_dumps(data)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def send_error_json(self, message: str, status: int = 400) -> None:
        self.send_json({"error": message}, status)

    def require_user(self) -> Optional[sqlite3.Row]:
        if not self.device_id():
            self.send_error_json("Invalid device token", HTTPStatus.UNAUTHORIZED)
            return None
        user = self.current_user()
        if not user:
            self.send_error_json("请先配置 API Key", HTTPStatus.UNAUTHORIZED)
            return None
        return user

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path.startswith("/api/"):
            self.handle_api_get(parsed.path)
            return
        if parsed.path.startswith("/uploads/"):
            self.serve_upload(parsed.path)
            return
        self.serve_static(parsed.path)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_error_json("Not found", HTTPStatus.NOT_FOUND)
            return
        try:
            self.handle_api_post(parsed.path)
        except json.JSONDecodeError:
            self.send_error_json("Invalid JSON", HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self.send_error_json(mask_secrets(str(exc)), HTTPStatus.INTERNAL_SERVER_ERROR)

    def serve_static(self, url_path: str) -> None:
        if not DIST_DIR.exists():
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<!doctype html><meta charset='utf-8'><title>EasyChat</title>"
                b"<body style='font-family: system-ui; padding: 40px'>"
                b"<h1>EasyChat frontend is not built</h1>"
                b"<p>Run <code>cd frontend && npm install && npm run build</code>.</p></body>"
            )
            return
        relative = url_path.lstrip("/") or "index.html"
        candidate = (DIST_DIR / relative).resolve()
        if DIST_DIR not in candidate.parents and candidate != DIST_DIR:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not candidate.is_file():
            candidate = DIST_DIR / "index.html"
        data = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", MIME_TYPES.get(candidate.suffix, "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def serve_upload(self, url_path: str) -> None:
        relative = url_path.removeprefix("/uploads/").lstrip("/")
        candidate = (UPLOAD_DIR / relative).resolve()
        if not UPLOAD_DIR.exists() or (UPLOAD_DIR not in candidate.parents and candidate != UPLOAD_DIR) or not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        data = candidate.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", MIME_TYPES.get(candidate.suffix.lower(), "application/octet-stream"))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=31536000, immutable")
        self.end_headers()
        self.wfile.write(data)

    def handle_api_get(self, path: str) -> None:
        if path == "/api/device":
            self.send_json({"deviceToken": issue_device_token()})
            return

        if path == "/api/me":
            if not self.device_id():
                self.send_error_json("Invalid device token", HTTPStatus.UNAUTHORIZED)
                return
            user = self.current_user()
            self.send_json(
                {
                    "configured": bool(user),
                    "provider": user["provider"] if user else None,
                    "macAddress": user["mac_address"] if user else detect_mac(self.client_address[0]),
                    "models": {
                        "openai": DEFAULT_OPENAI_MODEL,
                        "claude": DEFAULT_CLAUDE_MODEL,
                        "image": DEFAULT_IMAGE_MODEL,
                    },
                }
            )
            return

        user = self.require_user()
        if not user:
            return

        if path == "/api/conversations":
            self.send_json({"conversations": list_conversations(user["id"])})
            return

        if path.startswith("/api/conversations/"):
            conversation_id = path.rsplit("/", 1)[-1]
            with db() as conn:
                conversation = conn.execute(
                    "SELECT * FROM conversations WHERE id=? AND user_id=?",
                    (conversation_id, user["id"]),
                ).fetchone()
            if not conversation:
                self.send_error_json("Conversation not found", HTTPStatus.NOT_FOUND)
                return
            self.send_json(
                {
                    "conversation": {
                        "id": conversation["id"],
                        "title": conversation["title"],
                        "provider": conversation["provider"],
                        "model": conversation["model"],
                    },
                    "messages": load_messages(conversation_id),
                }
            )
            return

        self.send_error_json("Not found", HTTPStatus.NOT_FOUND)

    def handle_api_post(self, path: str) -> None:
        if path == "/api/setup":
            device_id = self.device_id()
            if not device_id:
                self.send_error_json("Invalid device token", HTTPStatus.UNAUTHORIZED)
                return
            payload = self.read_json()
            provider = (payload.get("provider") or "").lower()
            api_key = (payload.get("apiKey") or "").strip()
            if provider not in {"openai", "claude"}:
                self.send_error_json("Provider must be openai or claude", HTTPStatus.BAD_REQUEST)
                return
            if not api_key:
                self.send_error_json("请粘贴 API Key", HTTPStatus.BAD_REQUEST)
                return
            ts = now()
            mac = detect_mac(self.client_address[0])
            with db() as conn:
                conn.execute(
                    """
                    INSERT INTO users(device_id, mac_address, provider, api_key, created_at, updated_at)
                    VALUES(?,?,?,?,?,?)
                    ON CONFLICT(device_id) DO UPDATE SET
                        mac_address=excluded.mac_address,
                        provider=excluded.provider,
                        api_key=excluded.api_key,
                        updated_at=excluded.updated_at
                    """,
                    (device_id, mac, provider, api_key, ts, ts),
                )
            self.send_json({"ok": True, "provider": provider, "macAddress": mac})
            return

        user = self.require_user()
        if not user:
            return

        if path == "/api/conversations":
            payload = self.read_json()
            title = (payload.get("title") or "新会话").strip()[:80]
            conversation_id = create_conversation(user["id"], user["provider"], title)
            conversations = list_conversations(user["id"])
            current = next(item for item in conversations if item["id"] == conversation_id)
            self.send_json(current)
            return

        if path.startswith("/api/conversations/") and path.endswith("/title"):
            conversation_id = path.split("/")[-2]
            payload = self.read_json()
            updated = update_conversation_title(user["id"], conversation_id, payload.get("title") or "")
            if not updated:
                self.send_error_json("Conversation not found", HTTPStatus.NOT_FOUND)
                return
            self.send_json(updated)
            return

        if path.startswith("/api/conversations/") and path.endswith("/delete"):
            conversation_id = path.split("/")[-2]
            if not delete_conversation(user["id"], conversation_id):
                self.send_error_json("Conversation not found", HTTPStatus.NOT_FOUND)
                return
            self.send_json({"ok": True})
            return

        if path == "/api/chat":
            self.handle_chat(user)
            return

        if path == "/api/image":
            self.handle_image(user)
            return

        self.send_error_json("Not found", HTTPStatus.NOT_FOUND)

    def sse_headers(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()

    def sse(self, event: str, data: Any) -> None:
        self.wfile.write(f"event: {event}\n".encode("utf-8"))
        self.wfile.write(b"data: ")
        self.wfile.write(json_dumps(data))
        self.wfile.write(b"\n\n")
        self.wfile.flush()

    def handle_chat(self, user: sqlite3.Row) -> None:
        payload = self.read_json()
        text = (payload.get("message") or "").strip()
        images = payload.get("images") or []
        conversation_id = payload.get("conversationId")
        if not text:
            self.send_error_json("Message is required", HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(images, list):
            self.send_error_json("Invalid image payload", HTTPStatus.BAD_REQUEST)
            return
        conversation_id = ensure_conversation(user, conversation_id, text)
        reference_urls = save_reference_images(images)
        persist_message(conversation_id, "user", f"{text}{image_reference_markdown(reference_urls)}")
        self.sse_headers()
        self.sse(
            "meta",
            {
                "conversationId": conversation_id,
                "provider": user["provider"],
                "model": DEFAULT_OPENAI_MODEL if user["provider"] == "openai" else DEFAULT_CLAUDE_MODEL,
                "title": title_from_prompt(text),
            },
        )
        chunks: List[str] = []
        try:
            history = history_for_provider(conversation_id)
            stream = stream_openai(user["api_key"], history) if user["provider"] == "openai" else stream_claude(user["api_key"], history)
            for chunk in stream:
                if chunk:
                    chunks.append(chunk)
                    self.sse("delta", {"text": chunk})
            answer = "".join(chunks).strip()
            persist_message(conversation_id, "assistant", answer)
            self.sse("done", {"ok": True})
        except Exception as exc:
            self.sse("error", {"message": mask_secrets(str(exc))})

    def handle_image(self, user: sqlite3.Row) -> None:
        payload = self.read_json()
        prompt = (payload.get("prompt") or "").strip()
        images = payload.get("images") or []
        quality = normalize_image_quality(payload.get("quality"))
        conversation_id = payload.get("conversationId")
        if user["provider"] != "openai":
            self.send_error_json("Claude 模式不支持生图，请切换到 OpenAI Key", HTTPStatus.BAD_REQUEST)
            return
        if not prompt:
            self.send_error_json("请输入图片描述", HTTPStatus.BAD_REQUEST)
            return
        if not isinstance(images, list):
            self.send_error_json("Invalid image payload", HTTPStatus.BAD_REQUEST)
            return
        conversation_id = ensure_conversation(user, conversation_id, f"生成图片：{prompt}")
        reference_urls = save_reference_images(images)
        user_content = f"生成图片：{prompt}{image_reference_markdown(reference_urls)}"
        persist_message(conversation_id, "user", user_content)
        image_url = generate_openai_image(user["api_key"], prompt, images, quality)
        persist_message(conversation_id, "assistant", "图片已生成", image_url)
        self.send_json({"conversationId": conversation_id, "imageUrl": image_url, "referenceUrls": reference_urls})


if __name__ == "__main__":
    init_db()
    httpd = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"EasyChat API running at http://{HOST}:{PORT}")
    print(f"SQLite DB: {DB_PATH}")
    httpd.serve_forever()
