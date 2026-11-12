# -*- coding: utf-8 -*-
"""treeview plug 修補工具 — 下載最新版、烤入 mod_tcsoft 固定配色、上傳空間。

Usage:
    python patch_treeview.py            # 下載 GitHub 最新版 → patch → 上傳
    python patch_treeview.py <本地檔>   # 用指定的 plug 檔當來源

背景:SilverBullet 的側欄面板 iframe 吃不到 space-style(變數與 class 都
進不去),treeview 未修補時所有顏色跟 SB 內建預設的深淺跑。本工具把它內嵌
的 assets/treeview.css(base64 打包)中所有外部變數引用換成固定配色,
讓側欄不分深淺恆定 mod_tcsoft 樣貌。treeview 更新版本後重跑本工具即可。
上傳後在 SilverBullet 跑 Plugs: Reload + System: Reload 生效。
"""
import base64
import io
import os
import re
import sys

import requests

PLUG_URL = ("https://raw.githubusercontent.com/joekrill/"
            "silverbullet-treeview/main/treeview.plug.js")
# connection settings come from the environment: SB_URL (LAN address),
# SB_URL_EXT (optional second address), SB_TOKEN (SilverBullet API token)
SB_BASE = os.environ["SB_URL"].rstrip("/")
SB_BASE_EXT = os.environ.get("SB_URL_EXT", SB_BASE).rstrip("/")
TOKEN = os.environ["SB_TOKEN"]
H = {"X-Sync-Mode": "true", "Authorization": f"Bearer {TOKEN}"}

# mod_tcsoft 固定配色(vim 主題 mod_tcsoft.vim 移植)
REPLACEMENTS = [
    ("background-color: var(--root-background-color)",
     "background-color: #fffaf0"),
    ("--treeview-page-color: var(--editor-wiki-link-page-color);",
     "--treeview-page-color: #000000;"),
    ("--treeview-page-background-color: "
     "var(--editor-wiki-link-page-background-color);",
     "--treeview-page-background-color: transparent;"),
    ("--treeview-folder-color: var(--editor-wiki-link-page-missing-color);",
     "--treeview-folder-color: #993300;"),
    ("--treeview-attachment-color: var(--subtle-color);",
     "--treeview-attachment-color: #666666;"),
    ("--treeview-current-page-color: #eee;",
     "--treeview-current-page-color: #000000;"),
    ("--treeview-current-page-background-color: var(--ui-accent-color);",
     "--treeview-current-page-background-color: #d1cdc5;"),
    ("--treeview-current-page-border-color: "
     "var(--treeview-page-border-color);",
     "--treeview-current-page-border-color: #ff9900;"),
    ("background-color: var(--editor-widget-background-color)",
     "background-color: #f5efe3"),
    ("border: var(--top-border-color) 1px solid",
     "border: #d1cdc5 1px solid"),
    ("color: var(--top-color)", "color: #000000"),
    ("background-color: var(--action-button-background-color)",
     "background-color: transparent"),
    ("color: var(--action-button-color)", "color: #555555"),
    ("color: var(--action-button-hover-color)", "color: #993300"),
    ("color: var(--action-button-active-color)", "color: #ff9900"),
    ("color: var(--root-color)", "color: #000000"),
    ("var(--ui-accent-color)", "#ff9900"),
    ("var(--subtle-background-color)", "#e8e2d4"),
    ("var(--subtle-color)", "#777777"),
    ("var(--editor-command-button-color)", "#555555"),
]

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def main():
    if len(sys.argv) > 1:
        plug = open(sys.argv[1], encoding="utf-8").read()
        print(f"source: {sys.argv[1]} ({len(plug)} chars)")
    else:
        plug = requests.get(PLUG_URL, timeout=60).text
        print(f"source: GitHub main ({len(plug)} chars)")

    m = re.search(r'"assets/treeview\.css":\{data:"data:text/css;base64,'
                  r'([A-Za-z0-9+/=]+)"', plug)
    assert m, "找不到內嵌 css 儲存區(plug 打包格式可能變了,人工檢查)"
    css = base64.b64decode(m.group(1)).decode("utf-8")

    missed = [old for old, _ in REPLACEMENTS if old not in css]
    for old, new in REPLACEMENTS:
        css = css.replace(old, new)
    if missed:
        print(f"WARN: {len(missed)} 條樣板沒對上(上游 css 改版了,檢查後補):")
        for x in missed:
            print("  ", x)
    leftovers = sorted(set(re.findall(r"var\(--(?!treeview|st-)[\w-]+\)",
                                      css)))
    leftovers = [x for x in leftovers
                 if "font" not in x and "padding" not in x]
    if leftovers:
        print("WARN: 仍引用外部顏色變數(可能造成跟主題漂移):", leftovers)

    plug2 = plug.replace(
        m.group(1),
        base64.b64encode(css.encode("utf-8")).decode("ascii"))
    body = plug2.encode("utf-8")

    base = SB_BASE
    try:
        requests.get(f"{base}/.fs", headers=H, timeout=5)
    except requests.RequestException:
        base = SB_BASE_EXT
    r = requests.put(f"{base}/.fs/_plug/treeview.plug.js", data=body,
                     headers={**H, "Content-Type": "application/javascript"},
                     timeout=60)
    r.raise_for_status()
    sizes = {f["name"]: f["size"]
             for f in requests.get(f"{base}/.fs", headers=H,
                                   timeout=30).json()}
    assert sizes.get("_plug/treeview.plug.js") == len(body), "verify failed"
    print(f"OK _plug/treeview.plug.js ({len(body)} bytes, verified) — "
          "SilverBullet 跑 Plugs: Reload + System: Reload 生效")


if __name__ == "__main__":
    main()
