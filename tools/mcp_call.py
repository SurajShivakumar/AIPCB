"""Tiny MCP client for driving the KiCad engine (or copilot) from the shell.

Usage:
    python tools/mcp_call.py engine list
    python tools/mcp_call.py engine call <tool> '<json args>' [<tool> '<json args>' ...]
    python tools/mcp_call.py copilot call <tool> '<json args>'

Multiple tool calls in one invocation share a single server session, so
state such as an opened project carries across calls.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parent.parent
ENGINE_DIR = ROOT / "vendor" / "KiCAD-MCP-Server"


def server_params(name: str) -> StdioServerParameters:
    if name == "engine":
        return StdioServerParameters(
            command="node",
            args=[str(ENGINE_DIR / "dist" / "index.js")],
            cwd=str(ENGINE_DIR),
            env={**os.environ, "LOG_LEVEL": "error", "KICAD_AUTO_LAUNCH": "false"},
        )
    if name == "copilot":
        return StdioServerParameters(
            command=str(ROOT / ".venv" / "Scripts" / "python.exe"),
            args=["-m", "copilot.server"],
            cwd=str(ROOT),
            env={**os.environ},
        )
    raise SystemExit(f"unknown server {name!r}")


def _render(result) -> str:
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        parts.append(text if text is not None else f"<{block.type} block>")
    return "\n".join(parts)


async def _call_when_ready(session: ClientSession, tool: str, args: dict, wait_s: float = 90):
    # The engine connects its MCP transport before its Python worker is spawned,
    # so calls in the first seconds fail with "not running". Retry until it's up.
    deadline = asyncio.get_running_loop().time() + wait_s
    while True:
        result = await session.call_tool(tool, args)
        if not (result.is_error and "is not running" in _render(result)):
            return result
        if asyncio.get_running_loop().time() > deadline:
            return result
        await asyncio.sleep(2)


async def main(argv: list[str]) -> None:
    server, action, *rest = argv
    async with stdio_client(server_params(server)) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            if action == "list":
                tools = (await session.list_tools()).tools
                for t in tools:
                    print(f"{t.name}: {(t.description or '').splitlines()[0][:100]}")
                print(f"\n{len(tools)} tools")
                return
            if action == "schema":
                tools = {t.name: t for t in (await session.list_tools()).tools}
                for name in rest:
                    print(name, json.dumps(tools[name].input_schema, indent=1))
                return
            pairs = list(zip(rest[0::2], rest[1::2]))
            for tool, raw_args in pairs:
                result = await _call_when_ready(session, tool, json.loads(raw_args))
                status = "ERROR" if result.is_error else "ok"
                print(f"=== {tool} [{status}]")
                print(_render(result))


if __name__ == "__main__":
    timeout = float(os.environ.get("MCP_CALL_TIMEOUT", "300"))
    try:
        asyncio.run(asyncio.wait_for(main(sys.argv[1:]), timeout))
    except TimeoutError:
        raise SystemExit(f"timed out after {timeout:.0f}s")
