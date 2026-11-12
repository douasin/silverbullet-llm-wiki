# -*- coding: utf-8 -*-
"""SilverBullet space CLI — list / get / put / delete.

Usage:
    python sb.py list [prefix] [--all]
    python sb.py get  <page-path> [output-file] [--archived]
    python sb.py put  <page-path> <local-file> [--archived]
    python sb.py delete <page-path> [--archived]

Page paths use forward slashes; ".md" is appended automatically for pages
(paths that already contain an extension, e.g. .png, are used as-is).

Archive/ 下是使用者封存的頁面:list 預設隱藏(--all 才列出),讀寫刪一律
拒絕——**先取得使用者明確允許**,再帶 --archived 旗標重試。
"""
import os
import sys
import io
import mimetypes
from urllib.parse import quote

import requests

# connection settings come from the environment (Claude Code: the "env"
# block of settings.json). SB_URL is the LAN address; SB_URL_EXT is an
# optional second address tried when the first is unreachable.
BASE = os.environ["SB_URL"].rstrip("/")
BASE_EXT = os.environ.get("SB_URL_EXT", BASE).rstrip("/")
USER, PW = os.environ.get("SB_USER", ""), os.environ.get("SB_PASSWORD", "")
TOKEN = os.environ.get("SB_TOKEN", "")  # API bearer(優先)
H = {"X-Sync-Mode": "true"}
ARCHIVE_PREFIX = "Archive/"

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def login():
    s = requests.Session()
    global BASE
    if TOKEN:
        s.headers["Authorization"] = f"Bearer {TOKEN}"
        for base in (BASE, BASE_EXT):
            try:
                if s.get(f"{base}/.fs", headers=H, timeout=10).ok:
                    BASE = base
                    return s
            except requests.RequestException:
                continue
        sys.exit("cannot reach SilverBullet (tried LAN and external URL)")
    for base in (BASE, BASE_EXT):
        try:
            r = s.post(f"{base}/.auth",
                       data={"username": USER, "password": PW, "from": "/"},
                       timeout=10)
            if r.ok:
                BASE = base
                return s
        except requests.RequestException:
            continue
    sys.exit("cannot reach SilverBullet (tried LAN and external URL)")


def check_archive(path, flags):
    if path.startswith(ARCHIVE_PREFIX) and "--archived" not in flags:
        sys.exit(f"REFUSED: {path} 已封存 — 未經使用者允許不得讀寫。"
                 "取得允許後加 --archived 重試。")


def norm(path):
    path = path.strip().strip("/")
    if "." not in path.rsplit("/", 1)[-1]:
        path += ".md"
    return path


def main():
    flags = {a for a in sys.argv if a.startswith("--")}
    sys.argv = [a for a in sys.argv if not a.startswith("--")]
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    cmd = sys.argv[1]
    s = login()

    if cmd == "list":
        prefix = sys.argv[2] if len(sys.argv) > 2 else ""
        files = s.get(f"{BASE}/.fs", headers=H).json()
        for f in sorted(files, key=lambda x: x["name"]):
            if f["name"].startswith("Library/"):
                continue
            if (f["name"].startswith(ARCHIVE_PREFIX) and "--all" not in flags
                    and not prefix.startswith(ARCHIVE_PREFIX.rstrip("/"))):
                continue  # 封存頁預設不列
            if prefix and not f["name"].startswith(prefix):
                continue
            print(f"{f['size']:>9}  {f['name']}")

    elif cmd == "get":
        path = norm(sys.argv[2])
        check_archive(path, flags)
        r = s.get(f"{BASE}/.fs/{quote(path)}", headers=H)
        r.raise_for_status()
        if len(sys.argv) > 3:
            with open(sys.argv[3], "wb") as f:
                f.write(r.content)
            print(f"saved {path} -> {sys.argv[3]} ({len(r.content)} bytes)")
        else:
            print(r.content.decode("utf-8"))

    elif cmd == "put":
        path = norm(sys.argv[2])
        check_archive(path, flags)
        with open(sys.argv[3], "rb") as f:
            body = f.read()
        if path.endswith(".md") and b"\r" in body:
            sys.exit(f"REFUSED: {sys.argv[3]} contains CR/CRLF line endings — "
                     "produce the file with LF only (Write tool or "
                     "python open(..., newline='\\n'))")
        ctype = mimetypes.guess_type(path)[0] or "text/markdown"
        r = s.put(f"{BASE}/.fs/{quote(path)}", data=body,
                  headers={**H, "Content-Type": ctype})
        r.raise_for_status()
        # verify the write actually landed
        names = {f["name"]: f["size"] for f in s.get(f"{BASE}/.fs", headers=H).json()}
        if names.get(path) == len(body):
            print(f"OK {path} ({len(body)} bytes, verified)")
        else:
            sys.exit(f"WROTE BUT VERIFY FAILED: {path} not in listing with expected size")

    elif cmd == "delete":
        path = norm(sys.argv[2])
        check_archive(path, flags)
        r = s.delete(f"{BASE}/.fs/{quote(path)}", headers=H)
        r.raise_for_status()
        print(f"deleted {path}")

    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
