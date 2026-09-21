from __future__ import annotations

import json
from types import TracebackType
from typing import Any

from valuation.comein.config import ServerConfig, load_server
from valuation.comein.errors import McpToolError, McpTransportError
from valuation.comein.http import StreamableHttpSession
from valuation.comein.sse import SseSession


class McpClient:
    """同步 MCP 客户端。会话在 with 块内复用，离开即断开。"""

    def __init__(self, config: ServerConfig) -> None:
        self.config = config
        self._session: SseSession | StreamableHttpSession | None = None
        self.initialize_result: dict[str, Any] | None = None

    @classmethod
    def comein(cls) -> McpClient:
        return cls.connect()

    @classmethod
    def connect(cls, name: str | None = None) -> McpClient:
        return cls(load_server(name))

    def __enter__(self) -> McpClient:
        if self.config.transport == "sse":
            session: SseSession | StreamableHttpSession = SseSession(
                self.config.url, self.config.headers
            )
        else:
            session = StreamableHttpSession(self.config.url, self.config.headers)
        self.initialize_result = session.start()
        self._session = session
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None

    def list_tools(self) -> list[dict[str, Any]]:
        result = self._rpc("tools/list", {})
        tools = (result or {}).get("tools") if isinstance(result, dict) else None
        return list(tools or [])

    def call(self, name: str, arguments: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        args = dict(arguments or {})
        args.update(kwargs)
        result = self._rpc("tools/call", {"name": name, "arguments": args})
        return unwrap_tool_result(name, result)

    def _rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        if self._session is None:
            raise McpTransportError("McpClient 必须在 with 块内使用")
        return self._session.rpc(method, params)


def unwrap_tool_result(tool: str, result: Any) -> Any:
    if result is None:
        raise McpToolError(tool, "空结果")
    if not isinstance(result, dict):
        return result
    if result.get("isError") or result.get("is_error"):
        raise McpToolError(tool, _content_text(result) or "isError")
    structured = result.get("structuredContent")
    if structured is None:
        structured = result.get("structured_content")
    if structured is not None:
        return structured
    text = _content_text(result)
    if not text:
        return result
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def _content_text(result: dict[str, Any]) -> str:
    parts: list[str] = []
    for block in result.get("content") or []:
        if isinstance(block, dict) and block.get("text"):
            parts.append(str(block["text"]))
        elif hasattr(block, "text") and getattr(block, "text"):
            parts.append(str(block.text))
    return "\n".join(parts).strip()
