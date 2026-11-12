# -*- coding: utf-8 -*-
"""SilverBullet MCP server — expose page list/read/write/delete as MCP tools.

Designed to run next to SilverBullet (same NAS) and be reached from
claude.ai custom connectors over HTTPS.

Security model (layered):
  1. URL path secret (MCP_PATH_SECRET, required): the endpoint only exists at
     /<secret>/mcp — a capability URL. Anything else 404s.
  2. Optional bearer token (MCP_BEARER_TOKEN): if set, requests must also
     carry "Authorization: Bearer <token>". Leave unset for claude.ai
     connectors (their UI cannot send custom headers; the path secret is
     the credential there).
  3. SilverBullet credentials live only in this process's environment —
     they are never exposed through the MCP surface.

Env vars:
  SB_URL            SilverBullet base URL as seen from this container
                    (e.g. http://<NAS-IP>:3001)
  SB_USER           SilverBullet username
  SB_PASSWORD       SilverBullet password
  MCP_PATH_SECRET   long random string, e.g. secrets.token_urlsafe(32)
  MCP_BEARER_TOKEN  optional second factor (see above)
  PORT              listen port (default 8008)
"""
import hashlib
import hmac
import html
import mimetypes
import os
import re
import subprocess
import sys
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote

import httpx
import markdown
import uvicorn
from fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse
from starlette.routing import Mount, Route

SB_URL = os.environ["SB_URL"].rstrip("/")
SB_USER = os.environ["SB_USER"]
SB_PASSWORD = os.environ["SB_PASSWORD"]
# preferred auth: API bearer token (SB_AUTH_TOKEN on the SilverBullet side);
# falls back to cookie login with user/password when unset
SB_TOKEN = os.environ.get("SB_TOKEN", "")
PATH_SECRET = os.environ["MCP_PATH_SECRET"]
BEARER_TOKEN = os.environ.get("MCP_BEARER_TOKEN", "")
# public read-only share links: /share/<SHARE_SECRET>/<page>, only for pages
# whose frontmatter opts in with `share: true` (or `share: edit`).
# Empty = feature disabled.
SHARE_SECRET = os.environ.get("SHARE_SECRET", "")
# collaboration write access: scoped MCP at /ally/<ALLY_SECRET>/mcp and raw
# GET/PUT at /edit/<ALLY_SECRET>/<page>, only for pages whose frontmatter
# opts in with `share: edit`. Empty = feature disabled.
ALLY_SECRET = os.environ.get("ALLY_SECRET", "")
PORT = int(os.environ.get("PORT", "8008"))
LOG_LEVEL = os.environ.get("LOG_LEVEL", "info")
# page history: the space folder is mounted read-only and mirrored into a
# git repo living OUTSIDE the space (SilverBullet never sees .git). Every
# successful write through this bridge commits with a source label; a sweep
# thread catches manual/browser edits. Feature auto-disables if the mounts
# are absent (e.g. local testing).
SPACE_DIR = os.environ.get("SPACE_DIR", "/space")
HISTORY_DIR = os.environ.get("HISTORY_DIR", "/history")
GIT_SWEEP_SECONDS = int(os.environ.get("GIT_SWEEP_SECONDS", "600"))
BUILD_TAG = "2026-11-11 (review side off the tool face; registered changes read as done)"
# pages under this prefix are user-archived: AI tools must not read or write
# them without the user's explicit permission (allow_archived=true)
ARCHIVE_PREFIX = "Archive/"
# serve TLS directly (for router-forwarded deployments with no reverse proxy):
# set both to enable; plain HTTP is then rejected at the handshake
SSL_CERTFILE = os.environ.get("SSL_CERTFILE", "")
SSL_KEYFILE = os.environ.get("SSL_KEYFILE", "")

if len(PATH_SECRET) < 16:
    sys.exit("MCP_PATH_SECRET too short — use at least 16 chars "
             "(python -c \"import secrets;print(secrets.token_urlsafe(32))\")")

SYNC_HEADERS = {"X-Sync-Mode": "true"}


class SilverBullet:
    """Thin client over SilverBullet's /.fs HTTP API. Bearer-token auth when
    SB_TOKEN is set (stateless, never expires); cookie re-login otherwise."""

    def __init__(self):
        headers = {"Authorization": f"Bearer {SB_TOKEN}"} if SB_TOKEN else {}
        self.client = httpx.Client(base_url=SB_URL, timeout=20,
                                   headers=headers)
        if not SB_TOKEN:
            self._login()

    def _login(self):
        r = self.client.post("/.auth",
                             data={"username": SB_USER,
                                   "password": SB_PASSWORD,
                                   "from": "/"})
        if r.status_code >= 400:
            raise RuntimeError(f"SilverBullet login failed: HTTP {r.status_code}")
        # SilverBullet answers HTTP 200 with an error JSON on bad credentials
        if "application/json" in r.headers.get("content-type", ""):
            body = r.json()
            if body.get("status") != "ok":
                raise RuntimeError(f"SilverBullet login rejected: {body}")

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        headers = {**SYNC_HEADERS, **kwargs.pop("headers", {})}
        r = self.client.request(method, path, headers=headers, **kwargs)
        # cookie mode: expired cookie shows as 401/403 or the login page
        expired = not SB_TOKEN and (
            r.status_code in (401, 403) or (
                r.status_code == 200
                and "text/html" in r.headers.get("content-type", "")
            )
        )
        if expired:
            self._login()
            r = self.client.request(method, path, headers=headers, **kwargs)
        r.raise_for_status()
        return r

    def listing(self) -> list[dict]:
        return self._request("GET", "/.fs").json()

    def read(self, path: str) -> bytes:
        return self._request("GET", f"/.fs/{quote(path)}").content

    def write(self, path: str, body: bytes):
        ctype = mimetypes.guess_type(path)[0] or "text/markdown"
        self._request("PUT", f"/.fs/{quote(path)}", content=body,
                      headers={"Content-Type": ctype})

    def delete(self, path: str):
        self._request("DELETE", f"/.fs/{quote(path)}")


sb = SilverBullet()

GIT_ENABLED = os.path.isdir(SPACE_DIR) and os.path.isdir(HISTORY_DIR)
GIT_ENV = {**os.environ, "GIT_DIR": os.path.join(HISTORY_DIR, "git"),
           "GIT_WORK_TREE": SPACE_DIR}
_git_lock = threading.Lock()


def _git(*args, check=False) -> str:
    r = subprocess.run(
        ["git", "-c", "user.name=sb-mcp", "-c", "user.email=sb-mcp@nas",
         *args],
        env=GIT_ENV, cwd=SPACE_DIR, capture_output=True, encoding="utf-8")
    if check and r.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {r.stderr.strip()}")
    return r.stdout


def git_commit(message: str):
    if not GIT_ENABLED:
        return
    # SilverBullet may flush the write to disk a beat after replying
    time.sleep(0.3)
    with _git_lock:
        _git("add", "-A")
        _git("commit", "-m", message)  # exits 1 when clean — fine


def git_commit_async(message: str):
    threading.Thread(target=git_commit, args=(message,), daemon=True).start()


def _git_init():
    if not GIT_ENABLED:
        print("git history disabled (space/history mounts missing)",
              flush=True)
        return
    if not os.path.isdir(os.path.join(HISTORY_DIR, "git")):
        _git("init", check=True)
    git_commit("startup snapshot")
    print(f"git history enabled ({SPACE_DIR} -> {GIT_ENV['GIT_DIR']})",
          flush=True)


def _sweep_loop():
    while True:
        time.sleep(GIT_SWEEP_SECONDS)
        git_commit("sweep (manual/browser edits)")


def _require_git():
    if not GIT_ENABLED:
        raise RuntimeError("page history is not available on this "
                           "deployment (space/history mounts missing)")


def norm(path: str) -> str:
    path = path.strip().strip("/")
    if "." not in path.rsplit("/", 1)[-1]:
        path += ".md"
    return path


def _check_archive(full: str, allow_archived: bool):
    if full.startswith(ARCHIVE_PREFIX) and not allow_archived:
        raise ValueError(
            f"{full} 已封存:未經使用者允許不得讀寫。先向使用者取得允許,"
            "再帶 allow_archived=true 重試。")


mcp = FastMCP(
    name="SilverBullet",
    instructions=(
        "Read and write pages in the user's SilverBullet note space. "
        "FIRST ACTION in every session: call readme() and follow what it "
        "returns. It is the only description of how to use this space and "
        "these tools; the tool descriptions say only what each tool is."
    ),
)

README_PAGE = "AI 守則/SilverBullet 維護手冊.md"
README_VERSION_LABEL = "readme 版本："


def _readme_text_and_version() -> tuple[str, str]:
    text = sb.read(README_PAGE).decode("utf-8")
    return text, hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


def _raise_when_readme_version_is_not_current(tool: str, readme: str):
    """Refuse a call whose caller has not read the current usage text."""
    if (readme or "").strip() == _readme_text_and_version()[1]:
        return
    raise ValueError(
        f"{tool} 拒收：readme 參數不是現行版本，用法可能已經更新。重新呼叫 readme() 讀完，"
        "照現行用法、帶它回的版本再呼叫。工具清單裡沒有 readme 表示這條連線載入的是舊的工具清單，"
        "請使用者重連 connector 或開新的 session。")


@mcp.tool
def readme() -> str:
    """How to use this space and every tool here. Call it first in every
    session, and again whenever a tool tells you the usage has changed."""
    text, version = _readme_text_and_version()
    return f"{README_VERSION_LABEL}{version}\n\n{text}"


@mcp.tool
def list_pages(prefix: str = "") -> str:
    """List pages (and attachments) in the space, optionally filtered by
    path prefix. Returns one "size  name" line per file."""
    lines = []
    for f in sorted(sb.listing(), key=lambda x: x["name"]):
        name = f["name"]
        if name.startswith("Library/"):
            continue
        if name.startswith(ARCHIVE_PREFIX):
            continue  # user-archived pages are invisible to AI tools
        if prefix and not name.startswith(prefix):
            continue
        lines.append(f"{f['size']:>9}  {name}")
    if not lines:
        return f"(no pages matching prefix '{prefix}')"
    return "\n".join(lines)


@mcp.tool
def read_page(path: str, allow_archived: bool = False) -> str:
    """Read the full content of a page. `path` example:
    "Projects/Notes" (no .md needed). Pages under Archive/ are
    user-archived: reading them requires the user's explicit permission
    in this conversation, then pass allow_archived=true."""
    full = norm(path)
    _check_archive(full, allow_archived)
    data = sb.read(full)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"{full} is binary ({len(data)} bytes) — "
                         "only text pages can be read through this tool")


@mcp.tool
def write_page(path: str, content: str, allow_archived: bool = False) -> str:
    """Create or REPLACE a page with `content` (full page text).
    To edit, read_page first, modify, then write back the whole text.
    Writing under Archive/ requires the user's explicit permission
    (then pass allow_archived=true)."""
    full = norm(path)
    _check_archive(full, allow_archived)
    body = content.encode("utf-8")
    sb.write(full, body)
    sizes = {f["name"]: f["size"] for f in sb.listing()}
    if sizes.get(full) != len(body):
        raise RuntimeError(f"wrote {full} but verification failed "
                           "(size mismatch in listing)")
    git_commit_async(f"write_page {full} (main)")
    return f"OK {full} ({len(body)} bytes, verified)"


@mcp.tool
def delete_page(path: str, allow_archived: bool = False) -> str:
    """Delete a page. Irreversible — confirm with the user before calling
    this on anything they did not explicitly ask to delete. Archived pages
    (Archive/) additionally require allow_archived=true after permission."""
    full = norm(path)
    _check_archive(full, allow_archived)
    sb.delete(full)
    git_commit_async(f"delete_page {full} (main)")
    return f"deleted {full}"


@mcp.tool
def page_history(path: str, limit: int = 20) -> str:
    """List the change history of a page: one "rev  date  action" line per
    revision, newest first. Use the rev with page_diff / restore_page."""
    _require_git()
    full = norm(path)
    out = _git("log", f"-{int(limit)}", "--pretty=format:%h  %ad  %s",
               "--date=format:%Y-%m-%d %H:%M", "--", full)
    return out or f"(no history for {full})"


@mcp.tool
def page_diff(path: str, rev: str, allow_archived: bool = False) -> str:
    """Show what changed between revision `rev` (from page_history) and the
    page as it is now."""
    _require_git()
    full = norm(path)
    _check_archive(full, allow_archived)
    if not full.endswith(".md"):
        raise ValueError("history tools handle text pages only")
    out = _git("diff", rev, "--", full)
    return out or f"(no difference between {rev} and current)"


@mcp.tool
def restore_page(path: str, rev: str, allow_archived: bool = False) -> str:
    """Restore a page to its content at revision `rev` (from page_history).
    Writes through the normal API so SilverBullet reindexes immediately;
    the restore itself is recorded in history, so it can be undone."""
    _require_git()
    full = norm(path)
    _check_archive(full, allow_archived)
    if not full.endswith(".md"):
        raise ValueError("history tools handle text pages only")
    content = _git("show", f"{rev}:{full}", check=True)
    body = content.encode("utf-8")
    sb.write(full, body)
    sizes = {f["name"]: f["size"] for f in sb.listing()}
    if sizes.get(full) != len(body):
        raise RuntimeError(f"restored {full} but verification failed "
                           "(size mismatch in listing)")
    git_commit_async(f"restore_page {full} @ {rev} (main)")
    return f"OK restored {full} to {rev} ({len(body)} bytes, verified)"


# ---- knowledge base (kb/) ----
# Layout: kb/<name> knowledge or _source page (flat, never moved);
# kb/index/<category> category index — a "/"-separated path of any depth
# (大類, 大類/小類, 大類/小類/子類, …), each level its own page — created only
# when the user approves (kb_create_index, or kb_move_pending_to_index on
# a target that does not exist yet); kb/index/week/<ISO week> staging page
# where this week's writes are registered with their proposed category for
# the user's weekly review; kb/log append-only timeline. A page's kind
# (knowledge / decision / record) is a value, not a structure: it is the
# 類型 line under the title and the #<kind> tag right after the link on the
# staging and index rows; a decision additionally carries a 理由 section
# above the separator. Routing hints are plain #tags at the end of the
# link lines in the index pages: a category's own line carries its pages'
# tags, each ancestor's line carries the names of its child categories;
# they ride the staging line and are appended when the line is moved, or
# via kb_append_index_tags.
# Every tool validates the input shape, then writes and logs; nothing is
# verified or checked afterwards.
KB_PREFIX = "kb/"
KB_ROOT_INDEX = "kb/index.md"
KB_INDEX_PREFIX = "kb/index/"
KB_WEEK_PREFIX = "kb/index/week/"
KB_DEPRECATED_PREFIX = "kb/deprecated/"
KB_DEPRECATED_INDEX = "kb/index/deprecated.md"
KB_LOG = "kb/log.md"
KB_SEPARATOR = "-----以下為原文，非必要不讀-----"
KB_LINKS_SEPARATOR = "-----連結記錄，工具維護-----"
KB_SUMMARY_HEADING = "## 總結｜"
KB_REASONING_HEADING = "## 理由"
KB_ORIGINAL_HEADING = "## 原文｜"
KB_KIND_LINE = "類型："
KB_FILE_LINE = "檔案："
KB_STATUS_LINE = "狀態："
KB_REASON_LINE = "原因："
KB_REPLACED_BY_LINE = "取代："
KB_ORIGINAL_INDEX_LINE = "原分類："
KB_LINK_OUT = "連到："
KB_LINK_IN = "被連："
KB_LINK_INDEX = "登記於："
KB_MEDIA_PREFIX = "kb-media/"
KB_KINDS = ("knowledge", "decision", "record", "source")
KB_MERGE_TARGET_KEYS = ("replace", "deprecate", "merge")
KB_RESERVED_INDEX_ROOTS = ("week", "deprecated")
KB_NAME_RE = re.compile(r"\A\d{8}_[a-z0-9_]+\Z")
KB_INDEX_RE = re.compile(r"\A[^/\s]+(?:/[^/\s]+)*\Z")
KB_TAG_RE = re.compile(r"\A[^\s#]+\Z")
KB_SOURCE_PAGE_FORBIDDEN = (KB_PREFIX, ARCHIVE_PREFIX, "Library/")


def _lf(value):
    return _normalize_lf(value) if isinstance(value, str) else value


def _kb_full(name: str) -> str:
    return f"{KB_PREFIX}{name}.md"


def _kb_index_full(index: str) -> str:
    return f"{KB_INDEX_PREFIX}{index}.md"


def _kb_bare_name(name: str, tool: str) -> str:
    """Every kb tool takes the page name in ONE form: without the kb/
    prefix. Anything else is rejected with the fix spelled out."""
    name = (name or "").strip()
    if name.startswith(KB_PREFIX):
        raise ValueError(f"{tool} 拒收：name 不含 kb/ 前綴，請傳 {name[len(KB_PREFIX):]}")
    if not KB_NAME_RE.match(name):
        raise ValueError(f"{tool} 拒收：name 須為 8 碼日期_小寫英數底線，不含 kb/")
    return name


def _kb_index_error(index) -> str | None:
    if not isinstance(index, str) or not KB_INDEX_RE.match(index):
        return "須為以 / 分層的分類路徑（層數不限，例 遊戲/screeps/capture；無空白）"
    if index.split("/")[0] in KB_RESERVED_INDEX_ROOTS:
        return f"{index.split('/')[0]} 是保留字，不能當分類"
    return None


def _kb_tags_error(tags) -> str | None:
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return "須為字串陣列"
    bad = [t for t in tags if not KB_TAG_RE.match(t)]
    if bad:
        return f"不得含空白或 #：{', '.join(bad)}"
    if len(set(tags)) != len(tags):
        return "重複"
    return None


def _kb_lower_tags(tags) -> list:
    """Tags are lowercase everywhere; the tools normalise so callers need
    not. Non-list input is returned as is for the format check to reject."""
    if not isinstance(tags, list):
        return tags
    return [t.lower() if isinstance(t, str) else t for t in tags]


def _kb_lower_index(index):
    """Category paths are lowercase everywhere, like tags; the tools
    normalise so callers need not. Non-str input is returned as is for the
    format check to reject."""
    return index.strip().lower() if isinstance(index, str) else index


def _kb_tag_suffix(tags: list) -> str:
    return "".join(f" #{t}" for t in tags)


def _kb_week_of(date: datetime) -> str:
    year, week, _ = date.isocalendar()
    return f"{year}-W{week:02d}"


# ---- link records (雙向鏈) ----
# Every kb page ends with a tool-maintained block listing the links that touch
# it: 連到 (pages it links to), 被連 (pages that link to it) and 登記於 (index
# pages that list it). Readers never see the block; retire / merge use it to
# update every referrer without scanning the space.

def _kb_split_links(text: str) -> tuple[str, list[str]]:
    body, _, block = text.partition(f"\n{KB_LINKS_SEPARATOR}\n")
    records = [line for line in block.split("\n") if line.strip()]
    return body, records


def _kb_join_links(body: str, records: list[str]) -> str:
    body = body.rstrip("\n") + "\n"
    if not records:
        return body
    return body + f"\n{KB_LINKS_SEPARATOR}\n" + "\n".join(records) + "\n"


def _kb_add_link_records(full: str, *new_records: str):
    body, records = _kb_split_links(sb.read(full).decode("utf-8"))
    added = [r for r in new_records if r not in records]
    if not added:
        return
    sb.write(full, _kb_join_links(body, records + added).encode("utf-8"))


def _kb_remove_link_records(full: str, should_remove) -> list[str]:
    """Drop the records `should_remove(record)` is true for; returns them."""
    body, records = _kb_split_links(sb.read(full).decode("utf-8"))
    dropped = [r for r in records if should_remove(r)]
    if dropped:
        sb.write(full, _kb_join_links(body, [r for r in records
                                             if r not in dropped]).encode("utf-8"))
    return dropped


def _kb_link_page(value: str) -> str:
    """'[[kb/x]]' / '[[kb/x|alias]]' / '[[index]]' → full page path."""
    inner = value.strip()[2:-2].split("|")[0]
    return norm(inner)


def _kb_registered_indexes(records: list[str]) -> list[str]:
    return [r[len(KB_LINK_INDEX):].strip() for r in records
            if r.startswith(KB_LINK_INDEX)]


def _kb_referrer_pages(records: list[str], exclude: set[str]) -> list[str]:
    pages = []
    for r in records:
        if r.startswith((KB_LINK_OUT, KB_LINK_IN)):
            value = r.split("：", 1)[1].split(" #", 1)[0]
            page = _kb_link_page(value)
            if page not in exclude and page not in pages:
                pages.append(page)
    return pages


def _kb_rewrite_links_on_page(full: str, old_name: str, new_path: str,
                              status: str, extra: str = ""):
    """Point every [[kb/<old_name>]] (alias kept) on the page at new_path
    and mark it: '[[new_path|alias]] #<status><extra>'. Already marked
    links are left alone."""
    text = _page_text_or_none(full)
    if text is None:
        return
    pattern = re.compile(rf"\[\[kb/{re.escape(old_name)}(\|[^\]]*)?\]\]"
                         rf"(?! #(?:deprecated|replaced|merged))")
    new_text = pattern.sub(lambda m: f"[[{new_path}{m.group(1) or ''}]] #{status}{extra}",
                           text)
    if new_text != text:
        sb.write(full, new_text.encode("utf-8"))


def _kb_page_kind(text: str) -> str | None:
    for line in text.split("\n")[:3]:
        if line.startswith(KB_KIND_LINE):
            return line[len(KB_KIND_LINE):].strip()
    return None


def _kb_week_rows_for(week_text: str, name: str) -> list[tuple]:
    """(kind_tag, proposed index, tag suffix, summary) of the page's pending rows."""
    return re.findall(rf"^- \[\[{re.escape(KB_PREFIX + name)}\]\] "
                      rf"(#\S+) → (\S+)((?: #\S+)*) — (.*)$", week_text, re.MULTILINE)


def _kb_marker_line(name: str, key: str, target: str, reason: str) -> str:
    line = f"  ⇒ [[{KB_PREFIX}{name}]] {key} [[{KB_PREFIX}{target}]]"
    return f"{line} — {reason}" if reason else line


def _kb_marker_re(name: str):
    return re.compile(rf"^  ⇒ \[\[{re.escape(KB_PREFIX + name)}\]\] "
                      rf"(replace|deprecate|merge) \[\[kb/([^\]]+)\]\](?: — (.*))?$",
                      re.MULTILINE)


def _kb_deprecate_marker_line(name: str, reason: str) -> str:
    """A standalone retirement proposal (no new page behind it)."""
    return f"  ⇒ deprecate [[{KB_PREFIX}{name}]] — {reason}"


def _kb_deprecate_marker_re(name: str):
    return re.compile(rf"^  ⇒ deprecate \[\[{re.escape(KB_PREFIX + name)}\]\] — (.*)$",
                      re.MULTILINE)


def _kb_has_pending(lines) -> bool:
    """A staging page still needs review while it holds a row or a marker."""
    return any(line.startswith("- [[") or line.startswith("  ⇒") for line in lines)


def _kb_find_deprecate_marker(name: str, names: set):
    """(staging page path, match) of the standalone deprecate proposal for
    the page, searched across every staging page; None when there is none."""
    marker_re = _kb_deprecate_marker_re(name)
    for week_full in sorted(n for n in names if n.startswith(KB_WEEK_PREFIX)):
        match = marker_re.search(sb.read(week_full).decode("utf-8"))
        if match:
            return week_full, match
    return None


KB_CHANGE_AGAINST_RE = re.compile(
    r"^  ⇒ (?:\[\[kb/([^\]]+)\]\] (replace|merge|deprecate)|(deprecate)) "
    r"\[\[kb/([^\]]+)\]\](?: — (.*))?$", re.MULTILINE)


def _kb_changes_registered_against_pages(names: set) -> dict:
    """{page name: (key, new page name or None, reason)} for every change on
    the staging pages that has not been carried out yet. Callers are served
    as if these were already done."""
    changes = {}
    for week_full in sorted(n for n in names if n.startswith(KB_WEEK_PREFIX)):
        for new_name, key, standalone_key, target, reason in KB_CHANGE_AGAINST_RE.findall(
                sb.read(week_full).decode("utf-8")):
            changes.setdefault(target, (key or standalone_key, new_name or None, reason))
    return changes


def _kb_gone_note(name: str, key: str, new_name: str | None, reason: str) -> str:
    """One wording for a page that no longer serves knowledge."""
    if key == "merge":
        return f"{KB_PREFIX}{name} 已併入 {KB_PREFIX}{new_name}，改讀那一頁"
    if key == "replace":
        return (f"{KB_PREFIX}{name} 已被取代（{reason}），不提供知識；"
                f"已由 {KB_PREFIX}{new_name} 取代，改讀那一頁")
    return f"{KB_PREFIX}{name} 已作廢（{reason}），不提供知識"


def _kb_page_categories(name: str, records: list[str]) -> list[str]:
    """Categories the page is listed in, or (still pending) proposed for."""
    categories = []
    for reg in _kb_registered_indexes(records):
        if not reg.startswith("week/"):
            categories.append(reg)
            continue
        week_text = _page_text_or_none(_kb_index_full(reg)) or ""
        categories += [proposed for _, proposed, _, _ in _kb_week_rows_for(week_text, name)]
    return categories


def _kb_unregister_from_indexes(name: str, records: list[str], names: set) -> list[str]:
    """Remove the page's rows (and its marker) from every index page it is
    registered in; returns the categories it was in or proposed for."""
    categories = []
    for reg in _kb_registered_indexes(records):
        full = _kb_index_full(reg)
        text = _page_text_or_none(full)
        if text is None:
            continue
        if reg.startswith("week/"):
            categories += [p for _, p, _, _ in _kb_week_rows_for(text, name)]
            row_re = re.compile(rf"^- \[\[{re.escape(KB_PREFIX + name)}\]\] .*$")
            marker_re = _kb_marker_re(name)
            kept = [line for line in text.split("\n")
                    if not row_re.match(line) and not marker_re.match(line)]
            _kb_save_week_page_or_remove_when_empty(full, kept, names)
        else:
            categories.append(reg)
            prefix = f"- [[{KB_PREFIX}{name}]]"
            kept = [line for line in text.split("\n")
                    if not (line == prefix or line.startswith(prefix + " "))]
            sb.write(full, "\n".join(kept).encode("utf-8"))
    return categories


def _kb_link_deprecated_index_from_root(names: set):
    link = f"- [[{KB_DEPRECATED_INDEX[:-3]}]] 已廢止"
    if KB_DEPRECATED_INDEX not in names:
        sb.write(KB_DEPRECATED_INDEX, "# 已廢止\n\n".encode("utf-8"))
        names.add(KB_DEPRECATED_INDEX)
    exists = KB_ROOT_INDEX in names
    if exists and link in sb.read(KB_ROOT_INDEX).decode("utf-8").split("\n"):
        return
    _kb_append_lines(KB_ROOT_INDEX, exists, "# kb index\n\n", [link])
    names.add(KB_ROOT_INDEX)


def _kb_retire(target: str, status: str, reason: str, new_name: str | None,
               names: set) -> list[str]:
    """deprecate / replace: move kb/<target> to kb/deprecated/<target> with
    status, reason, replacement and original categories in its header, drop
    it from every index page, point every referrer at the new location
    (marked), list it in kb/index/deprecated. Returns report lines."""
    full = _kb_full(target)
    body, records = _kb_split_links(sb.read(full).decode("utf-8"))
    categories = _kb_unregister_from_indexes(target, records, names)
    title, _, rest = body.partition("\n")
    header = [title, f"{KB_STATUS_LINE}{status}", f"{KB_REASON_LINE}{reason}"]
    if new_name:
        header.append(f"{KB_REPLACED_BY_LINE}[[{KB_PREFIX}{new_name}]]")
    header += [f"{KB_ORIGINAL_INDEX_LINE}{c}" for c in categories]
    kept_records = [r for r in records if not r.startswith(KB_LINK_INDEX)]
    deprecated_full = f"{KB_DEPRECATED_PREFIX}{target}.md"
    sb.write(deprecated_full,
             _kb_join_links("\n".join(header) + "\n" + rest, kept_records).encode("utf-8"))
    sb.delete(full)
    names.discard(full)
    names.add(deprecated_full)
    extra = f" [[{KB_PREFIX}{new_name}]]" if new_name else ""
    for page in _kb_referrer_pages(records, exclude={full}):
        _kb_rewrite_links_on_page(page, target, deprecated_full[:-3], status, extra)
    _kb_link_deprecated_index_from_root(names)
    note = f"原分類：{', '.join(categories) or '（無）'}；原因：{reason}"
    if new_name:
        note += f"；取代：[[{KB_PREFIX}{new_name}]]"
    _kb_append_lines(KB_DEPRECATED_INDEX, True, "",
                     [f"- [[{deprecated_full[:-3]}]] #{status} — {note}"])
    return [f"  {status}: {KB_PREFIX}{target} → {deprecated_full[:-3]}"
            + (f" (取代：{KB_PREFIX}{new_name})" if new_name else ""),
            f"  referrers updated: {len(_kb_referrer_pages(records, exclude={full}))}"]


def _kb_merge(target: str, new_name: str, names: set) -> list[str]:
    """merge: the new page keeps its summaries / reasoning and gains both
    originals (each under 原文｜<id>) and the old page's link records; the
    old page is removed from every index and deleted; its referrers point at
    the new page (marked #merged). Returns report lines."""
    target_full, new_full = _kb_full(target), _kb_full(new_name)
    body_t, records_t = _kb_split_links(sb.read(target_full).decode("utf-8"))
    body_n, records_n = _kb_split_links(sb.read(new_full).decode("utf-8"))
    _kb_unregister_from_indexes(target, records_t, names)
    above_n, _, below_n = body_n.partition(f"\n{KB_SEPARATOR}\n")
    _, _, below_t = body_t.partition(f"\n{KB_SEPARATOR}\n")
    merged_body = (f"{above_n.rstrip(chr(10))}\n{KB_SEPARATOR}\n"
                   f"{KB_ORIGINAL_HEADING}{new_name}\n{below_n.strip(chr(10))}\n\n"
                   f"{KB_ORIGINAL_HEADING}{target}\n{below_t.strip(chr(10))}\n")
    link_to_target = f"{KB_LINK_OUT}[[{KB_PREFIX}{target}]]"
    link_from_new = f"{KB_LINK_IN}[[{KB_PREFIX}{new_name}]]"
    merged_records = [r for r in records_n if r != link_to_target]
    merged_records += [r for r in records_t
                       if not r.startswith(KB_LINK_INDEX) and r != link_from_new
                       and r not in merged_records]
    sb.write(new_full, _kb_join_links(merged_body, merged_records).encode("utf-8"))
    sb.delete(target_full)
    names.discard(target_full)
    referrers = _kb_referrer_pages(records_t, exclude={target_full, new_full})
    for page in referrers:
        _kb_rewrite_links_on_page(page, target, new_full[:-3], "merged")
    return [f"  merged: {KB_PREFIX}{target} → {KB_PREFIX}{new_name} (舊頁已移除)",
            f"  referrers updated: {len(referrers)}"]


def _kb_merge_target_error(merge_target, name: str, kind: str, summaries: list,
                           names: set) -> list[str]:
    """Format and compatibility checks for a proposed change to an existing page."""
    if not isinstance(merge_target, dict):
        return ["merge_target: 須為物件，如 {\"replace\": \"<知識 id>\", \"reason\": \"…\"}"]
    keys = [k for k in KB_MERGE_TARGET_KEYS if k in merge_target]
    extra = [k for k in merge_target if k not in KB_MERGE_TARGET_KEYS + ("reason",)]
    if len(keys) != 1 or extra:
        return [f"merge_target: 只能有 replace / deprecate / merge 其中一個鍵（可加 reason）"]
    key = keys[0]
    target = merge_target[key]
    reason = (merge_target.get("reason") or "").strip() if isinstance(
        merge_target.get("reason"), str) else ""
    errors = []
    if key == "merge" and merge_target.get("reason"):
        errors.append("merge_target.reason: merge 不帶原因")
    if key != "merge" and not reason:
        errors.append(f"merge_target.reason: {key} 必填原因")
    if kind == "source":
        return errors + ["merge_target: source 頁不能提修改建議"]
    if not isinstance(target, str) or target.startswith(KB_PREFIX) \
            or not KB_NAME_RE.match(target):
        return errors + [f"merge_target.{key}: 須為現有知識 id（不含 kb/）"]
    if target == name:
        return errors + [f"merge_target.{key}: 不能指向自己"]
    if _kb_full(target) not in names:
        return errors + [f"merge_target.{key}: {KB_PREFIX}{target} 不存在（或已廢止）"]
    change = _kb_changes_registered_against_pages(names).get(target)
    if change:
        return errors + [f"merge_target.{key}: {_kb_gone_note(target, *change)}，不能再指向它"]
    text = sb.read(_kb_full(target)).decode("utf-8")
    target_kind = _kb_page_kind(text)
    if not target_kind:
        return errors + [f"merge_target.{key}: {KB_PREFIX}{target} 是 source 頁，不能改動"]
    if key in ("replace", "merge") and target_kind != kind:
        errors.append(f"merge_target.{key}: 型態不同（{target_kind} ≠ {kind}），"
                      "只有同型態能 replace / merge")
    if key in ("replace", "merge"):
        _, records = _kb_split_links(text)
        target_categories = set(_kb_page_categories(target, records))
        mine = {item.get("index") for item in summaries if isinstance(item, dict)}
        if target_categories != mine:
            errors.append(f"merge_target.{key}: 分類不同（{', '.join(sorted(target_categories))} ≠ "
                          f"{', '.join(sorted(str(m) for m in mine))}），"
                          "同分類才叫 replace / merge")
    return errors


def _kb_check(name: str, kind: str, summaries: list, source: dict,
              original: str, reasoning: str, file: str, index: str,
              names: set) -> list[str]:
    errors = []
    if name.startswith(KB_PREFIX):
        errors.append(f"name: 不含 kb/ 前綴，請傳 {name[len(KB_PREFIX):]}")
    elif not KB_NAME_RE.match(name):
        errors.append("name: 須為 8 碼日期_小寫英數底線，"
                      "例 20261107_silverbullet_git_backup")
    else:
        try:
            datetime.strptime(name[:8], "%Y%m%d")
        except ValueError:
            errors.append("name: 前 8 碼不是合法日期")
        if _kb_full(name) in names:
            errors.append(f"name: {KB_PREFIX}{name} 已存在；要改它就用另一個 name 寫一頁新的，"
                          "並帶 merge_target 指向它")
    if kind not in KB_KINDS:
        errors.append(f"kind: 須為 {' / '.join(KB_KINDS)}")
        return errors
    if kind == "source":
        if not name.endswith("_source"):
            errors.append("name: kind=source 須以 _source 結尾")
        if not original.strip():
            errors.append("original: source 頁必填非空")
        if summaries:
            errors.append("summaries: source 頁不得帶")
        if source:
            errors.append("source: source 頁不得帶")
        if reasoning:
            errors.append("reasoning: source 頁不得帶")
        if file and (not file.startswith(KB_MEDIA_PREFIX)
                     or re.search(r"\s", file) or file == KB_MEDIA_PREFIX):
            errors.append(f"file: 須為 {KB_MEDIA_PREFIX}<檔名>，無空白")
        index_error = _kb_index_error(index)
        if index_error:
            errors.append(f"index: source 頁必填，登記在其知識的分類或最近共同母層；{index_error}")
        return errors
    if file:
        errors.append(f"file: 只有 source 頁能帶，{kind} 不得帶")
    if index:
        errors.append(f"index: {kind} 的分類寫在 summaries[].index，不用 index 參數")
    if name.endswith("_source"):
        errors.append(f"name: {kind} 不得以 _source 結尾")
    if original:
        errors.append(f"original: {kind} 不得帶，外部原文放 source.original")
    if kind == "decision" and not reasoning.strip():
        errors.append("reasoning: decision 必填非空（為何這樣定、否決了什麼）")
    if kind != "decision" and reasoning:
        errors.append(f"reasoning: 只有 decision 能帶，{kind} 不得帶")
    if not summaries:
        errors.append(f"summaries: {kind} 必填且非空")
    seen = set()
    for i, item in enumerate(summaries):
        label = f"summaries[{i}]"
        index = item.get("index") if isinstance(item, dict) else None
        text = _lf(item.get("text")) if isinstance(item, dict) else None
        index_error = _kb_index_error(index)
        if index_error:
            errors.append(f"{label}.index: {index_error}")
        elif index in seen:
            errors.append(f"{label}.index: {index} 重複")
        else:
            seen.add(index)
        if not isinstance(text, str) or not text.strip() \
                or "\n" in text.strip():
            errors.append(f"{label}.text: 須為非空單行")
        tags_error = (_kb_tags_error(item.get("tags") or [])
                      if isinstance(item, dict) else None)
        if tags_error:
            errors.append(f"{label}.tags: {tags_error}")
    stype = source.get("type")
    if stype == "page":
        page = source.get("page")
        if not isinstance(page, str) or not page.strip():
            errors.append("source.page: 必填")
        elif page.strip().startswith(KB_SOURCE_PAGE_FORBIDDEN):
            errors.append("source.page: 不得指向 kb/、Archive/、Library/")
        elif norm(page) not in names:
            errors.append(f"source.page: {page} 不存在")
    elif stype == "text":
        text = source.get("original")
        if not isinstance(text, str) or not text.strip():
            errors.append("source.original: 必填非空")
    elif stype == "source":
        source_name = source.get("name")
        if not isinstance(source_name, str) \
                or not source_name.endswith("_source"):
            errors.append("source.name: 須以 _source 結尾")
        elif _kb_full(source_name) not in names:
            errors.append(f"source.name: {KB_PREFIX}{source_name} 不存在，"
                          "先用 kind=source 寫入")
    else:
        errors.append("source.type: 須為 page、text 或 source")
    return errors


def _kb_knowledge_page(name: str, kind: str, summaries: list, source: dict,
                       reasoning: str) -> str:
    lines = [f"# {name}", f"{KB_KIND_LINE}{kind}", ""]
    for item in summaries:
        lines += [f"{KB_SUMMARY_HEADING}{item['index']}",
                  _lf(item["text"]).strip(), ""]
    if reasoning:
        lines += [KB_REASONING_HEADING, reasoning.strip("\n"), ""]
    lines += [KB_SEPARATOR, ""]
    if source["type"] == "page":
        full = norm(source["page"])
        link = full[:-3] if full.endswith(".md") else full
        lines.append(f"來源：[[{link}]]")
    elif source["type"] == "source":
        lines.append(f"原文：[[{KB_PREFIX}{source['name']}]]")
    else:
        lines.append(_lf(source["original"]).strip("\n"))
    return "\n".join(lines) + "\n"


def _kb_append_lines(full: str, exists: bool, header: str, lines: list):
    current = sb.read(full).decode("utf-8") if exists else header
    if current and not current.endswith("\n"):
        current += "\n"
    sb.write(full, (current + "\n".join(lines) + "\n").encode("utf-8"))


@mcp.tool
def kb_write(name: str, kind: str, summaries: list[dict] | None = None,
             source: dict | None = None, original: str = "",
             reasoning: str = "", file: str = "", index: str = "",
             merge_target: dict | None = None, readme: str = "") -> str:
    """Write ONE kb page. Parameters and procedure: readme(), 知識庫
    section."""
    _raise_when_readme_version_is_not_current("kb_write", readme)
    name = name.strip()
    original = _normalize_lf(original or "")
    reasoning = _normalize_lf(reasoning or "")
    summaries = summaries or []
    for item in summaries:
        if isinstance(item, dict):
            item["tags"] = _kb_lower_tags(item.get("tags") or [])
            item["index"] = _kb_lower_index(item.get("index"))
    source = source or {}
    names = {f["name"] for f in sb.listing()}
    file = (file or "").strip()
    index = _kb_lower_index(index or "")
    errors = _kb_check(name, kind, summaries, source, original, reasoning,
                       file, index, names)
    if merge_target is not None and not errors:
        errors += _kb_merge_target_error(merge_target, name, kind, summaries, names)
    if errors:
        raise ValueError("kb_write 拒收：\n" + "\n".join(f"- {e}"
                                                      for e in errors))
    full = _kb_full(name)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M%z")
    week = _kb_week_of(datetime.strptime(name[:8], "%Y%m%d"))
    week_full = f"{KB_WEEK_PREFIX}{week}.md"
    records = [f"{KB_LINK_INDEX}week/{week}"]
    if kind == "source":
        file_line = f"{KB_FILE_LINE}{file}\n" if file else ""
        sb.write(full, _kb_join_links(f"# {name}\n{file_line}\n{original.strip(chr(10))}\n",
                                      records).encode("utf-8"))
        label = file or next((line.strip() for line in original.split("\n")
                              if line.strip()), name)[:60]
        _kb_append_lines(week_full, week_full in names, f"# {week} 待審\n\n",
                         [f"- [[{KB_PREFIX}{name}]] #source → {index} — 原文：{label}"])
        _kb_link_week_from_root_index(week, names)
        _kb_append_lines(KB_LOG, KB_LOG in names, "# kb log\n\n",
                         [f"- {stamp}  write(source)  [[{KB_PREFIX}{name}]]  → {index}"])
        git_commit_async(f"kb_write {full} (main)")
        return f"OK {full} (source page)"
    if source["type"] == "source":
        records.append(f"{KB_LINK_OUT}[[{KB_PREFIX}{source['name']}]]")
        _kb_add_link_records(_kb_full(source["name"]), f"{KB_LINK_IN}[[{full[:-3]}]]")
    marker = None
    if merge_target is not None:
        key = next(k for k in KB_MERGE_TARGET_KEYS if k in merge_target)
        target = merge_target[key]
        records.append(f"{KB_LINK_OUT}[[{KB_PREFIX}{target}]]")
        _kb_add_link_records(_kb_full(target), f"{KB_LINK_IN}[[{full[:-3]}]]")
        marker = _kb_marker_line(name, key, target,
                                 (merge_target.get("reason") or "").strip())
    sb.write(full, _kb_join_links(_kb_knowledge_page(name, kind, summaries, source,
                                                     reasoning), records).encode("utf-8"))
    rows = [f"- [[{KB_PREFIX}{name}]] #{kind} → {item['index']}"
            f"{_kb_tag_suffix(item['tags'])} — "
            f"{_lf(item['text']).strip()}" for item in summaries]
    if marker:
        rows.append(marker)
    _kb_append_lines(week_full, week_full in names, f"# {week} 待審\n\n", rows)
    _kb_link_week_from_root_index(week, names)
    indexes = ", ".join(item["index"] for item in summaries)
    _kb_append_lines(KB_LOG, KB_LOG in names, "# kb log\n\n",
                     [f"- {stamp}  write({kind})  [[{KB_PREFIX}{name}]]  "
                      f"→ {indexes}" + (f"  {marker.strip()[2:]}" if marker else "")])
    git_commit_async(f"kb_write {full} (main)")
    return f"OK {full}" + (" (+ merge_target)" if marker else "")


def _kb_link_week_from_root_index(week: str, names: set):
    """Every page must be reachable from the root index before review too:
    make sure kb/index exists and links this week's staging page (once)."""
    link = f"- [[{KB_WEEK_PREFIX}{week}]] 待審"
    exists = KB_ROOT_INDEX in names
    if exists and link in sb.read(KB_ROOT_INDEX).decode("utf-8").split("\n"):
        return
    _kb_append_lines(KB_ROOT_INDEX, exists, "# kb index\n\n", [link])
    names.add(KB_ROOT_INDEX)


def _kb_save_week_page_or_remove_when_empty(week_full: str, kept: list[str],
                                            names: set):
    """Write the staging page back; once nothing is left to review, delete
    the page and drop its line from the root index (kb_write recreates both
    when the next row arrives)."""
    if _kb_has_pending(kept):
        sb.write(week_full, "\n".join(kept).encode("utf-8"))
        return
    sb.delete(week_full)
    names.discard(week_full)
    link = f"- [[{week_full[:-3]}]] 待審"
    lines = sb.read(KB_ROOT_INDEX).decode("utf-8").split("\n")
    if link in lines:
        lines.remove(link)
        sb.write(KB_ROOT_INDEX, "\n".join(lines).encode("utf-8"))


def _kb_create_index_and_parents(index: str, names: set) -> list[str]:
    """Create kb/index/<index> and every missing ancestor, top down,
    registering each new page in its parent (a top-level category in the
    root kb/index). `names` is updated in place. Returns the pages created."""
    parts = index.split("/")
    created = []
    for depth in range(1, len(parts) + 1):
        part = "/".join(parts[:depth])
        full = _kb_index_full(part)
        if full in names:
            continue
        sb.write(full, f"# {part}\n\n".encode("utf-8"))
        names.add(full)
        parent = (_kb_index_full("/".join(parts[:depth - 1])) if depth > 1
                  else KB_ROOT_INDEX)
        _kb_append_lines(parent, parent in names, "# kb index\n\n",
                         [f"- [[{full[:-3]}]]"])
        names.add(parent)
        created.append(full)
    return created


def _kb_append_tags_to_index_and_ancestor_lines(index: str,
                                                tags: list) -> list[str]:
    """Routing tags by level: the line that links kb/index/<index> in its
    parent page gets the page tags; each ancestor's line (up to the root
    kb/index) gets only the NAME of the child category below it, so a
    parent row says which sub-categories exist instead of accumulating
    every leaf tag. Missing tags are appended, nothing is removed. Returns
    one report line per page changed."""
    parts = index.split("/")
    report = []
    for depth in range(len(parts), 0, -1):
        link = f"- [[{_kb_index_full('/'.join(parts[:depth]))[:-3]}]]"
        parent = (_kb_index_full("/".join(parts[:depth - 1])) if depth > 1
                  else KB_ROOT_INDEX)
        wanted = tags if depth == len(parts) else [parts[depth].lower()]
        lines = sb.read(parent).decode("utf-8").split("\n")
        hits = [i for i, line in enumerate(lines)
                if line == link or line.startswith(link + " ")]
        if len(hits) != 1:
            raise ValueError(f"{parent[:-3]} 沒有唯一的 {link} 行")
        present = set(re.findall(r" #(\S+)", lines[hits[0]]))
        missing = [t for t in wanted if t not in present]
        if not missing:
            continue
        lines[hits[0]] += _kb_tag_suffix(missing)
        sb.write(parent, "\n".join(lines).encode("utf-8"))
        report.append(f"  tags {parent[:-3]}: "
                      + " ".join(f"+#{t}" for t in missing))
    return report


@mcp.tool
def kb_append_index_tags(index: str, tags: list[str]) -> str:
    """Add routing #tags to an existing kb category. When and how:
    readme(), 知識庫 section."""
    index = _kb_lower_index(index)
    tags = _kb_lower_tags(tags)
    errors = []
    index_error = _kb_index_error(index)
    if index_error:
        errors.append(f"index: {index_error}")
    tags_error = _kb_tags_error(tags)
    if tags_error:
        errors.append(f"tags: {tags_error}")
    elif not tags:
        errors.append("tags: 不得為空")
    names = {f["name"] for f in sb.listing()}
    if not index_error and _kb_index_full(index) not in names:
        errors.append(f"index: {KB_INDEX_PREFIX}{index} 不存在")
    if errors:
        raise ValueError("kb_append_index_tags 拒收：\n"
                         + "\n".join(f"- {e}" for e in errors))
    report = _kb_append_tags_to_index_and_ancestor_lines(index, tags)
    if not report:
        return f"OK {KB_INDEX_PREFIX}{index} (no new tags)"
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M%z")
    _kb_append_lines(KB_LOG, KB_LOG in names, "# kb log\n\n",
                     [f"- {stamp}  tags  {KB_INDEX_PREFIX}{index}  "
                      + " ".join(f"+#{t}" for t in tags)])
    git_commit_async(f"kb_append_index_tags {KB_INDEX_PREFIX}{index} (main)")
    return "\n".join([f"OK {KB_INDEX_PREFIX}{index}"] + report)


def kb_create_index(index: str) -> str:
    """Review side (internal endpoint): create a category and any missing
    parents."""
    index = _kb_lower_index(index)
    index_error = _kb_index_error(index)
    if index_error:
        raise ValueError(f"kb_create_index 拒收：index {index_error}")
    names = {f["name"] for f in sb.listing()}
    if _kb_index_full(index) in names:
        raise ValueError(f"kb_create_index 拒收：{KB_INDEX_PREFIX}{index} 已存在")
    created = _kb_create_index_and_parents(index, names)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M%z")
    _kb_append_lines(KB_LOG, KB_LOG in names, "# kb log\n\n",
                     [f"- {stamp}  create  {', '.join(c[:-3] for c in created)}"])
    git_commit_async(f"kb_create_index {KB_INDEX_PREFIX}{index} (main)")
    return "OK created:\n" + "\n".join(f"  {c[:-3]}" for c in created)


def kb_move_pending_to_index(name: str,
                             final_index_by_proposed: dict | None = None
                             ) -> str:
    """Review side (internal endpoint): move one page's staging rows into
    their categories and carry out the change it registered (replace /
    deprecate / merge), or the standalone retirement registered against it."""
    name = _kb_bare_name(name, "kb_move_pending_to_index")
    final_index_by_proposed = {proposed: _kb_lower_index(final) for proposed, final
                               in (final_index_by_proposed or {}).items()}
    errors = []
    names = {f["name"] for f in sb.listing()}
    full = _kb_full(name)
    if full not in names:
        raise ValueError(f"kb_move_pending_to_index 拒收：\n- name: {KB_PREFIX}{name} 不存在")
    week = _kb_week_of(datetime.strptime(name[:8], "%Y%m%d"))
    week_full = f"{KB_WEEK_PREFIX}{week}.md"
    week_text = sb.read(week_full).decode("utf-8") if week_full in names else ""
    line_re = re.compile(rf"^- \[\[{re.escape(KB_PREFIX + name)}\]\] "
                         rf"(#\S+) → (\S+)((?: #\S+)*) — (.*)$", re.MULTILINE)
    pending = line_re.findall(week_text)
    retirement = _kb_find_deprecate_marker(name, names)
    if not pending and not retirement:
        raise ValueError(f"kb_move_pending_to_index 拒收：\n- "
                         f"{KB_PREFIX}{name} 沒有待審行,也沒有作廢標記")
    marker_re = _kb_marker_re(name)
    marker = marker_re.search(week_text) if pending else None
    if marker and _kb_full(marker.group(2)) not in names:
        errors.append(f"merge_target: {KB_PREFIX}{marker.group(2)} 已不存在"
                      "（先前已被廢止或合併），這頁的修改建議無法執行；"
                      "請使用者決定後改待審頁上的 ⇒ 行")
    proposed_all = [p for _, p, _, _ in pending]
    for proposed, final in final_index_by_proposed.items():
        if proposed not in proposed_all:
            errors.append(f"final_index_by_proposed: 提議分類 {proposed} "
                          "不在待審行裡")
        index_error = _kb_index_error(final)
        if index_error:
            errors.append(f"final_index_by_proposed[{proposed}]: {index_error}")
    targets = [final_index_by_proposed.get(p, p) for p in proposed_all]
    if len(set(targets)) != len(targets):
        errors.append("final_index_by_proposed: 最終分類重複")
    if errors:
        raise ValueError("kb_move_pending_to_index 拒收：\n"
                         + "\n".join(f"- {e}" for e in errors))
    created = []
    report = []
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M%z")
    if pending:
        # source pages carry no 總結 heading, so only knowledge-type rows retitle
        retitled = [(row[1], t) for row, t in zip(pending, targets)
                    if row[1] != t and row[0] != "#source"]
        if retitled:
            page_text = sb.read(full).decode("utf-8")
            for proposed, target in retitled:
                heading = f"{KB_SUMMARY_HEADING}{proposed}\n"
                if page_text.count(heading) != 1:
                    raise ValueError(f"kb_move_pending_to_index 拒收：\n- "
                                     f"{KB_PREFIX}{name} 沒有唯一的 "
                                     f"{heading.strip()} 標題")
                page_text = page_text.replace(
                    heading, f"{KB_SUMMARY_HEADING}{target}\n")
            sb.write(full, page_text.encode("utf-8"))
        for (kind_tag, proposed, tag_suffix, summary), target \
                in zip(pending, targets):
            created += _kb_create_index_and_parents(target, names)
            _kb_append_lines(_kb_index_full(target), True, "",
                             [f"- [[{KB_PREFIX}{name}]] {kind_tag} — {summary}"])
            report.append(f"  moved: {proposed} → {target}"
                          if target != proposed else f"  moved: {target}")
            tags = _kb_lower_tags([t[1:] for t in tag_suffix.split()])
            if tags:
                report += _kb_append_tags_to_index_and_ancestor_lines(target,
                                                                      tags)
        kept = [line for line in week_text.split("\n")
                if not line_re.fullmatch(line) and not marker_re.fullmatch(line)]
        _kb_save_week_page_or_remove_when_empty(week_full, kept, names)
        _kb_remove_link_records(full, lambda r: r == f"{KB_LINK_INDEX}week/{week}")
        _kb_add_link_records(full, *[f"{KB_LINK_INDEX}{t}" for t in targets])
        _kb_append_lines(KB_LOG, KB_LOG in names, "# kb log\n\n",
                         [f"- {stamp}  move  [[{KB_PREFIX}{name}]]  → "
                          f"{', '.join(targets)}"])
        if marker:
            key, change_target, reason = marker.group(1), marker.group(2), marker.group(3) or ""
            if key == "merge":
                report += _kb_merge(change_target, name, names)
            else:
                report += _kb_retire(change_target, "replaced" if key == "replace" else "deprecated",
                                     reason, name if key == "replace" else None, names)
            _kb_append_lines(KB_LOG, True, "",
                             [f"- {stamp}  {key}  [[{KB_PREFIX}{change_target}]]  by [[{KB_PREFIX}{name}]]"
                              + (f"  — {reason}" if reason else "")])
        report.append(f"  week: {week_full[:-3]} (-{len(pending)} lines)")
    if retirement:
        marker_week_full, found = retirement
        reason = found.group(1)
        report += _kb_retire(name, "deprecated", reason, None, names)
        lines = sb.read(marker_week_full).decode("utf-8").split("\n")
        kept = [line for line in lines if not _kb_deprecate_marker_re(name).fullmatch(line)]
        _kb_save_week_page_or_remove_when_empty(marker_week_full, kept, names)
        _kb_append_lines(KB_LOG, KB_LOG in names, "# kb log\n\n",
                         [f"- {stamp}  deprecate  [[{KB_PREFIX}{name}]]  — {reason}"])
    git_commit_async(f"kb_move_pending_to_index {full} (main)")
    if created:
        report.append("  created: " + ", ".join(c[:-3] for c in created))
    return "\n".join([f"OK {full}"] + report)


@mcp.tool
def kb_deprecate(name: str, reason: str, readme: str = "") -> str:
    """Retire one existing kb page with nothing replacing it. Parameters and
    procedure: readme(), 知識庫 section."""
    _raise_when_readme_version_is_not_current("kb_deprecate", readme)
    name = _kb_bare_name(name, "kb_deprecate")
    reason = (reason or "").strip()
    names = {f["name"] for f in sb.listing()}
    errors = []
    if _kb_full(name) not in names:
        errors.append(f"name: {KB_PREFIX}{name} 不存在（或已廢止）")
    elif not _kb_page_kind(sb.read(_kb_full(name)).decode("utf-8")):
        errors.append(f"name: {KB_PREFIX}{name} 是 source 頁，不能廢止")
    if not reason:
        errors.append("reason: 必填")
    change = _kb_changes_registered_against_pages(names).get(name)
    if change:
        errors.append(f"name: {_kb_gone_note(name, *change)}")
    if errors:
        raise ValueError("kb_deprecate 拒收：\n" + "\n".join(f"- {e}" for e in errors))
    week = _kb_week_of(datetime.now().astimezone())
    week_full = f"{KB_WEEK_PREFIX}{week}.md"
    _kb_append_lines(week_full, week_full in names, f"# {week} 待審\n\n",
                     [_kb_deprecate_marker_line(name, reason)])
    _kb_link_week_from_root_index(week, names)
    stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M%z")
    _kb_append_lines(KB_LOG, KB_LOG in names, "# kb log\n\n",
                     [f"- {stamp}  deprecate-proposed  [[{KB_PREFIX}{name}]]  — {reason}"])
    git_commit_async(f"kb_deprecate {_kb_full(name)} (main)")
    return f"OK {KB_PREFIX}{name}"


def kb_register_backlink(name: str, from_page: str) -> str:
    """Record on kb/<name> that the SilverBullet page `from_page` (path
    without .md, e.g. "index") links to it, so later changes to the page can
    update that link."""
    name = _kb_bare_name(name, "kb_register_backlink")
    from_page = (from_page or "").strip()
    if not from_page or from_page.endswith(".md"):
        raise ValueError("kb_register_backlink 拒收：from_page 須為頁名（不含 .md）")
    full = _kb_full(name)
    if _page_text_or_none(full) is None:
        raise ValueError(f"kb_register_backlink 拒收：{KB_PREFIX}{name} 不存在")
    _kb_add_link_records(full, f"{KB_LINK_IN}[[{from_page}]]")
    return f"OK {KB_PREFIX}{name} ← [[{from_page}]]"


def _kb_above_and_below_separator(text: str) -> tuple[str, str]:
    """Page text above and below the separator (link records stripped); a
    page without a separator (a source page) is all 'above'."""
    body, _ = _kb_split_links(text)
    above, _, below = body.partition(f"\n{KB_SEPARATOR}\n")
    return above, below


def _kb_split(name: str) -> tuple[str, str]:
    """What a caller may read of the page: (above, below) the separator. A
    page that was retired — or has a retirement / replacement / merge
    registered against it — is refused with one and the same wording. A page
    with a merge of its own registered gets the other page's original
    appended, as it will be stored once the merge is carried out."""
    name = _kb_bare_name(name, "kb_read")
    text = _page_text_or_none(_kb_full(name))
    if text is None:
        retired = _page_text_or_none(f"{KB_DEPRECATED_PREFIX}{name}.md")
        if retired is None:
            raise ValueError(f"kb_read 拒收：{KB_PREFIX}{name} 不存在")
        head = {line.split("：", 1)[0]: line.split("：", 1)[1]
                for line in retired.split("\n")[1:6] if "：" in line}
        new_name = head.get(KB_REPLACED_BY_LINE[:-1], "").strip("[]")[len(KB_PREFIX):]
        raise ValueError("kb_read 拒收：" + _kb_gone_note(
            name, "replace" if new_name else "deprecate", new_name or None,
            head.get(KB_REASON_LINE[:-1], "")))
    names = {f["name"] for f in sb.listing()}
    change = _kb_changes_registered_against_pages(names).get(name)
    if change:
        raise ValueError("kb_read 拒收：" + _kb_gone_note(name, *change))
    above, below = _kb_above_and_below_separator(text)
    for week_full in (n for n in names if n.startswith(KB_WEEK_PREFIX)):
        own = _kb_marker_re(name).search(sb.read(week_full).decode("utf-8"))
        if own and own.group(1) == "merge":
            _, absorbed = _kb_above_and_below_separator(
                _page_text_or_none(_kb_full(own.group(2))) or "")
            below = (f"{KB_ORIGINAL_HEADING}{name}\n{below.strip(chr(10))}\n\n"
                     f"{KB_ORIGINAL_HEADING}{own.group(2)}\n{absorbed.strip(chr(10))}\n")
    return above, below


@mcp.tool
def kb_read(name: str) -> str:
    """Read a kb page's summaries. See readme(), 知識庫 section."""
    above, _ = _kb_split(name)
    return above


@mcp.tool
def kb_read_original(name: str) -> str:
    """Read a kb page's original material. See readme(), 知識庫 section."""
    above, below = _kb_split(name)
    return below or above


@mcp.tool
def kb_index() -> str:
    """The kb entry point: root index, existing categories, recently added
    pages. See readme(), 知識庫 section."""
    names = sorted(f["name"] for f in sb.listing()
                   if f["name"].startswith(KB_INDEX_PREFIX) and f["name"].endswith(".md"))
    categories = [n[len(KB_INDEX_PREFIX):-3] for n in names
                  if not n.startswith(KB_WEEK_PREFIX) and n != KB_DEPRECATED_INDEX]
    gone = _kb_changes_registered_against_pages(set(names))
    recent = []
    for n in names:
        if not n.startswith(KB_WEEK_PREFIX):
            continue
        for line in (_page_text_or_none(n) or "").split("\n"):
            row = re.match(r"- \[\[kb/([^\]]+)\]\] ", line)
            if row and row.group(1) not in gone:
                recent.append(line)
    root = [line for line in (_page_text_or_none(KB_ROOT_INDEX) or "(kb/index 不存在)").split("\n")
            if not line.startswith(f"- [[{KB_WEEK_PREFIX}")]
    return "\n".join([
        f"{README_VERSION_LABEL}{_readme_text_and_version()[1]}（和你讀到的不同就重新呼叫 readme()）",
        f"=== {KB_ROOT_INDEX[:-3]} ===",
        "\n".join(root),
        "=== 現有分類（分類路徑；對應頁是 kb/index/<分類>；寫入時知識頁填在 summaries[].index、source 頁填 index）===",
        "\n".join(categories) or "(尚無分類)",
        "=== 近期新增的頁（分類頁上還看不到，找知識時這裡也要比對；→ 後面是它的分類）===",
        "\n".join(recent) or "(沒有)",
    ])


FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
SHARE_FLAG_RE = re.compile(r"^share:\s*(?:true|edit)\s*$", re.MULTILINE)
EDIT_FLAG_RE = re.compile(r"^share:\s*edit\s*$", re.MULTILINE)
WIDGET_LINE_RE = re.compile(r"^\$\{[^\n]*\}[ \t]*\n?", re.MULTILINE)
WIKILINK_RE = re.compile(r"\[\[([^\]|]+)\|([^\]]+)\]\]|\[\[([^\]]+)\]\]")

SHARE_TEMPLATE = """<!doctype html>
<html><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>__TITLE__</title>
<style>
  /* 淺色=blackbeauty 的日間版(同色彩語彙,紙感底) */
  :root {
    --bg: #fbfaf6; --fg: #1c1c1c; --muted: #6b6b6b;
    --heading: #a020a0; --link: #2f5fd0; --accent: #9a7d00;
    --code-bg: #f0efe9; --border: #d8d6cf;
    --blockquote: #4a4a8a; --table-head: #f0efe9;
    --diff-add: #2e7d32; --diff-del: #b03a3a; --diff-hunk: #4a68c8;
  }
  /* 深色=blackbeauty 本尊 */
  html[data-theme="dark"] {
    --bg: #000000; --fg: #f0f0f0; --muted: #9a9a9a;
    --heading: #ff00ff; --link: #80a0ff; --accent: #ffff60;
    --code-bg: #141414; --border: #333333;
    --blockquote: #b0b0ff; --table-head: #101010;
    --diff-add: #60ff60; --diff-del: #ffa0a0; --diff-hunk: #80a0ff;
  }
  body { margin: 0; font-family: system-ui, -apple-system, "Segoe UI",
         "Noto Sans TC", sans-serif; line-height: 1.65;
         background: var(--bg); color: var(--fg);
         transition: background .15s, color .15s; }
  main { max-width: 720px; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }
  h1, h2, h3 { line-height: 1.3; color: var(--heading); }
  a { color: var(--link); }
  pre { overflow-x: auto; padding: .75rem; border-radius: 6px;
        background: var(--code-bg); }
  code { background: var(--code-bg); padding: .1em .3em; border-radius: 4px; }
  pre code { padding: 0; }
  table { border-collapse: collapse; display: block; overflow-x: auto; }
  th { background: var(--table-head); }
  th, td { border: 1px solid var(--border); padding: .35rem .6rem; }
  blockquote { margin-left: 0; padding-left: 1rem;
               border-left: 3px solid var(--border); color: var(--blockquote); }
  #theme-toggle { position: fixed; top: .75rem; right: .75rem;
    cursor: pointer; border: 1px solid var(--border);
    background: var(--code-bg); color: var(--fg);
    border-radius: 6px; padding: .25rem .6rem; font-size: 1rem;
    line-height: 1.4; }
  #theme-toggle:hover { color: var(--accent); border-color: var(--accent); }
  .d-add { color: var(--diff-add); }
  .d-del { color: var(--diff-del); }
  .d-hunk { color: var(--diff-hunk); }
  details { margin: .5rem 0; }
  summary { cursor: pointer; color: var(--heading); }
</style>
</head><body>
<button id="theme-toggle" aria-label="切換深淺主題">◐</button>
<main>__BODY__</main>
<script>
(function () {
  var root = document.documentElement;
  var saved = null;
  try { saved = localStorage.getItem("sb-share-theme"); } catch (e) {}
  var sys = window.matchMedia("(prefers-color-scheme: dark)").matches
    ? "dark" : "light";
  root.setAttribute("data-theme", saved || sys);
  document.getElementById("theme-toggle").onclick = function () {
    var next = root.getAttribute("data-theme") === "dark" ? "light" : "dark";
    root.setAttribute("data-theme", next);
    try { localStorage.setItem("sb-share-theme", next); } catch (e) {}
  };
})();
</script>
</body></html>"""


def render_share(title: str, body: str) -> str:
    return (SHARE_TEMPLATE
            .replace("__TITLE__", title)
            .replace("__BODY__", body))


def _shared_flag(target: str) -> bool:
    full = norm(target)
    if not full.endswith(".md") or full.startswith("Library/"):
        return False
    try:
        text = sb.read(full).decode("utf-8")
    except (httpx.HTTPStatusError, UnicodeDecodeError):
        return False
    fm = FRONTMATTER_RE.match(text)
    return bool(fm and SHARE_FLAG_RE.search(fm.group(1)))


def share_page(request):
    supplied = request.path_params["secret"]
    if not SHARE_SECRET or not hmac.compare_digest(supplied, SHARE_SECRET):
        return PlainTextResponse("Not Found", status_code=404)
    full = norm(request.path_params["page"])
    if (not full.endswith(".md") or full.startswith("Library/")
            or full.startswith(ARCHIVE_PREFIX)):
        return PlainTextResponse("Not Found", status_code=404)
    try:
        text = sb.read(full).decode("utf-8")
    except (httpx.HTTPStatusError, UnicodeDecodeError):
        return PlainTextResponse("Not Found", status_code=404)
    fm = FRONTMATTER_RE.match(text)
    if not fm or not SHARE_FLAG_RE.search(fm.group(1)):
        return PlainTextResponse("Not Found", status_code=404)
    body_md = FRONTMATTER_RE.sub("", text, count=1)
    body_md = WIDGET_LINE_RE.sub("", body_md)

    # wikilinks: targets that are themselves shared link to their share view,
    # everything else flattens to plain text (no dead links, no leaks)
    shared_cache: dict[str, bool] = {}

    def link_repl(m):
        target = (m.group(1) or m.group(3)).strip()
        alias = m.group(2) or m.group(3)
        if target not in shared_cache:
            shared_cache[target] = _shared_flag(target)
        if shared_cache[target]:
            href = f"/share/{SHARE_SECRET}/{quote(target, safe='/')}"
            return f"[{alias}]({href})"
        return alias

    body_md = WIKILINK_RE.sub(link_repl, body_md)
    body_html = markdown.markdown(body_md,
                                  extensions=["tables", "fenced_code"])
    title = html.escape(full[:-3].rsplit("/", 1)[-1])
    page_html = render_share(title, body_html)
    return HTMLResponse(page_html, headers={"Cache-Control": "no-store"})


ally = FastMCP(
    name="SilverBulletAlly",
    instructions=(
        "Collaboration endpoint for shared pages in the owner's note "
        "space. Only pages whose frontmatter contains `share: edit` are "
        "visible here — everything else does not exist for you. "
        "FIRST ACTION in every session: read_page the page the owner "
        "named and follow the format spec written in that page. To add "
        "an entry use append_page with the section parameter (the text "
        "of a `## ` heading) — it inserts at the top of that section and "
        "verifies the write. Use write_page only to fix structure, and "
        "keep the frontmatter intact. A write succeeded if and only if "
        "the reply contains 'verified'."
    ),
)


def _editable_text(path: str) -> tuple[str, str]:
    full = norm(path)
    if full.startswith(ARCHIVE_PREFIX):
        raise ValueError(f"{path}: not found or not opened for collaboration")
    try:
        text = sb.read(full).decode("utf-8")
    except (httpx.HTTPStatusError, UnicodeDecodeError):
        raise ValueError(f"{path}: not found or not opened for collaboration")
    fm = FRONTMATTER_RE.match(text)
    if not fm or not EDIT_FLAG_RE.search(fm.group(1)):
        raise ValueError(f"{path}: not found or not opened for collaboration")
    return full, text


def _normalize_lf(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _write_verified(full: str, text: str):
    body = text.encode("utf-8")
    sb.write(full, body)
    sizes = {f["name"]: f["size"] for f in sb.listing()}
    if sizes.get(full) != len(body):
        raise RuntimeError(f"wrote {full} but verification failed "
                           "(size mismatch in listing)")


@ally.tool
def read_page(path: str) -> str:
    """Read a collaboration page (only pages opened with `share: edit`
    exist here)."""
    return _editable_text(path)[1]


@ally.tool
def append_page(path: str, text: str, section: str = "") -> str:
    """Add an entry to a collaboration page and verify the write.
    With `section`, the text is inserted as the FIRST line under that
    `## section` heading — use this for dated entries.
    Without `section`, the text is appended to the end of the page.
    Safe for concurrent updates."""
    full, current = _editable_text(path)
    text = _normalize_lf(text).strip("\n")
    if section:
        lines = current.split("\n")
        wanted = f"## {section.strip()}"
        idx = next((i for i, line in enumerate(lines)
                    if line.strip() == wanted), None)
        if idx is None:
            heads = [line.strip()[3:] for line in lines
                     if line.strip().startswith("## ")]
            raise ValueError(f"section '{section}' not found — "
                             f"available sections: {heads}")
        j = idx + 1
        while j < len(lines) and not lines[j].strip():
            j += 1
        lines[j:j] = [text]
        new_text = "\n".join(lines)
    else:
        new_text = current if current.endswith("\n") else current + "\n"
        new_text += text + "\n"
    _write_verified(full, new_text)
    git_commit_async(f"append_page {full} (ally)")
    where = (f"top of section '## {section.strip()}'" if section
             else "end of page")
    return (f"OK {full} (+{len(text.encode('utf-8'))} bytes at {where}, "
            "verified)")


@ally.tool
def write_page(path: str, text: str) -> str:
    """REPLACE a whole collaboration page: read_page first, modify, write
    back the full text. The text must keep the page frontmatter
    (`share: edit`) or the write is rejected."""
    full, _ = _editable_text(path)
    text = _normalize_lf(text)
    fm = FRONTMATTER_RE.match(text)
    if not fm or not EDIT_FLAG_RE.search(fm.group(1)):
        raise ValueError("rejected: text must keep the page frontmatter "
                         "block containing `share: edit` — read_page, "
                         "reapply your change and retry")
    _write_verified(full, text)
    git_commit_async(f"write_page {full} (ally)")
    return f"OK {full} ({len(text.encode('utf-8'))} bytes, verified)"


async def edit_endpoint(request):
    supplied = request.path_params["secret"]
    if not ALLY_SECRET or not hmac.compare_digest(supplied, ALLY_SECRET):
        return PlainTextResponse("Not Found", status_code=404)
    try:
        full, text = _editable_text(request.path_params["page"])
    except ValueError:
        return PlainTextResponse("Not Found", status_code=404)
    if request.method == "GET":
        return PlainTextResponse(text,
                                 media_type="text/markdown; charset=utf-8")
    raw = _normalize_lf((await request.body()).decode("utf-8"))
    fm = FRONTMATTER_RE.match(raw)
    if not fm or not EDIT_FLAG_RE.search(fm.group(1)):
        return PlainTextResponse(
            "rejected: content must keep the frontmatter block "
            "containing `share: edit`", status_code=400)
    _write_verified(full, raw)
    git_commit_async(f"PUT {full} (edit)")
    return PlainTextResponse(f"OK {full} ({len(raw.encode('utf-8'))} bytes, "
                             "verified)")


def _colorize_diff(patch: str) -> str:
    out = []
    for line in patch.splitlines():
        esc = html.escape(line)
        if line.startswith("+") and not line.startswith("+++"):
            out.append(f'<span class="d-add">{esc}</span>')
        elif line.startswith("-") and not line.startswith("---"):
            out.append(f'<span class="d-del">{esc}</span>')
        elif line.startswith("@@"):
            out.append(f'<span class="d-hunk">{esc}</span>')
        else:
            out.append(esc)
    return "\n".join(out)


def history_page(request):
    """Read-only web view of a page's git history (timeline + colored
    diffs), guarded by the share secret."""
    supplied = request.path_params["secret"]
    if not SHARE_SECRET or not hmac.compare_digest(supplied, SHARE_SECRET):
        return PlainTextResponse("Not Found", status_code=404)
    full = norm(request.path_params["page"])
    if (not full.endswith(".md") or full.startswith("Library/")
            or full.startswith(ARCHIVE_PREFIX)):
        return PlainTextResponse("Not Found", status_code=404)
    if not GIT_ENABLED:
        return PlainTextResponse("history disabled", status_code=404)
    raw = _git("log", "-p", "-20", "--date=format:%Y-%m-%d %H:%M",
               "--pretty=format:\x01%h\x09%ad\x09%s", "--", full)
    if not raw.strip():
        return PlainTextResponse("Not Found", status_code=404)
    sections = []
    for block in raw.split("\x01"):
        if not block.strip():
            continue
        head, _, patch = block.partition("\n")
        rev, _, rest = head.partition("\x09")
        date, _, subject = rest.partition("\x09")
        title = html.escape(f"{rev}  {date}  {subject}")
        body = (f"<pre>{_colorize_diff(patch)}</pre>" if patch.strip()
                else "<p>(無差異內容)</p>")
        sections.append(f"<details><summary>{title}</summary>{body}"
                        f"</details>")
    page_name = html.escape(full[:-3])
    body_html = (f"<h1>頁面歷史:{page_name}</h1>"
                 f"<p>最近 {len(sections)} 筆修訂,點開看差異;"
                 f"還原請叫 AI 用 restore_page。</p>"
                 + "".join(sections))
    return HTMLResponse(render_share(f"歷史:{page_name}", body_html),
                        headers={"Cache-Control": "no-store"})


START_TIME = time.time()


def _page_text_or_none(name: str):
    try:
        return sb.read(name).decode("utf-8")
    except (httpx.HTTPStatusError, UnicodeDecodeError):
        return None


def status_page(request):
    supplied = request.path_params["secret"]
    if not SHARE_SECRET or not hmac.compare_digest(supplied, SHARE_SECRET):
        return PlainTextResponse("Not Found", status_code=404)
    files = sb.listing()
    space_files = [f for f in files if not f["name"].startswith("Library/")]
    total = sum(f["size"] for f in space_files)
    up_h = (time.time() - START_TIME) / 3600
    git_log = (_git("log", "-5", "--pretty=format:%h  %ad  %s",
                    "--date=format:%m-%d %H:%M")
               if GIT_ENABLED else "(git 未啟用)")
    funnel = _page_text_or_none("Trigger/funnel-restart-result.md") \
        or "(尚無 funnel 重啟紀錄)"
    backup = _page_text_or_none("Trigger/backup-status.md") \
        or "(尚無備份紀錄 — sb_backup.py 跑過一次後出現)"
    body = (
        f"<h1>系統狀態</h1>"
        f"<ul>"
        f"<li>MCP build:{html.escape(BUILD_TAG)}</li>"
        f"<li>容器連續運行:{up_h:.1f} 小時</li>"
        f"<li>空間:{len(space_files)} 檔,{total} bytes</li>"
        f"<li>版控:{'啟用' if GIT_ENABLED else '停用'}</li>"
        f"</ul>"
        f"<h2>最近 5 筆 commit</h2><pre>{html.escape(git_log)}</pre>"
        f"<h2>Funnel 最後重啟</h2><pre>{html.escape(funnel)}</pre>"
        f"<h2>最後備份</h2><pre>{html.escape(backup)}</pre>"
    )
    page_html = render_share("系統狀態", body)
    return HTMLResponse(page_html, headers={"Cache-Control": "no-store"})


# paths that visitors / the ally connector reach with their own URL secrets —
# the bearer token (if set) guards only the owner's MCP surface
PUBLIC_PREFIXES = ("/share/", "/edit/", "/ally/", "/status/", "/history/")


class BearerCheck(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        if BEARER_TOKEN and not request.url.path.startswith(PUBLIC_PREFIXES):
            supplied = request.headers.get("authorization", "")
            expected = f"Bearer {BEARER_TOKEN}"
            if not hmac.compare_digest(supplied, expected):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        return await call_next(request)


INTERNAL_OPERATIONS = {
    "kb_register_backlink": kb_register_backlink,
    "kb_move_pending_to_index": kb_move_pending_to_index,
    "kb_create_index": kb_create_index,
}


async def internal_operation_endpoint(request):
    """Operations that are not MCP tools, so callers never see them: the
    review side and the services that link to kb pages from outside kb/.
    Body: the operation's arguments as a JSON object."""
    operation = INTERNAL_OPERATIONS.get(request.path_params["operation"])
    if not operation:
        return PlainTextResponse("Not Found", status_code=404)
    try:
        reply = operation(**(await request.json()))
    except (ValueError, TypeError) as e:
        return PlainTextResponse(str(e), status_code=400)
    return PlainTextResponse(reply)


main_app = mcp.http_app(path=f"/{PATH_SECRET}/mcp")
ally_app = ally.http_app(path="/mcp")

routes = [Route(f"/{PATH_SECRET}/internal/{{operation}}",
                internal_operation_endpoint, methods=["POST"]),
          Route("/share/{secret}/{page:path}", share_page, methods=["GET"]),
          Route("/status/{secret}", status_page, methods=["GET"]),
          Route("/history/{secret}/{page:path}", history_page,
                methods=["GET"])]
if ALLY_SECRET:
    routes.append(Route("/edit/{secret}/{page:path}", edit_endpoint,
                        methods=["GET", "PUT"]))
    routes.append(Mount(f"/ally/{ALLY_SECRET}", app=ally_app))
routes.append(Mount("/", app=main_app))


@asynccontextmanager
async def _lifespan(app):
    async with main_app.lifespan(main_app):
        async with ally_app.lifespan(ally_app):
            yield


app = Starlette(routes=routes, lifespan=_lifespan)
app.add_middleware(BearerCheck)
# claude.ai's connector flow includes browser-side requests — without CORS the
# preflight dies and registration fails. Origins are not a security boundary
# here (the path secret is); expose the MCP session header for browser clients.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["mcp-session-id", "mcp-protocol-version"],
    max_age=86400,
)

if __name__ == "__main__":
    ssl_kwargs = {}
    if SSL_CERTFILE or SSL_KEYFILE:
        if not (SSL_CERTFILE and SSL_KEYFILE):
            sys.exit("SSL_CERTFILE and SSL_KEYFILE must be set together")
        ssl_kwargs = {"ssl_certfile": SSL_CERTFILE, "ssl_keyfile": SSL_KEYFILE}
    scheme = "https" if ssl_kwargs else "http"
    _git_init()
    threading.Thread(target=_sweep_loop, daemon=True).start()
    print(f"SilverBullet MCP build {BUILD_TAG}", flush=True)
    print(f"listening on {scheme}://0.0.0.0:{PORT} "
          f"(endpoint path /<secret>/mcp, secret hidden)", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level=LOG_LEVEL,
                access_log=True, **ssl_kwargs)
