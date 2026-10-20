"""Small dependency-free HTTP client for the OpenViking data plane."""

from __future__ import annotations

import json
import mimetypes
import os
import urllib.error
import urllib.parse
import urllib.request
import uuid
from dataclasses import dataclass
from typing import Any, Callable


class OpenVikingClientError(RuntimeError):
    """Raised when OpenViking rejects or cannot complete a request."""

    def __init__(
        self,
        message: str,
        *,
        http_status: int | None = None,
        response_text: str = "",
    ):
        super().__init__(message)
        self.http_status = http_status
        self.response_text = str(response_text or "")


@dataclass(frozen=True)
class OpenVikingIdentity:
    """Trusted-mode identity injected by the Memory Gateway."""

    account_id: str
    user_id: str


class OpenVikingHTTPClient:
    """Call OpenViking without adding third-party packages to the bridge host."""

    def __init__(
        self,
        base_url: str,
        *,
        api_key: str = "",
        timeout: int = 30,
        opener: Callable[..., Any] | None = None,
    ):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.api_key = str(api_key or "").strip()
        self.timeout = max(int(timeout or 30), 1)
        self._opener = opener or urllib.request.urlopen
        if not self.base_url:
            raise OpenVikingClientError("OpenViking base URL is not configured")

    def request(
        self,
        method: str,
        path: str,
        *,
        identity: OpenVikingIdentity,
        query: dict[str, Any] | None = None,
        payload: dict[str, Any] | None = None,
        raw_body: bytes | None = None,
        content_type: str = "",
    ) -> Any:
        url = f"{self.base_url}/{str(path or '').lstrip('/')}"
        if query:
            encoded_query = urllib.parse.urlencode(
                {key: value for key, value in query.items() if value is not None}
            )
            if encoded_query:
                url = f"{url}?{encoded_query}"

        if payload is not None and raw_body is not None:
            raise OpenVikingClientError("payload and raw_body are mutually exclusive")
        body = raw_body
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(url, data=body, method=method.upper())
        request.add_header("Accept", "application/json")
        if body is not None:
            request.add_header("Content-Type", content_type or "application/json")
        if self.api_key:
            request.add_header("X-API-Key", self.api_key)
        request.add_header("X-OpenViking-Account", identity.account_id)
        request.add_header("X-OpenViking-User", identity.user_id)
        request.add_header("X-OpenViking-Actor-Peer", identity.user_id)
        request.add_header("X-OpenViking-Role", "user")

        try:
            with self._opener(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise OpenVikingClientError(
                f"OpenViking request failed with HTTP {exc.code}: {detail[:500]}",
                http_status=exc.code,
                response_text=detail,
            ) from exc
        except urllib.error.URLError as exc:
            raise OpenVikingClientError(f"OpenViking is unavailable: {exc.reason}") from exc

        if not raw:
            return {}
        try:
            envelope = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OpenVikingClientError("OpenViking returned invalid JSON") from exc
        if not isinstance(envelope, dict):
            raise OpenVikingClientError("OpenViking returned an invalid response envelope")
        if str(envelope.get("status") or "ok").lower() not in {"ok", "success", "accepted"}:
            message = envelope.get("message") or envelope.get("error") or "request rejected"
            raise OpenVikingClientError(f"OpenViking rejected the request: {message}")
        return envelope.get("result", envelope)

    def temp_upload(
        self,
        identity: OpenVikingIdentity,
        file_path: str,
        *,
        file_name: str = "",
        mime_type: str = "",
    ) -> Any:
        """Upload one trusted local file and return its temporary import id."""

        resolved_path = os.fspath(file_path)
        upload_name = str(file_name or os.path.basename(resolved_path) or "resource.bin")
        safe_name = "".join(
            ch if ch.isascii() and (ch.isalnum() or ch in {"-", "_", "."}) else "-"
            for ch in upload_name
        ).strip(".-") or "resource.bin"
        resolved_mime = str(mime_type or mimetypes.guess_type(upload_name)[0] or "application/octet-stream")
        boundary = f"----openviking-{uuid.uuid4().hex}"
        with open(resolved_path, "rb") as source_file:
            file_content = source_file.read()
        prefix = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{safe_name}"\r\n'
            f"Content-Type: {resolved_mime}\r\n\r\n"
        ).encode("ascii")
        body = prefix + file_content + f"\r\n--{boundary}--\r\n".encode("ascii")
        return self.request(
            "POST",
            "/api/v1/resources/temp_upload",
            identity=identity,
            raw_body=body,
            content_type=f"multipart/form-data; boundary={boundary}",
        )

    def add_resource(
        self,
        identity: OpenVikingIdentity,
        *,
        temp_file_id: str,
        target_uri: str,
        source_name: str = "",
        wait: bool = False,
    ) -> Any:
        return self.request(
            "POST",
            "/api/v1/resources",
            identity=identity,
            payload={
                "temp_file_id": temp_file_id,
                "to": target_uri,
                "create_parent": True,
                "source_name": source_name or None,
                "reason": "Transparent Company knowledge synchronization",
                "instruction": "Preserve factual content and source structure; generate searchable summaries.",
                "wait": bool(wait),
                "processing_mode": "semantic_and_vectors",
                "tags": ["touming=knowledge"],
            },
        )

    def move(self, identity: OpenVikingIdentity, *, from_uri: str, to_uri: str) -> Any:
        return self.request(
            "POST",
            "/api/v1/fs/mv",
            identity=identity,
            payload={"from_uri": from_uri, "to_uri": to_uri},
        )

    def delete_uri(self, identity: OpenVikingIdentity, uri: str, *, recursive: bool = False) -> Any:
        return self.request(
            "DELETE",
            "/api/v1/fs",
            identity=identity,
            query={"uri": uri, "recursive": "true" if recursive else "false"},
        )

    def recall(
        self,
        identity: OpenVikingIdentity,
        *,
        query: str,
        max_chars: int = 5000,
    ) -> Any:
        return self.request(
            "POST",
            "/api/v1/search/recall",
            identity=identity,
            payload={
                "query": query,
                "max_chars": max(500, min(int(max_chars or 5000), 12000)),
                "peer_scope": "actor",
                "render": True,
            },
        )

    def ensure_session(self, identity: OpenVikingIdentity, session_id: str) -> Any:
        encoded_id = urllib.parse.quote(str(session_id or ""), safe="")
        return self.request(
            "GET",
            f"/api/v1/sessions/{encoded_id}",
            identity=identity,
            query={"auto_create": "true"},
        )

    def add_session_messages(
        self,
        identity: OpenVikingIdentity,
        session_id: str,
        messages: list[dict[str, Any]],
    ) -> Any:
        encoded_id = urllib.parse.quote(str(session_id or ""), safe="")
        return self.request(
            "POST",
            f"/api/v1/sessions/{encoded_id}/messages/batch",
            identity=identity,
            payload={"messages": messages},
        )

    def commit_session(
        self,
        identity: OpenVikingIdentity,
        session_id: str,
        *,
        keep_recent_count: int = 10,
    ) -> Any:
        encoded_id = urllib.parse.quote(str(session_id or ""), safe="")
        return self.request(
            "POST",
            f"/api/v1/sessions/{encoded_id}/commit",
            identity=identity,
            payload={"keep_recent_count": max(0, min(int(keep_recent_count or 0), 100))},
        )

    def ensure_memory_root(self, identity: OpenVikingIdentity, root_uri: str) -> Any:
        return self.request(
            "POST",
            "/api/v1/fs/mkdir",
            identity=identity,
            payload={"uri": root_uri, "description": "Agent-owned long-term memory space"},
        )

    def list_directory(self, identity: OpenVikingIdentity, uri: str) -> Any:
        """List one OpenViking directory using the documented filesystem API."""

        return self.request(
            "GET",
            "/api/v1/fs/ls",
            identity=identity,
            query={"uri": uri},
        )

    def find_memories(
        self,
        identity: OpenVikingIdentity,
        *,
        root_uri: str,
        query: str,
        limit: int,
    ) -> Any:
        return self.request(
            "POST",
            "/api/v1/search/find",
            identity=identity,
            payload={
                "query": query,
                "target_uri": root_uri,
                "context_type": ["memory"],
                "limit": limit,
            },
        )

    def find_resources(
        self,
        identity: OpenVikingIdentity,
        *,
        root_uri: str,
        query: str,
        limit: int,
    ) -> Any:
        """Search one server-selected resource scope.

        Callers must resolve ``root_uri`` from control-plane permissions; model
        supplied Viking URIs must never be forwarded to this method.
        """

        return self.request(
            "POST",
            "/api/v1/search/find",
            identity=identity,
            payload={
                "query": query,
                "target_uri": root_uri,
                "context_type": ["resource"],
                "limit": limit,
            },
        )

    def read(self, identity: OpenVikingIdentity, uri: str) -> Any:
        return self.request(
            "GET",
            "/api/v1/content/read",
            identity=identity,
            query={"uri": uri},
        )

    def write(
        self,
        identity: OpenVikingIdentity,
        *,
        uri: str,
        content: str,
        mode: str,
    ) -> Any:
        return self.request(
            "POST",
            "/api/v1/content/write",
            identity=identity,
            payload={
                "uri": uri,
                "content": content,
                "mode": mode,
                "wait": True,
            },
        )

    def delete(self, identity: OpenVikingIdentity, uri: str) -> Any:
        return self.delete_uri(identity, uri, recursive=False)
