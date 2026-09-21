from __future__ import annotations

import argparse
import json
import sys

from valuation.comein.client import McpClient
from valuation.comein.config import default_server_name, load_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Comein MCP 客户端")
    parser.add_argument("--server", default="", help="mcp.json 里的服务名，默认 comein-mcp-all")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("ping", help="握手并列出工具名（不拉行情）")
    tools = sub.add_parser("tools", help="列出工具")
    tools.add_argument("--json", action="store_true")

    call = sub.add_parser("call", help="调用一个工具")
    call.add_argument("tool")
    call.add_argument("args", nargs="?", default="{}", help="JSON 参数对象")

    args = parser.parse_args(argv)
    name = args.server or default_server_name()
    cfg = load_server(name)
    print(f"server={cfg.name} transport={cfg.transport} url={cfg.url}", file=sys.stderr)

    with McpClient(cfg) as mcp:
        if args.cmd == "ping":
            tools_list = mcp.list_tools()
            names = [t.get("name") for t in tools_list]
            print(f"ok tools={len(names)}")
            for item in names[:20]:
                print(f"  {item}")
            if len(names) > 20:
                print(f"  … {len(names) - 20} more")
            return 0
        if args.cmd == "tools":
            tools_list = mcp.list_tools()
            if args.json:
                print(json.dumps(tools_list, ensure_ascii=False, indent=2))
            else:
                for item in tools_list:
                    print(item.get("name"))
            return 0
        payload = json.loads(args.args)
        if not isinstance(payload, dict):
            raise SystemExit("args 必须是 JSON 对象")
        result = mcp.call(args.tool, payload)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
