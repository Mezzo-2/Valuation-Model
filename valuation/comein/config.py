from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from valuation.comein.errors import McpConfigError

COMEIN_SERVER = "comein-mcp-all"


def _env(*keys: str) -> str:
    for key in keys:
        value = (os.environ.get(key) or "").strip()
        if value:
            return value
    return ""


@dataclass(frozen=True)
class ServerConfig:
    name: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    transport: str = "sse"


def default_server_name() -> str:
    return _env("VALUATION_MCP_SERVER", "SPIKE_MCP_SERVER") or COMEIN_SERVER


def config_path() -> Path:
    raw = _env("VALUATION_MCP_CONFIG", "SPIKE_MCP_CONFIG")
    if raw:
        return Path(raw).expanduser()
    return Path.home() / ".cursor" / "mcp.json"


def load_server(name: str | None = None) -> ServerConfig:
    name = (name or default_server_name()).strip()
    env = _from_env(name)
    if env is not None:
        return env
    return _from_cursor_mcp_json(name)


def _from_env(name: str) -> ServerConfig | None:
    url = _env("VALUATION_MCP_URL", "SPIKE_MCP_URL")
    if not url:
        return None
    headers: dict[str, str] = {}
    key = _env("VALUATION_MCP_KEY", "SPIKE_MCP_KEY", "COMEIN_MCP_KEY")
    if key:
        headers[_env("VALUATION_MCP_KEY_HEADER", "SPIKE_MCP_KEY_HEADER") or "x-mcp-key"] = key
    auth = _env("VALUATION_MCP_AUTH", "SPIKE_MCP_AUTH")
    if auth:
        headers["Authorization"] = auth
    transport = _env("VALUATION_MCP_TRANSPORT", "SPIKE_MCP_TRANSPORT") or _infer_transport(url, "")
    return ServerConfig(name=name, url=url, headers=headers, transport=transport)


def _from_cursor_mcp_json(name: str) -> ServerConfig:
    path = config_path()
    if not path.is_file():
        raise McpConfigError(
            f"找不到 MCP 配置 {path}。可设 VALUATION_MCP_URL 与 VALUATION_MCP_KEY，"
            f"或把 Cursor 的 mcp.json 放到默认位置。"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise McpConfigError(f"MCP 配置不是合法 JSON: {path}") from exc
    servers = data.get("mcpServers") or {}
    if name not in servers:
        known = ", ".join(sorted(servers)) or "(空)"
        raise McpConfigError(f"mcp.json 里没有服务 {name!r}。已有：{known}")
    entry = servers[name]
    if not isinstance(entry, dict):
        raise McpConfigError(f"服务 {name!r} 配置不是对象")
    url = str(entry.get("url") or "").strip()
    if not url:
        raise McpConfigError(f"服务 {name!r} 没有 url（stdio 服务不在本客户端范围内）")
    headers = _as_str_dict(entry.get("headers"))
    transport = str(entry.get("transport") or entry.get("type") or "").strip()
    transport = _infer_transport(url, transport)
    return ServerConfig(name=name, url=url, headers=headers, transport=transport)


def _infer_transport(url: str, declared: str) -> str:
    token = declared.lower()
    if token in {"sse", "http", "streamable-http", "streamable_http"}:
        return "sse" if token == "sse" else "http"
    if url.rstrip("/").endswith("/sse"):
        return "sse"
    return "http"


def _as_str_dict(raw: Any) -> dict[str, str]:
    if not raw:
        return {}
    if not isinstance(raw, dict):
        raise McpConfigError("headers 必须是对象")
    return {str(k): str(v) for k, v in raw.items() if v is not None}
