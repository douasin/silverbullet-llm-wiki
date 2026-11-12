"""Review: move every newly written kb page from the weekly staging pages into
its category index, oldest staging page first, through the bridge's internal
endpoint. Stops at the first refusal.

usage: review_move_all.py [<proposed category>=<final category> ...]
The bridge URL (…/<MCP_PATH_SECRET>/mcp) comes from SILVERBULLET_MCP_URL.
"""
import asyncio
import os
import re
import sys

import httpx
from fastmcp import Client

sys.stdout.reconfigure(encoding="utf-8")
MCP_URL = os.environ["SILVERBULLET_MCP_URL"]
BASE = MCP_URL.rsplit("/mcp", 1)[0]


async def main():
    async with Client(MCP_URL, timeout=120) as c:
        async def call(tool, **a):
            r = await c.call_tool(tool, a)
            return "\n".join(b.text for b in r.content if hasattr(b, "text"))

        listing = await call("list_pages", prefix="kb/index/week/")
        weeks = sorted(line.split()[-1][:-3] for line in listing.split("\n") if line.strip().endswith(".md"))
        retarget = dict(arg.split("=", 1) for arg in sys.argv[1:])
        names = []
        proposed_by_name = {}
        for week in weeks:
            text = await call("read_page", path=week)
            for name, proposed in re.findall(r"^- \[\[kb/([^\]]+)\]\] #\S+ → (\S+)", text, re.M):
                proposed_by_name.setdefault(name, []).append(proposed)
                if name not in names:
                    names.append(name)
            names += [n for n in re.findall(r"^  ⇒ deprecate \[\[kb/([^\]]+)\]\]", text, re.M)
                      if n not in names]
        print(f"{len(names)} 頁待搬")
        for name in names:
            final = {p: retarget[p] for p in proposed_by_name.get(name, []) if p in retarget}
            r = httpx.post(f"{BASE}/internal/kb_move_pending_to_index",
                           json={"name": name, "final_index_by_proposed": final}, timeout=120)
            print(f"--- {name}  [{r.status_code}]\n{r.text}")
            if r.status_code != 200:
                print("停在這裡，後面的沒搬")
                return
        print("\n=== kb_index ===\n" + (await call("kb_index")).split("=== 近期新增")[1])
        print("=== week pages left ===\n" + await call("list_pages", prefix="kb/index/week/"))

asyncio.run(main())
