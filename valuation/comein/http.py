from __future__ import annotations

import json
from itertools import count
from typing import Any

import httpx

from valuation.comein.errors import McpTransportError

_CLIENT_INFO = {"name": "valuation-comein", "version": "0.1.0"}
_PROTOCOL = "2024-11-05"


class StreamableHttpSession:
    """Streamable HTTP：每次 JSON-RPC POST 到同一 URL，带 Mcp-Session-Id。"""

    def __init__(
        self,
        url: str,
        headers: dict[str, str],
        *,
        connect_timeout: float = 30.0,
        call_timeout: float = 180.0,
    ) -> None:
        self._url = url
        self._headers = dict(headers)
        self._connect_timeout = connect_timeout
        self._call_timeout = call_timeout
        self._ids = count(1)
        self._session_id: str | None = None
        self._client: httpx.Client | None = None

    def start(self) -> dict[str, Any]:
        timeout = httpx.Timeout(self._connect_timeout, read=self._call_timeout)
        self._client = httpx.Client(timeout=timeout, follow_redirects=True)
        result = self.rpc(
            "initialize",
            {
                "protocolVersion": _PROTOCOL,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
        )
        self.notify("notifications/initialized")
        return result

    def close(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def rpc(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        req_id = next(self._ids)
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        msg = self._post(payload, timeout=timeout)
        if "error" in msg and msg["error"] is not None:
            err = msg["error"]
            if isinstance(err, dict):
                raise McpTransportError(f"{method}: {err.get('message') or err}")
            raise McpTransportError(f"{method}: {err}")
        return msg.get("result")

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload)

    def _post(self, payload: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        if self._client is None:
            raise McpTransportError("MCP 会话未打开")
        headers = {
            **self._headers,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        kwargs: dict[str, Any] = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        resp = self._client.post(self._url, headers=headers, json=payload, **kwargs)
        sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
        if sid:
            self._session_id = sid
        if resp.status_code >= 400:
            raise McpTransportError(f"POST {payload.get('method')} HTTP {resp.status_code}")
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" in ctype:
            return _first_sse_json(resp, payload.get("id"))
        if not resp.content:
            return {}
        try:
            body = resp.json()
        except json.JSONDecodeError as exc:
            raise McpTransportError(f"HTTP 响应不是 JSON: {exc}") from exc
        if not isinstance(body, dict):
            raise McpTransportError("HTTP 响应不是 JSON 对象")
        return body


def _first_sse_json(resp: httpx.Response, req_id: Any = None) -> dict[str, Any]:
    event = "message"
    chunks: list[str] = []
    for line in resp.iter_lines():
        if line is None:
            continue
        if line == "":
            if chunks:
                data = "\n".join(chunks)
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    event = "message"
                    chunks = []
                    continue
                if isinstance(msg, dict) and _sse_is_result(msg, req_id):
                    return msg
            event = "message"
            chunks = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip() or "message"
        elif line.startswith("data:"):
            chunks.append(line[5:].lstrip())
        _ = event
    raise McpTransportError("Streamable HTTP SSE 没有 JSON 消息")


def _sse_is_result(msg: dict[str, Any], req_id: Any) -> bool:
    method = str(msg.get("method") or "")
    if method.startswith("notifications/"):
        return False
    if req_id is not None and "id" in msg and msg.get("id") != req_id:
        return False
    return True
