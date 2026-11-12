"""Claude Code SessionStart hook: load the kb root index (categories, routing
tags, recently added pages) through the SilverBullet MCP bridge and hand it to
the model as additional context. One MCP call; on any failure it says so
briefly and exits 0 so the session still starts.

The bridge URL (with its path secret) comes from the SILVERBULLET_MCP_URL
environment variable, set in the "env" block of Claude Code's settings.json;
nothing is printed except the kb_index text.
"""
import asyncio
import json
import os
import sys


def emit(text: str):
    sys.stdout.reconfigure(encoding="utf-8")
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                             "additionalContext": text}},
                     ensure_ascii=False))


async def main():
    from fastmcp import Client
    async with Client(os.environ["SILVERBULLET_MCP_URL"], timeout=20) as c:
        r = await c.call_tool("kb_index", {})
        return "\n".join(b.text for b in r.content if hasattr(b, "text"))


try:
    index = asyncio.run(main())
    emit("【kb_index 的結果（SessionStart hook 自動載入；session 開頭不必再呼叫，之後要看最新的內容再呼叫）】"
         "怎麼讀、怎麼寫照 SilverBullet MCP 工具 readme() 回的內容的「知識庫」節。\n\n" + index)
except Exception as e:  # bridge down, LAN unavailable, etc. — never block the session
    emit(f"【kb 知識庫】根 index 載入失敗（{type(e).__name__}），需要時手動呼叫 kb_index。")
sys.exit(0)
