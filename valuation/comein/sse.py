from __future__ import annotations

import json
import threading
from concurrent.futures import Future
from itertools import count
from typing import Any
from urllib.parse import urlparse

import httpx

from valuation.comein.errors import McpTransportError

_CLIENT_INFO = {"name": "valuation-comein", "version": "0.1.0"}
_PROTOCOL = "2024-11-05"


class SseSession:
    """经典 MCP SSE：GET /sse 收消息，POST endpoint 发 JSON-RPC。"""

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
        self._lock = threading.Lock()
        self._pending: dict[int, Future[dict[str, Any]]] = {}
        self._endpoint: str | None = None
        self._endpoint_ready = threading.Event()
        self._closed = False
        self._reader_error: BaseException | None = None
        self._stream_client: httpx.Client | None = None
        self._post_client: httpx.Client | None = None
        self._stream_cm: Any = None
        self._thread: threading.Thread | None = None

    def start(self) -> dict[str, Any]:
        timeout = httpx.Timeout(self._connect_timeout, read=None)
        post_timeout = httpx.Timeout(self._connect_timeout, read=self._call_timeout)
        self._stream_client = httpx.Client(timeout=timeout, follow_redirects=True)
        self._post_client = httpx.Client(timeout=post_timeout, follow_redirects=True)
        self._stream_cm = self._stream_client.stream(
            "GET",
            self._url,
            headers={**self._headers, "Accept": "text/event-stream"},
        )
        resp = self._stream_cm.__enter__()
        if resp.status_code != 200:
            self.close()
            raise McpTransportError(f"SSE GET {resp.status_code}")
        self._thread = threading.Thread(target=self._read_loop, args=(resp,), daemon=True)
        self._thread.start()
        if not self._endpoint_ready.wait(self._connect_timeout):
            self.close()
            raise McpTransportError("SSE 未返回 endpoint 事件")
        init = self.rpc(
            "initialize",
            {
                "protocolVersion": _PROTOCOL,
                "capabilities": {},
                "clientInfo": _CLIENT_INFO,
            },
            timeout=self._connect_timeout,
        )
        self.notify("notifications/initialized")
        return init

    def close(self) -> None:
        self._closed = True
        if self._stream_cm is not None:
            try:
                self._stream_cm.__exit__(None, None, None)
            except Exception:
                pass
            self._stream_cm = None
        if self._stream_client is not None:
            self._stream_client.close()
            self._stream_client = None
        if self._post_client is not None:
            self._post_client.close()
            self._post_client = None
        with self._lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for fut in pending:
            if not fut.done():
                fut.set_exception(McpTransportError("MCP 会话已关闭"))

    def rpc(self, method: str, params: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        req_id = next(self._ids)
        payload: dict[str, Any] = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            payload["params"] = params
        fut: Future[dict[str, Any]] = Future()
        with self._lock:
            self._pending[req_id] = fut
        try:
            http_msg = self._post(payload)
        except Exception as exc:
            with self._lock:
                self._pending.pop(req_id, None)
            raise McpTransportError(f"POST {method} 失败: {exc}") from exc
        if http_msg is not None and http_msg.get("id") == req_id:
            with self._lock:
                self._pending.pop(req_id, None)
            return _rpc_result(http_msg, method)
        try:
            msg = fut.result(timeout if timeout is not None else self._call_timeout)
        except Exception as exc:
            with self._lock:
                self._pending.pop(req_id, None)
            if self._reader_error is not None:
                raise McpTransportError(f"{method}: {self._reader_error}") from self._reader_error
            raise McpTransportError(f"{method} 等待响应超时") from exc
        return _rpc_result(msg, method)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        payload: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self._post(payload)

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if self._closed or self._post_client is None or not self._endpoint:
            raise McpTransportError("MCP 会话未打开")
        resp = self._post_client.post(
            self._endpoint,
            headers={
                **self._headers,
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
            },
            json=payload,
        )
        if resp.status_code >= 400:
            raise McpTransportError(f"POST {payload.get('method')} HTTP {resp.status_code}")
        if not resp.content:
            return None
        ctype = (resp.headers.get("content-type") or "").lower()
        if "json" not in ctype:
            return None
        try:
            body = resp.json()
        except json.JSONDecodeError:
            return None
        return body if isinstance(body, dict) else None

    def _read_loop(self, resp: httpx.Response) -> None:
        try:
            for event, data in _iter_sse(resp):
                if self._closed:
                    return
                if event == "endpoint":
                    self._endpoint = _resolve_endpoint(self._url, data)
                    self._endpoint_ready.set()
                    continue
                if event not in {"message", "data"}:
                    continue
                try:
                    msg = json.loads(data)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                self._dispatch(msg)
        except Exception as exc:
            self._reader_error = exc
            with self._lock:
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(McpTransportError(str(exc)))
                self._pending.clear()
        finally:
            self._endpoint_ready.set()

    def _dispatch(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        if method and "id" in msg:
            if method == "ping":
                try:
                    self._post({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
                except Exception:
                    pass
            return
        req_id = msg.get("id")
        if not isinstance(req_id, int):
            return
        with self._lock:
            fut = self._pending.pop(req_id, None)
        if fut is not None and not fut.done():
            fut.set_result(msg)


def _rpc_result(msg: dict[str, Any], method: str) -> Any:
    if "error" in msg and msg["error"] is not None:
        err = msg["error"]
        if isinstance(err, dict):
            raise McpTransportError(f"{method}: {err.get('message') or err}")
        raise McpTransportError(f"{method}: {err}")
    return msg.get("result")


def _resolve_endpoint(sse_url: str, data: str) -> str:
    data = data.strip()
    if data.startswith("http://") or data.startswith("https://"):
        return data
    parsed = urlparse(sse_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    if not data.startswith("/"):
        data = "/" + data
    return origin + data


def _iter_sse(resp: httpx.Response):
    event = "message"
    chunks: list[str] = []
    for line in resp.iter_lines():
        if line is None:
            continue
        if line == "":
            if chunks:
                yield event, "\n".join(chunks)
            event = "message"
            chunks = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event = line[6:].strip() or "message"
        elif line.startswith("data:"):
            chunks.append(line[5:].lstrip())
