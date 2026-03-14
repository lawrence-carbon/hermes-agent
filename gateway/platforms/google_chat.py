"""
Google Chat platform adapter for the Hermes gateway.

This adapter uses a Google Cloud service account (JSON key) to:
- poll configured spaces for new human messages
- dispatch normalized MessageEvent objects into the gateway pipeline
- send replies back to Google Chat spaces/threads

Environment variables:
    GOOGLECHAT_SERVICE_ACCOUNT       Service account JSON (inline) OR path to JSON file
    GOOGLECHAT_SERVICE_ACCOUNT_FILE  Optional path alias for service account JSON file
    GOOGLECHAT_SPACES                Comma-separated list of space IDs/names to poll
                                     (e.g. "spaces/AAAAabc123,spaces/BBBBdef456")
    GOOGLECHAT_HOME_CHANNEL          Default space for proactive delivery
    GOOGLECHAT_POLL_INTERVAL         Poll interval in seconds (default: 5)
    GOOGLECHAT_HTTP_TIMEOUT          HTTP timeout in seconds (default: 20)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, MessageEvent, MessageType, SendResult

try:
    import httpx
except ImportError:  # pragma: no cover - guarded by check_googlechat_requirements
    httpx = None  # type: ignore[assignment]

try:
    import jwt
except ImportError:  # pragma: no cover - guarded by check_googlechat_requirements
    jwt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

GOOGLECHAT_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLECHAT_API_BASE = "https://chat.googleapis.com/v1"
GOOGLECHAT_SCOPE = "https://www.googleapis.com/auth/chat.bot"
GOOGLECHAT_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:jwt-bearer"
MAX_MESSAGE_LENGTH = 4000


def check_googlechat_requirements() -> bool:
    """Check if Google Chat adapter dependencies are available."""
    return bool(httpx and jwt)


def _parse_csv(value: str) -> List[str]:
    """Parse a comma-separated string into trimmed non-empty entries."""
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def _normalize_space_name(space: str) -> str:
    """Normalize a Google Chat space name to the `spaces/...` resource form."""
    if not space:
        return ""
    s = str(space).strip()
    if not s:
        return ""
    if s.startswith("spaces/"):
        return s
    return f"spaces/{s}"


def _parse_spaces(value: str | List[str]) -> List[str]:
    """Parse + normalize space names from CSV string or list."""
    if isinstance(value, list):
        raw_spaces = [str(v).strip() for v in value if str(v).strip()]
    else:
        raw_spaces = _parse_csv(str(value or ""))
    seen = set()
    out = []
    for raw in raw_spaces:
        normalized = _normalize_space_name(raw)
        if normalized and normalized not in seen:
            seen.add(normalized)
            out.append(normalized)
    return out


def _parse_rfc3339(value: Optional[str]) -> Optional[datetime]:
    """Parse RFC3339 timestamp to timezone-aware datetime."""
    if not value:
        return None
    try:
        txt = value.strip()
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        dt = datetime.fromisoformat(txt)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _extract_space_from_message_name(message_name: str) -> Optional[str]:
    """Extract `spaces/...` from message resource name."""
    # Example: spaces/AAAA/messages/BBB
    if not message_name or not message_name.startswith("spaces/"):
        return None
    parts = message_name.split("/")
    if len(parts) >= 2:
        return f"{parts[0]}/{parts[1]}"
    return None


def _is_human_message(message: Dict[str, Any]) -> bool:
    """Return True when sender is human (not bot/system)."""
    sender = message.get("sender") or {}
    sender_type = str(sender.get("type", "")).upper()
    if sender_type == "HUMAN":
        return True
    if sender_type == "BOT":
        return False
    # Defensive fallback for payload variations.
    sender_name = str(sender.get("name", ""))
    if sender_name.startswith("users/"):
        return True
    return False


def _load_service_account(source: str) -> Dict[str, str]:
    """Load service account JSON from inline blob or file path."""
    if not source or not source.strip():
        raise ValueError("GOOGLECHAT_SERVICE_ACCOUNT is not configured")

    raw = source.strip()
    if raw.startswith("{"):
        data = json.loads(raw)
    else:
        path = Path(raw).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"Service account file not found: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))

    required = ("client_email", "private_key")
    missing = [k for k in required if not data.get(k)]
    if missing:
        raise ValueError(f"Service account JSON missing required fields: {', '.join(missing)}")

    private_key = str(data["private_key"]).replace("\\n", "\n")
    token_uri = str(data.get("token_uri") or GOOGLECHAT_TOKEN_URI)
    return {
        "client_email": str(data["client_email"]),
        "private_key": private_key,
        "token_uri": token_uri,
    }


class GoogleChatApiClient:
    """Small async Google Chat REST client with service-account auth."""

    def __init__(self, service_account_source: str, timeout: float = 20.0):
        self._service_account_source = service_account_source
        self._timeout = timeout
        self._service_account: Optional[Dict[str, str]] = None
        self._client: Optional["httpx.AsyncClient"] = None
        self._token: Optional[str] = None
        self._token_expiry_epoch: int = 0
        self._token_lock = asyncio.Lock()

    async def open(self) -> None:
        if not check_googlechat_requirements():
            raise RuntimeError("Google Chat dependencies missing: install httpx + PyJWT[crypto]")
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=self._timeout, follow_redirects=True)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _get_service_account(self) -> Dict[str, str]:
        if self._service_account is None:
            self._service_account = _load_service_account(self._service_account_source)
        return self._service_account

    async def _refresh_access_token(self) -> str:
        await self.open()
        assert self._client is not None

        sa = self._get_service_account()
        now = int(time.time())
        claims = {
            "iss": sa["client_email"],
            "scope": GOOGLECHAT_SCOPE,
            "aud": sa["token_uri"],
            "iat": now,
            "exp": now + 3600,
        }
        assertion = jwt.encode(claims, sa["private_key"], algorithm="RS256")

        response = await self._client.post(
            sa["token_uri"],
            data={
                "grant_type": GOOGLECHAT_GRANT_TYPE,
                "assertion": assertion,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if response.status_code >= 400:
            body = response.text[:500]
            raise RuntimeError(f"Google OAuth token exchange failed ({response.status_code}): {body}")

        payload = response.json()
        token = payload.get("access_token")
        expires_in = int(payload.get("expires_in", 3600))
        if not token:
            raise RuntimeError("Google OAuth token response missing access_token")

        self._token = str(token)
        # Refresh one minute early to avoid edge expirations.
        self._token_expiry_epoch = now + max(60, expires_in - 60)
        return self._token

    async def get_access_token(self, force_refresh: bool = False) -> str:
        async with self._token_lock:
            now = int(time.time())
            if not force_refresh and self._token and now < self._token_expiry_epoch:
                return self._token
            return await self._refresh_access_token()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        retry_on_401: bool = True,
    ) -> Dict[str, Any]:
        await self.open()
        assert self._client is not None

        token = await self.get_access_token()
        url = path if path.startswith("https://") else f"{GOOGLECHAT_API_BASE}/{path.lstrip('/')}"
        headers = {"Authorization": f"Bearer {token}"}
        if json_body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"

        response = await self._client.request(
            method=method,
            url=url,
            params=params,
            json=json_body,
            headers=headers,
        )

        if response.status_code == 401 and retry_on_401:
            await self.get_access_token(force_refresh=True)
            return await self._request(
                method,
                path,
                params=params,
                json_body=json_body,
                retry_on_401=False,
            )

        if response.status_code >= 400:
            body = response.text[:500]
            raise RuntimeError(
                f"Google Chat API {method.upper()} {path} failed ({response.status_code}): {body}"
            )

        if not response.text:
            return {}
        return response.json()

    async def get_space(self, space: str) -> Dict[str, Any]:
        return await self._request("GET", _normalize_space_name(space))

    async def list_messages(self, space: str, page_size: int = 100) -> List[Dict[str, Any]]:
        data = await self._request(
            "GET",
            f"{_normalize_space_name(space)}/messages",
            params={"pageSize": max(1, min(int(page_size), 1000))},
        )
        return data.get("messages", []) or []

    async def send_message(
        self,
        space: str,
        text: str,
        thread_name: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"text": text}
        params: Dict[str, Any] = {}
        if thread_name:
            payload["thread"] = {"name": str(thread_name)}
            params["messageReplyOption"] = "REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD"
        return await self._request(
            "POST",
            f"{_normalize_space_name(space)}/messages",
            params=params or None,
            json_body=payload,
        )

    async def edit_message(self, message_name: str, text: str) -> Dict[str, Any]:
        return await self._request(
            "PATCH",
            message_name,
            params={"updateMask": "text"},
            json_body={"text": text},
        )


class GoogleChatAdapter(BasePlatformAdapter):
    """Google Chat adapter based on polling selected spaces."""

    def __init__(self, config: PlatformConfig):
        super().__init__(config, Platform.GOOGLECHAT)

        service_account = (
            (config.token or "").strip()
            or os.getenv("GOOGLECHAT_SERVICE_ACCOUNT", "").strip()
            or os.getenv("GOOGLECHAT_SERVICE_ACCOUNT_FILE", "").strip()
        )
        raw_spaces: str | List[str] = config.extra.get("spaces") or os.getenv("GOOGLECHAT_SPACES", "")
        poll_spaces = _parse_spaces(raw_spaces)

        # If no explicit poll list is provided, fall back to home channel.
        home_space = (
            (config.home_channel.chat_id if config.home_channel else None)
            or os.getenv("GOOGLECHAT_HOME_CHANNEL", "")
        )
        if not poll_spaces and home_space:
            poll_spaces = [_normalize_space_name(home_space)]

        self._poll_spaces = poll_spaces
        self._poll_interval = max(float(os.getenv("GOOGLECHAT_POLL_INTERVAL", "5")), 1.0)
        self._max_seen = max(int(os.getenv("GOOGLECHAT_MAX_SEEN_MESSAGES", "5000")), 500)
        self._api = GoogleChatApiClient(
            service_account_source=service_account,
            timeout=float(os.getenv("GOOGLECHAT_HTTP_TIMEOUT", "20")),
        )

        self._space_cursor: Dict[str, datetime] = {}
        self._seen_ids: set[str] = set()
        self._seen_queue: deque[str] = deque()
        self._poll_task: Optional[asyncio.Task] = None

    async def connect(self) -> bool:
        if not self._poll_spaces:
            logger.warning("[%s] No GOOGLECHAT_SPACES configured; cannot receive messages.", self.name)
            return False

        try:
            await self._api.open()
            await self._api.get_access_token()
        except Exception as e:
            logger.error("[%s] Authentication failed: %s", self.name, e)
            return False

        validated_spaces: List[str] = []
        for space in self._poll_spaces:
            try:
                info = await self._api.get_space(space)
                validated_spaces.append(_normalize_space_name(info.get("name") or space))
            except Exception as e:
                logger.warning("[%s] Cannot access %s: %s", self.name, space, e)

        if not validated_spaces:
            logger.error("[%s] None of the configured spaces are accessible.", self.name)
            return False

        self._poll_spaces = validated_spaces
        now = datetime.now(timezone.utc)
        self._space_cursor = {space: now for space in self._poll_spaces}
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        print(f"[Google Chat] Connected. Polling {len(self._poll_spaces)} space(s).")
        return True

    async def disconnect(self) -> None:
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        await self._api.close()

    def _remember_message_id(self, message_id: str) -> None:
        if message_id in self._seen_ids:
            return
        if len(self._seen_queue) >= self._max_seen:
            oldest = self._seen_queue.popleft()
            self._seen_ids.discard(oldest)
        self._seen_queue.append(message_id)
        self._seen_ids.add(message_id)

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("[%s] Poll loop error: %s", self.name, e)
            await asyncio.sleep(self._poll_interval)

    async def _poll_once(self) -> None:
        for space in self._poll_spaces:
            await self._poll_space(space)

    async def _poll_space(self, space: str) -> None:
        messages = await self._api.list_messages(space, page_size=100)
        if not messages:
            return

        old_cursor = self._space_cursor.get(space, datetime.now(timezone.utc))
        new_cursor = old_cursor

        def _sort_key(msg: Dict[str, Any]) -> datetime:
            return _parse_rfc3339(msg.get("createTime")) or datetime.fromtimestamp(0, tz=timezone.utc)

        for message in sorted(messages, key=_sort_key):
            message_id = str(message.get("name") or "")
            if not message_id:
                continue

            created_at = _parse_rfc3339(message.get("createTime"))
            if created_at and created_at > new_cursor:
                new_cursor = created_at

            if message_id in self._seen_ids:
                continue
            self._remember_message_id(message_id)

            # Skip backlog from before startup cursor.
            if created_at and created_at <= old_cursor:
                continue

            if not _is_human_message(message):
                continue

            event = self._build_message_event(message, default_space=space)
            if event:
                await self.handle_message(event)

        self._space_cursor[space] = new_cursor

    def _build_message_event(self, message: Dict[str, Any], default_space: str) -> Optional[MessageEvent]:
        text = str(message.get("argumentText") or message.get("text") or "").strip()
        if not text:
            return None

        message_name = str(message.get("name") or "")
        space_info = message.get("space") or {}
        sender = message.get("sender") or {}

        space_name = _normalize_space_name(
            str(space_info.get("name") or _extract_space_from_message_name(message_name) or default_space)
        )
        if not space_name:
            return None

        space_type = str(space_info.get("spaceType") or space_info.get("type") or "").upper()
        chat_type = "dm" if space_type == "DM" else "group"
        thread_name = str((message.get("thread") or {}).get("name") or "").strip() or None
        thread_topic = thread_name.split("/threads/")[-1] if thread_name and "/threads/" in thread_name else None
        user_id = str(sender.get("name") or "")
        user_name = str(sender.get("displayName") or user_id or "Google Chat User")

        source = self.build_source(
            chat_id=space_name,
            chat_name=str(space_info.get("displayName") or space_name),
            chat_type=chat_type,
            user_id=user_id,
            user_name=user_name,
            thread_id=thread_name,
            chat_topic=thread_topic,
        )
        return MessageEvent(
            text=text,
            message_type=MessageType.COMMAND if text.startswith("/") else MessageType.TEXT,
            source=source,
            raw_message=message,
            message_id=message_name or None,
            reply_to_message_id=thread_name,
        )

    async def send(
        self,
        chat_id: str,
        content: str,
        reply_to: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> SendResult:
        del reply_to  # Google Chat replies are thread-based, handled via metadata.thread_id.

        if not content or not content.strip():
            return SendResult(success=False, error="Cannot send an empty message")

        space = _normalize_space_name(chat_id)
        if not space:
            return SendResult(success=False, error="Invalid Google Chat space ID")

        thread_name = None
        if metadata and metadata.get("thread_id"):
            thread_name = str(metadata["thread_id"]).strip()

        try:
            chunks = self.truncate_message(self.format_message(content), max_length=MAX_MESSAGE_LENGTH)
            last_message_id: Optional[str] = None

            for chunk in chunks:
                response = await self._api.send_message(space, chunk, thread_name=thread_name)
                last_message_id = response.get("name") or last_message_id
                if not thread_name:
                    thread_name = (response.get("thread") or {}).get("name")

            return SendResult(success=True, message_id=last_message_id)
        except Exception as e:
            logger.error("[%s] Send failed to %s: %s", self.name, space, e)
            return SendResult(success=False, error=str(e))

    async def edit_message(self, chat_id: str, message_id: str, content: str) -> SendResult:
        del chat_id  # message_id is already a full resource name in Google Chat.
        if not message_id:
            return SendResult(success=False, error="message_id is required")
        try:
            response = await self._api.edit_message(message_id, self.format_message(content))
            return SendResult(success=True, message_id=response.get("name") or message_id, raw_response=response)
        except Exception as e:
            return SendResult(success=False, error=str(e))

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        del chat_id, metadata
        # Google Chat API currently has no typing indicator endpoint.
        return None

    async def get_chat_info(self, chat_id: str) -> Dict[str, Any]:
        space = _normalize_space_name(chat_id)
        try:
            info = await self._api.get_space(space)
            raw_type = str(info.get("spaceType") or info.get("type") or "").upper()
            return {
                "name": info.get("displayName") or space,
                "type": "dm" if raw_type == "DM" else "group",
                "chat_id": space,
            }
        except Exception:
            return {
                "name": space,
                "type": "group",
                "chat_id": space,
            }

    def format_message(self, content: str) -> str:
        # Keep formatting conservative for widest compatibility in Google Chat.
        return content.replace("\r\n", "\n").strip()


async def send_googlechat_message_once(
    service_account_source: str,
    chat_id: str,
    content: str,
    thread_id: Optional[str] = None,
) -> Dict[str, Any]:
    """
    One-shot Google Chat sender for cron/send_message tool usage.

    This avoids requiring the long-running adapter process.
    """
    if not service_account_source:
        return {
            "error": (
                "Google Chat is not configured. Set GOOGLECHAT_SERVICE_ACCOUNT "
                "to a service account JSON blob or file path."
            )
        }

    client = GoogleChatApiClient(service_account_source=service_account_source)
    try:
        await client.open()
        response = await client.send_message(
            space=_normalize_space_name(chat_id),
            text=content,
            thread_name=thread_id,
        )
        return {
            "success": True,
            "platform": "googlechat",
            "chat_id": _normalize_space_name(chat_id),
            "message_id": response.get("name"),
            "thread_id": (response.get("thread") or {}).get("name") or thread_id,
        }
    except Exception as e:
        return {"error": f"Google Chat send failed: {e}"}
    finally:
        await client.close()
