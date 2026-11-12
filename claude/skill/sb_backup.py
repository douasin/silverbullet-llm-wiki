# -*- coding: utf-8 -*-
"""SilverBullet space backup — snapshot the whole space into a dated tar.gz.

Usage:
    python sb_backup.py [dest-root]

dest-root defaults to ~/silverbullet_backups.
Creates dest-root/silverbullet-<YYYY-MM-DD>.tar.gz containing:
  - the whole space (all files, Library included; via HTTP API)
  - history-git.tar.gz — the space's git history repo, pulled from the NAS
    over SSH (restore: extract into /volume1/docker/silverbullet-mcp/history/)
Re-running on the same day replaces that day's archive. Page mtimes are
each page's lastModified from the server. The git part needs the NAS to be
reachable over SSH (home LAN) — the tool aborts before writing anything
if it is not, so run it from home.
"""
import datetime
import io
import json
import os
import sys
import tarfile
import time
from pathlib import Path
from urllib.parse import quote

import requests

# connection settings come from the environment (same variables as sb.py).
# NAS_CREDENTIALS is the path of a JSON file with the NAS SSH login:
# {"host": ..., "port": ..., "user": ..., "password": ...}
NAS_CRED = Path(os.environ["NAS_CREDENTIALS"])
HISTORY_DIR = "/volume1/docker/silverbullet-mcp/history"

BASE = os.environ["SB_URL"].rstrip("/")
BASE_EXT = os.environ.get("SB_URL_EXT", BASE).rstrip("/")
USER, PW = os.environ.get("SB_USER", ""), os.environ.get("SB_PASSWORD", "")
TOKEN = os.environ.get("SB_TOKEN", "")  # API bearer(優先)
H = {"X-Sync-Mode": "true"}
DEFAULT_ROOT = Path.home() / "silverbullet_backups"

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


def fetch_history_git():
    """Pull the space's git repo off the NAS as a tar.gz byte blob.
    The repo is root-owned, so tar runs under sudo with the password on
    stdin; stdout carries the raw archive."""
    import paramiko
    cfg = json.loads(NAS_CRED.read_text(encoding="utf-8"))
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(cfg["host"], port=cfg["port"], username=cfg["user"],
                       password=cfg["password"], timeout=20)
    except Exception as e:
        sys.exit(f"cannot reach NAS over SSH for the git part ({e}) — "
                 "run this from the home network")
    stdin, out, err = client.exec_command(
        f'sudo -S -p "" tar -czf - -C "{HISTORY_DIR}" git', timeout=300)
    stdin.write(cfg["password"] + "\n")
    stdin.flush()
    data = out.read()
    problem = err.read().decode(errors="replace").strip()
    client.close()
    if not data.startswith(b"\x1f\x8b"):
        sys.exit(f"history git backup failed: {problem[:300]}")
    return data


def main():
    root = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_ROOT
    root.mkdir(parents=True, exist_ok=True)
    archive = root / f"silverbullet-{datetime.date.today().isoformat()}.tar.gz"
    partial = archive.with_suffix(".part")
    git_blob = fetch_history_git()
    s = login()
    files = s.get(f"{BASE}/.fs", headers=H).json()
    total_bytes = 0
    with tarfile.open(partial, "w:gz") as tar:
        info = tarfile.TarInfo("history-git.tar.gz")
        info.size = len(git_blob)
        info.mtime = time.time()
        tar.addfile(info, io.BytesIO(git_blob))
        for f in sorted(files, key=lambda x: x["name"]):
            name = f["name"]
            r = s.get(f"{BASE}/.fs/{quote(name)}", headers=H)
            r.raise_for_status()
            if len(r.content) != f["size"]:
                partial.unlink(missing_ok=True)
                sys.exit(f"ABORT {name}: downloaded {len(r.content)} bytes, "
                         f"listing says {f['size']} — refusing partial backup")
            info = tarfile.TarInfo(name)
            info.size = len(r.content)
            info.mtime = f.get("lastModified", 0) / 1000 or time.time()
            tar.addfile(info, io.BytesIO(r.content))
            total_bytes += len(r.content)
    partial.replace(archive)
    # 回寫備份狀態頁(系統狀態端點會顯示;LF-only)
    status = (f"# Backup Status\n\n"
              f"- 時間:{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
              f"- 空間:{len(files)} 檔,{total_bytes} bytes\n"
              f"- git 倉庫:{len(git_blob)} bytes\n"
              f"- 壓縮檔:{archive}({archive.stat().st_size} bytes)\n")
    s.put(f"{BASE}/.fs/Trigger/backup-status.md",
          data=status.encode("utf-8"),
          headers={**H, "Content-Type": "text/markdown"})
    print(f"OK {len(files)} files, {total_bytes} bytes "
          f"+ history-git {len(git_blob)} bytes "
          f"-> {archive} ({archive.stat().st_size} bytes compressed)")


if __name__ == "__main__":
    main()
