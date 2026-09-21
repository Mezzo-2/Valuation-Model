from __future__ import annotations


class McpError(RuntimeError):
    """MCP 客户端错误。"""


class McpConfigError(McpError):
    """找不到服务配置，或 URL / 鉴权缺失。"""


class McpTransportError(McpError):
    """握手、会话或 JSON-RPC 传输失败。"""


class McpToolError(McpError):
    """tools/call 返回 isError，或结果无法解析。"""

    def __init__(self, tool: str, message: str) -> None:
        self.tool = tool
        super().__init__(f"{tool}: {message}")
