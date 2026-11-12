"""kb-audio — drop an audio file, get a transcript, a one-shot summary, and an
import into the SilverBullet knowledge base (kb/) through the MCP bridge.

Two entrances, two path secrets, one container (no listing anywhere):
  /<AUDIO_PATH_SECRET>/  "public": drop -> transcript -> summary -> download
  /<KB_PATH_SECRET>/     "kb": the same plus a background textarea before the
                         drop (fed to summary and proposal) and the import pages.
A job belongs to the entrance that created it; the other secret gets 404.
  GET  /<secret>/                 drop page
  POST /<secret>/upload           multipart file + transcribe_model [+ context] -> 303 /<secret>/j/<uuid>
  POST /<KB_PATH_SECRET>/api/text multipart .txt/.md [+ context] -> {id, url}; no transcription,
                                  job already done (API only, no page; for large originals)
  POST /<KB_PATH_SECRET>/api/source multipart .txt/.md + name + index -> {page}; writes ONLY the
                                  kb source page through the bridge, no job / summary / proposal
  GET  /<secret>/j/<uuid>         job page (polls; reload-safe, state lives on disk)
  GET  /<secret>/api/j/<uuid>     state json
  GET  /<secret>/j/<uuid>/transcript.txt
  POST /<secret>/api/j/<uuid>/context   kb only; replace the background until the summary exists
  POST /<secret>/api/j/<uuid>/summary   once; 409 afterwards
  GET  /<secret>/j/<uuid>/summary.md
  GET  /<secret>/j/<uuid>/import  import page (needs summary)
  POST /<secret>/api/j/<uuid>/proposal  ask the model for name / pages / tags;
                                        with {feedback, current} it revises the current form instead
  POST /<secret>/api/j/<uuid>/import    once; copies audio to kb-media, kb_write via MCP

Env: AUDIO_PATH_SECRET + KB_PATH_SECRET (>=16 chars, different), OPENROUTER_API_KEY (transcription),
ANTHROPIC_API_KEY (summary + proposal), MCP_URL (bridge, LAN, with secret),
JOBS_DIR=/jobs, MEDIA_DIR=/media, MAX_UPLOAD_MB=500, PORT=8009.
"""
import asyncio
import datetime as dt
import hmac
import html
import json
import logging
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid

import anthropic
import httpx
import uvicorn
from opencc import OpenCC
from fastmcp import Client as McpClient
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import (FileResponse, HTMLResponse, JSONResponse,
                                 PlainTextResponse)
from starlette.routing import Route

BUILD_TAG = "2026-11-11 (kb-audio v2.14: versioned kb_write, backlink over HTTP)"

SECRET = os.environ["AUDIO_PATH_SECRET"]
KB_SECRET = os.environ["KB_PATH_SECRET"]
OPENROUTER_API_KEY = os.environ["OPENROUTER_API_KEY"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
MCP_URL = os.environ["MCP_URL"]
JOBS_DIR = os.environ.get("JOBS_DIR", "/jobs")
MEDIA_DIR = os.environ.get("MEDIA_DIR", "/media")
# upload cap per entrance in MB; 0 = unlimited (the kb entrance is the owner's)
MAX_UPLOAD_BYTES_BY_MODE = {
    "public": int(os.environ.get("MAX_UPLOAD_MB", "2048")) * 1024 * 1024,
    "kb": int(os.environ.get("KB_MAX_UPLOAD_MB", "0")) * 1024 * 1024,
}
PORT = int(os.environ.get("PORT", "8009"))
if len(SECRET) < 16 or len(KB_SECRET) < 16 or SECRET == KB_SECRET:
    sys.exit("AUDIO_PATH_SECRET and KB_PATH_SECRET must be >=16 chars and differ")
SECRET_BY_MODE = {"public": SECRET, "kb": KB_SECRET}

OPENROUTER_STT_URL = "https://openrouter.ai/api/v1/audio/transcriptions"
TRANSCRIBE_MODELS = {
    "qwen/qwen3-asr-1.7b": "Qwen3-ASR 1.7B（預設，中英）",
    "openai/whisper-large-v3": "Whisper large-v3",
}
SUMMARY_MODELS = {
    "claude-fable-5-1": "Claude Fable 5.1（預設）",
    "claude-opus-5-5": "Claude Opus 5.5",
    "claude-sonnet-5-5": "Claude Sonnet 5.5",
    "claude-haiku-5-5": "Claude Haiku 5.5",
}
DEFAULT_TRANSCRIBE = "qwen/qwen3-asr-1.7b"
DEFAULT_SUMMARY = "claude-fable-5-1"
# hard cap for the one-line summary that goes on index rows; the prompt
# quotes it, details belong in 理由 or the original text
SUMMARY_MAX_CHARS = 80
CHUNK_SECONDS = 600
ALLOWED_EXT = ("wav", "mp3", "m4a", "flac", "ogg", "webm", "aac")
# POST /<KB_PATH_SECRET>/api/text: a text file becomes a finished job whose
# transcript is the file itself, then the usual summary / proposal / import
TEXT_EXT = ("txt", "md")
KB_NAME_RE = re.compile(r"\A[a-z0-9_]+\Z")
KB_INDEX_RE = re.compile(r"\A[^/\s]+(?:/[^/\s]+)*\Z")
KB_TAG_RE = re.compile(r"\A[^\s#]+\Z")
NO_STORE = {"Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow"}

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
# transcription models tend to emit Simplified Chinese; s2twp = Simplified ->
# Traditional (Taiwan) with phrase conversion (软件→軟體, 网络→網路)
to_traditional = OpenCC("s2twp").convert
work_queue: "queue.Queue[str]" = queue.Queue()
state_lock = threading.Lock()


# ---- job state on disk (the only source of truth) ----
def job_dir(job_id: str) -> str:
    return os.path.join(JOBS_DIR, job_id)


def load_state(job_id: str) -> dict | None:
    path = os.path.join(job_dir(job_id), "state.json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict):
    path = os.path.join(job_dir(state["id"]), "state.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def job_log(job_id: str, line: str):
    stamp = dt.datetime.now().strftime("%H:%M:%S")
    with open(os.path.join(job_dir(job_id), "log.txt"), "a", encoding="utf-8") as f:
        f.write(f"{stamp} {line}\n")
    print(f"[{job_id[:8]}] {line}", flush=True)


def is_valid_job_id(job_id: str) -> bool:
    try:
        return str(uuid.UUID(job_id)) == job_id
    except ValueError:
        return False


# ---- transcription worker ----
def hms(seconds: float) -> str:
    seconds = int(seconds)
    return f"{seconds // 3600:02d}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"


def probe_duration(path: str) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", path],
        capture_output=True, text=True, timeout=120)
    return float(out.stdout.strip() or 0)


def split_into_chunks(audio: str, chunks_dir: str) -> list[str]:
    os.makedirs(chunks_dir, exist_ok=True)
    subprocess.run(
        ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", audio,
         "-ac", "1", "-ar", "16000", "-f", "segment",
         "-segment_time", str(CHUNK_SECONDS), "-reset_timestamps", "1",
         os.path.join(chunks_dir, "%03d.wav")],
        check=True, timeout=3600)
    return sorted(f for f in os.listdir(chunks_dir) if f.endswith(".wav"))


def transcribe_chunk(wav_path: str, model: str) -> list[tuple[float, str]]:
    """Returns [(start_seconds_within_chunk, text), ...]."""
    with open(wav_path, "rb") as f:
        r = httpx.post(OPENROUTER_STT_URL,
                       headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                       data={"model": model},
                       files={"file": (os.path.basename(wav_path), f, "audio/wav")},
                       timeout=600)
    r.raise_for_status()
    body = r.json()
    segments = body.get("segments")
    if isinstance(segments, list) and segments and all(
            isinstance(s, dict) and "text" in s for s in segments):
        return [(float(s.get("start") or 0), to_traditional(str(s["text"]).strip()))
                for s in segments if str(s["text"]).strip()]
    text = str(body.get("text") or "").strip()
    return [(0.0, to_traditional(text))] if text else []


def run_job(job_id: str):
    state = load_state(job_id)
    if not state or state["status"] in ("done", "failed"):
        return
    d = job_dir(job_id)
    audio = os.path.join(d, f"audio.{state['ext']}")
    chunks_dir = os.path.join(d, "chunks")
    try:
        state["status"] = "transcribing"
        if not state.get("duration"):
            state["duration"] = probe_duration(audio)
        chunks = (sorted(f for f in os.listdir(chunks_dir) if f.endswith(".wav"))
                  if os.path.isdir(chunks_dir) else [])
        if not chunks:
            job_log(job_id, "splitting into chunks")
            chunks = split_into_chunks(audio, chunks_dir)
        state["chunks_total"] = len(chunks)
        state["chunks_done"] = sum(
            1 for c in chunks if os.path.exists(os.path.join(chunks_dir, c[:-4] + ".txt")))
        save_state(state)
        for i, chunk in enumerate(chunks):
            txt_path = os.path.join(chunks_dir, chunk[:-4] + ".txt")
            if os.path.exists(txt_path):
                continue
            t0 = time.time()
            parts = transcribe_chunk(os.path.join(chunks_dir, chunk),
                                     state["transcribe_model"])
            offset = i * CHUNK_SECONDS
            lines = [f"[{hms(offset + start)}] {text}" for start, text in parts]
            with open(txt_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + ("\n" if lines else ""))
            state["chunks_done"] = i + 1
            save_state(state)
            job_log(job_id, f"chunk {i + 1}/{len(chunks)} done in {time.time() - t0:.1f}s")
        transcript = []
        for chunk in chunks:
            with open(os.path.join(chunks_dir, chunk[:-4] + ".txt"), encoding="utf-8") as f:
                transcript.append(f.read())
        with open(os.path.join(d, "transcript.txt"), "w", encoding="utf-8") as f:
            f.write("".join(transcript))
        state["status"] = "done"
        state["finished"] = dt.datetime.now().isoformat(timespec="seconds")
        save_state(state)
        job_log(job_id, "transcript ready")
    except Exception as e:  # keep the job inspectable instead of crashing the worker
        state["status"] = "failed"
        state["error"] = f"{type(e).__name__}: {e}"[:500]
        save_state(state)
        job_log(job_id, f"FAILED {state['error']}")


def worker_loop():
    while True:
        job_id = work_queue.get()
        try:
            run_job(job_id)
        finally:
            work_queue.task_done()


def requeue_unfinished():
    os.makedirs(JOBS_DIR, exist_ok=True)
    for job_id in sorted(os.listdir(JOBS_DIR)):
        state = load_state(job_id) if is_valid_job_id(job_id) else None
        if state and state["status"] in ("queued", "transcribing"):
            work_queue.put(job_id)
            print(f"requeued {job_id}", flush=True)


# ---- Claude: summary and import proposal ----
def read_transcript(job_id: str) -> str:
    with open(os.path.join(job_dir(job_id), "transcript.txt"), encoding="utf-8") as f:
        return f.read()


SUMMARY_PROMPT = """你是知識庫的整理者。下面是一段錄音的逐字稿（每段前有 [時:分:秒] 時間戳）；
前面若有「使用者提供的背景」，那是錄音主人事先寫的脈絡與初步總結，用它理解錄音，但不要把背景裡逐字稿沒有的事寫成錄音內容。
逐字稿是機器轉寫，會有錯字與誤聽（同音、近音、外文被拆成別的字）。你要修正它們：依背景與逐字稿前後文判斷講者實際說的詞
（例如前後文都在講音樂製作，「低 fi」就是 lo-fi；產品名、人名、術語尤其要對照背景與上下文），摘要裡一律寫校正後的詞，並在最後加一節列出修正。
用正體中文寫摘要，Markdown，結構固定：

# 一句話總結
（一句，講這段錄音是關於什麼、結論是什麼）

## 重點
- 條列，每條一到兩句，句尾附最相關的時間戳，如 [00:03:12]

## 決策與結論
- 若錄音裡有「決定採用什麼、否決什麼、為什麼」，逐條寫出：定案結果、理由、否決的替代案；沒有就寫「無」

## 待辦與未決
- 錄音裡提到要做、還沒定的事；沒有就寫「無」

## 逐字稿修正
- 「聽成的詞」→「校正後的詞」：依據一句；沒有就寫「無」

不要加任何不在逐字稿裡的資訊（修正誤聽不算），不要客套。"""

PROPOSAL_PROMPT = """你是知識庫（kb）的寫入助理。使用者要把一段錄音匯入 kb。你會拿到：kb 現有的分類與路由 tag、（可能有）使用者事先寫的背景、摘要、逐字稿。
逐字稿是機器轉寫，會有錯字與誤聽（同音、近音、外文被拆成別的字）。先修正它們：依背景、摘要與逐字稿前後文判斷講者實際說的詞
（例如前後文都在講音樂製作，「低 fi」就是 lo-fi；產品名、人名、術語尤其要對照背景與上下文），name、index、tags、總結、理由一律用校正後的詞，
並把會進頁面的修正列在 corrections 裡讓使用者核對：只列產品名、人名、專有術語與重複出現的關鍵詞（最多 10 條），
一般口語錯字不用列；拿不準的詞保留逐字稿原樣並在 why 註明「待確認」。
請提議怎麼拆成 kb 頁，只回 JSON（不要 Markdown 圍欄、不要說明文字），結構：

{
  "corrections": [{"heard": "<逐字稿寫的>", "corrected": "<講者實際說的>", "why": "<依據一句>"}],
  "name": "<小寫英文與底線的簡短檔名，不含日期，例 lofi_mix_notes>",
  "pages": [
    {
      "kind": "knowledge" | "decision",
      "summaries": [
        {"index": "<以 / 分層的分類路徑，層數不限（例 遊戲/screeps/capture）；優先用現有分類；沒有合適的才提議新的下層分類，掛在現有分類底下>",
         "text": "<總結一句，硬上限 SUMMARY_MAX_CHARS 個字（含標點），超過就是錯；只寫這頁回答什麼問題或定了什麼，
                  細節、例子、數字、案例一律不進這裡（decision 的進 reasoning，knowledge 的留在原文）；
                  kind=decision 時必須是定案結果本身，寫成「<對象> 採 <結果>」>",
         "tags": ["<小寫英文或中文，無空白，無 #>", ...]}
      ],
      "reasoning": "<只有 kind=decision 要：為何這樣定、考慮過哪些替代、為何否決；可多行>"
    }
  ]
}

規則：
- 一個錄音通常拆成 1～5 頁；每頁一個主題。錄音裡有明確「決定採用 X、不用 Y」的才是 decision。
- summaries 每頁一到兩個 index；同一頁多個 index 時各寫一句對應那個分類的總結。
- 不要提議 "inbox"。不要提議新的大類。
- tag 是給路由用的關鍵字，每個 index 兩到四個。
- 不可編造逐字稿沒有的內容；修正誤聽不算編造。
- 寫完先自己檢查一遍：每個 text 數一次字數，超過 SUMMARY_MAX_CHARS 字的刪到合規；每頁是否只有一個主題
  （一頁塞多件事就拆開）、decision 是否真的是講者定案而非建議、總結是否能單獨讀懂、index 名稱是否對得上頁面內容；
  檢查後再輸出。""".replace("SUMMARY_MAX_CHARS", str(SUMMARY_MAX_CHARS))


def ask_claude(model: str, system: str, user: str, max_tokens: int) -> str:
    resp = anthropic_client.messages.create(
        model=model, max_tokens=max_tokens, system=system,
        messages=[{"role": "user", "content": user}])
    return "".join(block.text for block in resp.content if block.type == "text")


def context_block(state: dict) -> str:
    context = (state.get("context") or "").strip()
    return f"## 使用者提供的背景\n{context}\n\n" if context else ""


def make_summary(job_id: str, model: str) -> str:
    state = load_state(job_id)
    text = ask_claude(model, SUMMARY_PROMPT,
                      f"{context_block(state)}## 逐字稿\n{read_transcript(job_id)}", 4000)
    with open(os.path.join(job_dir(job_id), "summary.md"), "w", encoding="utf-8") as f:
        f.write(text.strip() + "\n")
    return text


async def mcp_call(tool: str, **args) -> str:
    async with McpClient(MCP_URL) as c:
        result = await c.call_tool(tool, args)
        return "\n".join(b.text for b in result.content if hasattr(b, "text"))


async def kb_write_call(**args) -> str:
    """kb_write with the version of the usage text the bridge currently serves."""
    version = (await mcp_call("readme")).split("\n", 1)[0].split("：", 1)[1]
    return await mcp_call("kb_write", readme=version, **args)


def register_backlink(name: str, from_page: str) -> str:
    r = httpx.post(MCP_URL.rsplit("/mcp", 1)[0] + "/internal/kb_register_backlink",
                   json={"name": name, "from_page": from_page}, timeout=30)
    r.raise_for_status()
    return r.text


def kb_context() -> str:
    return asyncio.run(mcp_call("kb_index"))


def parse_proposal(raw: str) -> dict:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"\A```[a-z]*\n|\n```\Z", "", raw)
    data = json.loads(raw)
    errors = []
    if not isinstance(data.get("name"), str) or not KB_NAME_RE.match(data["name"]):
        errors.append("name 格式")
    pages = data.get("pages")
    if not isinstance(pages, list) or not pages:
        errors.append("pages 空")
        pages = []
    for i, page in enumerate(pages):
        if page.get("kind") not in ("knowledge", "decision"):
            errors.append(f"pages[{i}].kind")
        for j, s in enumerate(page.get("summaries") or []):
            if not isinstance(s.get("index"), str) or not KB_INDEX_RE.match(s["index"]) \
                    or s["index"].split("/")[0] in ("week", "inbox"):
                errors.append(f"pages[{i}].summaries[{j}].index")
            if not isinstance(s.get("text"), str) or not s["text"].strip():
                errors.append(f"pages[{i}].summaries[{j}].text")
            s["tags"] = [t.lower() for t in (s.get("tags") or [])
                         if isinstance(t, str) and KB_TAG_RE.match(t)]
        if not page.get("summaries"):
            errors.append(f"pages[{i}].summaries 空")
        if page.get("kind") == "decision" and not str(page.get("reasoning") or "").strip():
            errors.append(f"pages[{i}].reasoning 空")
    if errors:
        raise ValueError("模型提議格式不合：" + "、".join(errors))
    return data


def make_proposal(job_id: str, model: str, current: dict | None = None,
                  feedback: str = "") -> dict:
    """First call: propose from scratch. With `feedback`: revise `current`
    (the form as the user left it) according to the feedback, with every
    earlier round's feedback in view. Each round is kept in state."""
    state = load_state(job_id)
    with open(os.path.join(job_dir(job_id), "summary.md"), encoding="utf-8") as f:
        summary = f.read()
    user = (f"## kb 現況（kb_index 回傳）\n{kb_context()}\n\n{context_block(state)}"
            f"## 摘要\n{summary}\n\n## 逐字稿\n{read_transcript(job_id)}")
    if feedback:
        earlier = "\n".join(f"- 第 {i + 1} 輪：{r['feedback']}"
                            for i, r in enumerate(state.get("proposal_rounds") or []))
        user += (f"\n\n## 目前的提議（使用者可能已經手改過，手改的部分除非意見要求否則保留）\n"
                 f"{json.dumps(current, ensure_ascii=False, indent=1)}\n\n"
                 f"## 之前幾輪的修正意見\n{earlier or '（無）'}\n\n"
                 f"## 這一輪的修正意見（照這個改，回完整 JSON）\n{feedback}")
    raw = ask_claude(model, PROPOSAL_PROMPT, user, 8000)
    proposal = parse_proposal(raw)
    state = load_state(job_id)
    if feedback:
        state.setdefault("proposal_rounds", []).append(
            {"feedback": feedback, "proposal": proposal,
             "when": dt.datetime.now().isoformat(timespec="seconds")})
    state["proposal"] = proposal
    save_state(state)
    return proposal


HOME_PAGE = "index"
HOME_AUDIO_HEADING = "## kb 音檔記錄"


def append_home_audio_record(line: str) -> str:
    """Insert one list line right under the home page's 「kb 音檔記錄」
    heading (newest first) through the bridge. Returns a short note; a
    missing heading is reported, not fixed."""
    text = asyncio.run(mcp_call("read_page", path=HOME_PAGE))
    lines = text.split("\n")
    try:
        at = lines.index(HOME_AUDIO_HEADING)
    except ValueError:
        return f"首頁沒有「{HOME_AUDIO_HEADING}」段，沒有登記"
    lines.insert(at + 1, line)
    return asyncio.run(mcp_call("write_page", path=HOME_PAGE, content="\n".join(lines)))


def nearest_common_category(proposal: dict) -> str:
    """The source page is registered where its pages are: their category
    when all share one, otherwise the deepest category that is an ancestor
    of every page's categories (at least the shared top level)."""
    paths = [s["index"].split("/") for page in proposal["pages"] for s in page["summaries"]]
    common = paths[0]
    for path in paths[1:]:
        n = 0
        while n < min(len(common), len(path)) and common[n] == path[n]:
            n += 1
        common = common[:n]
    return "/".join(common) if common else paths[0][0]


def do_import(job_id: str, proposal: dict) -> list[str]:
    """Copy the audio into kb-media, write the source page, then every page,
    then register the import on the home page. Returns the bridge's reply lines."""
    state = load_state(job_id)
    day = dt.date.today().strftime("%Y%m%d")
    base = f"{day}_{proposal['name']}"
    media_name = f"{base}.{state['ext']}"
    os.makedirs(MEDIA_DIR, exist_ok=True)
    shutil.copy2(os.path.join(job_dir(job_id), f"audio.{state['ext']}"),
                 os.path.join(MEDIA_DIR, media_name))
    replies = [asyncio.run(kb_write_call(
        name=f"{base}_source", kind="source",
        original=read_transcript(job_id), file=f"kb-media/{media_name}",
        index=nearest_common_category(proposal)))]
    for i, page in enumerate(proposal["pages"]):
        suffix = "" if len(proposal["pages"]) == 1 else f"_{i + 1}"
        args = dict(name=f"{base}{suffix}", kind=page["kind"],
                    summaries=page["summaries"],
                    source={"type": "source", "name": f"{base}_source"})
        if page["kind"] == "decision":
            args["reasoning"] = page["reasoning"]
        replies.append(asyncio.run(kb_write_call(**args)))
    page_links = " ".join(
        f"[[kb/{base}{'' if len(proposal['pages']) == 1 else f'_{i + 1}'}|{i + 1}]]"
        for i in range(len(proposal["pages"])))
    replies.append(append_home_audio_record(
        f"- {dt.date.today().isoformat()} {state['filename']}（{int((state.get('duration') or 0) // 60)} 分）"
        f" → [[kb/{base}_source|原文]] {page_links}"))
    # the home-page line links to every page: register the backlink on each (雙向鏈)
    for linked in [f"{base}_source"] + [
            f"{base}{'' if len(proposal['pages']) == 1 else f'_{i + 1}'}"
            for i in range(len(proposal["pages"]))]:
        replies.append(register_backlink(linked, HOME_PAGE))
    state["import"] = {"when": dt.datetime.now().isoformat(timespec="seconds"),
                       "media": f"kb-media/{media_name}", "source": f"kb/{base}_source",
                       "replies": replies}
    save_state(state)
    job_log(job_id, f"imported as kb/{base}* ({len(proposal['pages'])} pages)")
    return replies


# ---- HTTP ----
def mode_of(request: Request) -> str | None:
    """Which entrance the supplied path secret belongs to, or None."""
    supplied = request.path_params.get("secret", "")
    for mode, secret in SECRET_BY_MODE.items():
        if hmac.compare_digest(supplied, secret):
            return mode
    return None


def not_found():
    return PlainTextResponse("Not Found", status_code=404, headers=NO_STORE)


def page(title: str, body: str, script: str = "") -> HTMLResponse:
    doc = f"""<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow"><title>{html.escape(title)}</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:900px;margin:2rem auto;padding:0 1rem;line-height:1.5;color:#222}}
h1{{font-size:1.4rem}} pre{{white-space:pre-wrap;background:#f6f6f6;padding:1rem;border-radius:6px;max-height:60vh;overflow:auto}}
.drop{{border:2px dashed #999;border-radius:10px;padding:3rem;text-align:center;color:#555}}
.drop.over{{border-color:#06c;color:#06c;background:#eef5ff}}
button{{font-size:1rem;padding:.5rem 1rem;border-radius:6px;border:1px solid #888;background:#fff;cursor:pointer}}
button:disabled{{opacity:.5;cursor:default}} .row{{margin:1rem 0;display:flex;gap:.75rem;align-items:center;flex-wrap:wrap}}
.status{{padding:.5rem .75rem;border-radius:6px;background:#eee;display:inline-block}}
.err{{color:#b00}} input,textarea,select{{font:inherit;padding:.3rem}} textarea{{width:100%}}
table{{border-collapse:collapse;width:100%}} td,th{{border:1px solid #ddd;padding:.4rem;vertical-align:top;text-align:left}}
small{{color:#666}} a{{color:#06c}}
</style></head><body>{body}<script>{script}</script></body></html>"""
    return HTMLResponse(doc, headers=NO_STORE)


def limit_text(mode: str) -> str:
    cap = MAX_UPLOAD_BYTES_BY_MODE[mode]
    return f"上限 {cap // 1024 // 1024} MB" if cap else "大小不限"


def is_over_limit(mode: str, size: int) -> bool:
    cap = MAX_UPLOAD_BYTES_BY_MODE[mode]
    return bool(cap) and size > cap


def options_html(models: dict, default: str) -> str:
    return "".join(f'<option value="{html.escape(k)}"{" selected" if k == default else ""}>'
                   f'{html.escape(v)}</option>' for k, v in models.items())


async def drop_page(request: Request):
    mode = mode_of(request)
    if not mode:
        return not_found()
    base = f"/{SECRET_BY_MODE[mode]}"
    title = "kb-audio：音檔轉逐字稿" + ("（匯入 kb）" if mode == "kb" else "")
    context_html = ("""<div class="row"><label style="width:100%">背景／你已知的重點（選填，摘要與匯入提議都會參考）
<textarea id="context" rows="4" placeholder="例：這是 11/8 跟 A 討論 lofi 專輯混音的錄音，提到的『那個插件』是 Valhalla Supermassive"></textarea></label></div>"""
                    if mode == "kb" else "")
    body = f"""<h1>{html.escape(title)}</h1>
{context_html}
<div class="row"><label>轉寫模型 <select id="model">{options_html(TRANSCRIBE_MODELS, DEFAULT_TRANSCRIBE)}</select></label></div>
<div class="drop" id="drop">把音檔拖到這裡，或點一下選檔<br><small>{html.escape(", ".join(ALLOWED_EXT))}，{limit_text(mode)}</small>
<input type="file" id="file" accept="{",".join(f".{e}" for e in ALLOWED_EXT)},audio/*,audio/mp4,audio/x-m4a" style="display:none"></div>
<div class="row"><span class="status" id="st">等待檔案</span><button type="button" id="go" style="display:none">儲存背景並前往轉檔頁</button></div>
<p><small>build {html.escape(BUILD_TAG)}</small></p>"""
    script = f"""
const drop=document.getElementById('drop'),file=document.getElementById('file'),st=document.getElementById('st'),go=document.getElementById('go');
const kbMode={'true' if mode == 'kb' else 'false'};let jobUrl=null,jobId=null;
drop.onclick=()=>file.click();
['dragenter','dragover'].forEach(e=>drop.addEventListener(e,ev=>{{ev.preventDefault();drop.classList.add('over')}}));
['dragleave','drop'].forEach(e=>drop.addEventListener(e,ev=>{{ev.preventDefault();drop.classList.remove('over')}}));
drop.addEventListener('drop',ev=>{{if(ev.dataTransfer.files[0])send(ev.dataTransfer.files[0])}});
file.onchange=()=>{{if(file.files[0])send(file.files[0])}};
function send(f){{
  const fd=new FormData();fd.append('file',f);fd.append('transcribe_model',document.getElementById('model').value);
  const ctx=document.getElementById('context');if(ctx)fd.append('context',ctx.value);
  st.textContent='上傳中 '+f.name+' ('+Math.round(f.size/1048576)+' MB)…';
  const x=new XMLHttpRequest();x.open('POST','{base}/upload');
  x.upload.onprogress=e=>{{if(e.lengthComputable)st.textContent='上傳中 '+Math.round(e.loaded*100/e.total)+'%'}};
  x.onload=()=>{{
    if(x.status!==200){{st.textContent='失敗：'+x.responseText;st.className='status err';return}}
    const r=JSON.parse(x.responseText);
    if(!kbMode){{location.href=r.url;return}}
    jobUrl=r.url;jobId=r.id;drop.style.display='none';
    st.textContent='上傳完成，已開始轉寫。背景可以繼續寫，寫完按右邊的按鈕；不按也沒關係，轉檔頁會等你。';
    go.style.display='';
  }};
  x.onerror=()=>{{st.textContent='上傳失敗（連線）';st.className='status err'}};
  x.send(fd);
}}
go.onclick=async()=>{{
  go.disabled=true;
  const r=await fetch('{base}/api/j/'+jobId+'/context',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{context:document.getElementById('context').value}})}});
  if(r.ok){{location.href=jobUrl}}else{{st.textContent='背景儲存失敗：'+await r.text();st.className='status err';go.disabled=false}}
}};"""
    return page(title, body, script)


async def upload(request: Request):
    mode = mode_of(request)
    if not mode:
        return not_found()
    length = int(request.headers.get("content-length") or 0)
    if is_over_limit(mode, length):
        return PlainTextResponse(f"檔案超過上限（{limit_text(mode)}）", status_code=413,
                                 headers=NO_STORE)
    form = await request.form()
    up = form.get("file")
    model = form.get("transcribe_model") or DEFAULT_TRANSCRIBE
    if up is None or not getattr(up, "filename", ""):
        return PlainTextResponse("沒有檔案", status_code=400, headers=NO_STORE)
    if model not in TRANSCRIBE_MODELS:
        return PlainTextResponse("未知的轉寫模型", status_code=400, headers=NO_STORE)
    ext = up.filename.rsplit(".", 1)[-1].lower() if "." in up.filename else ""
    if ext not in ALLOWED_EXT:
        return PlainTextResponse(f"副檔名須為 {', '.join(ALLOWED_EXT)}", status_code=400,
                                 headers=NO_STORE)
    job_id = str(uuid.uuid4())
    d = job_dir(job_id)
    os.makedirs(d)
    size = 0
    with open(os.path.join(d, f"audio.{ext}"), "wb") as f:
        while chunk := await up.read(1 << 20):
            size += len(chunk)
            if is_over_limit(mode, size):
                shutil.rmtree(d, ignore_errors=True)
                return PlainTextResponse(f"檔案超過上限（{limit_text(mode)}）", status_code=413,
                                         headers=NO_STORE)
            f.write(chunk)
    context = (form.get("context") or "").replace("\r\n", "\n").strip() if mode == "kb" else ""
    state = {"id": job_id, "mode": mode,
             "created": dt.datetime.now().isoformat(timespec="seconds"),
             "filename": up.filename, "ext": ext, "size": size,
             "transcribe_model": model, "context": context, "status": "queued",
             "chunks_total": 0, "chunks_done": 0, "duration": 0,
             "summary": None, "proposal": None, "import": None}
    save_state(state)
    job_log(job_id, f"uploaded {up.filename} {size} bytes model={model} mode={mode} "
                    f"context={len(context)} chars")
    work_queue.put(job_id)
    return JSONResponse({"id": job_id, "url": f"/{SECRET_BY_MODE[mode]}/j/{job_id}"},
                        headers=NO_STORE)


async def text_api(request: Request):
    """kb entrance only, no page: POST a .txt/.md as multipart `file`
    (+ optional `context`). The file is taken verbatim as the transcript and
    the job is created already `done`; continue on the normal job page
    (summary → import). For originals too large to pass through a model."""
    if mode_of(request) != "kb":
        return not_found()
    form = await request.form()
    up = form.get("file")
    if up is None or not getattr(up, "filename", ""):
        return PlainTextResponse("沒有檔案", status_code=400, headers=NO_STORE)
    ext = up.filename.rsplit(".", 1)[-1].lower() if "." in up.filename else ""
    if ext not in TEXT_EXT:
        return PlainTextResponse(f"副檔名須為 {', '.join(TEXT_EXT)}", status_code=400,
                                 headers=NO_STORE)
    raw = await up.read()
    if is_over_limit("kb", len(raw)):
        return PlainTextResponse(f"檔案超過上限（{limit_text('kb')}）", status_code=413,
                                 headers=NO_STORE)
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")
    job_id = str(uuid.uuid4())
    d = job_dir(job_id)
    os.makedirs(d)
    with open(os.path.join(d, f"audio.{ext}"), "wb") as f:
        f.write(raw)
    with open(os.path.join(d, "transcript.txt"), "w", encoding="utf-8") as f:
        f.write(text if text.endswith("\n") else text + "\n")
    now = dt.datetime.now().isoformat(timespec="seconds")
    state = {"id": job_id, "mode": "kb", "created": now, "finished": now,
             "filename": up.filename, "ext": ext, "size": len(raw),
             "transcribe_model": "(text file, no transcription)",
             "context": (form.get("context") or "").replace("\r\n", "\n").strip(),
             "status": "done", "chunks_total": 1, "chunks_done": 1, "duration": 0,
             "summary": None, "proposal": None, "import": None}
    save_state(state)
    job_log(job_id, f"text file {up.filename} {len(raw)} bytes taken as transcript")
    return JSONResponse({"id": job_id, "url": f"/{KB_SECRET}/j/{job_id}"}, headers=NO_STORE)


async def source_api(request: Request):
    """kb entrance only, no page, no job: POST a .txt/.md as multipart `file`
    with `name` (lowercase English, no date, no _source) and `index` (the
    category of the pages that will cite it, or their nearest common
    parent). The file is written straight through the bridge as
    kb_write(kind="source") and the page name comes back. For an original
    whose pages the caller prepares itself."""
    if mode_of(request) != "kb":
        return not_found()
    form = await request.form()
    up = form.get("file")
    name = (form.get("name") or "").strip()
    index = (form.get("index") or "").strip()
    if up is None or not getattr(up, "filename", ""):
        return PlainTextResponse("沒有檔案", status_code=400, headers=NO_STORE)
    ext = up.filename.rsplit(".", 1)[-1].lower() if "." in up.filename else ""
    if ext not in TEXT_EXT:
        return PlainTextResponse(f"副檔名須為 {', '.join(TEXT_EXT)}", status_code=400,
                                 headers=NO_STORE)
    if not KB_NAME_RE.match(name) or name.endswith("_source"):
        return PlainTextResponse("name 須為小寫英數底線，不含日期，不以 _source 結尾",
                                 status_code=400, headers=NO_STORE)
    if not index:
        return PlainTextResponse("index 必填（引用它的頁所在分類，或最近共同母層）",
                                 status_code=400, headers=NO_STORE)
    raw = await up.read()
    if is_over_limit("kb", len(raw)):
        return PlainTextResponse(f"檔案超過上限（{limit_text('kb')}）", status_code=413,
                                 headers=NO_STORE)
    text = raw.decode("utf-8", errors="replace")
    page = f"{dt.date.today().strftime('%Y%m%d')}_{name}_source"
    try:
        reply = await kb_write_call(name=page, kind="source", original=text, index=index)
    except Exception as e:
        return PlainTextResponse(f"{type(e).__name__}: {e}"[:600], status_code=502, headers=NO_STORE)
    print(f"[source-api] {up.filename} {len(raw)} bytes -> kb/{page} @ {index}", flush=True)
    return JSONResponse({"page": f"kb/{page}", "reply": reply}, headers=NO_STORE)


def public_state(state: dict) -> dict:
    return {k: v for k, v in state.items() if k != "proposal"} | {
        "has_proposal": bool(state.get("proposal")),
        "queue_size": work_queue.qsize()}


def load_job_or_404(request: Request, require_mode: str | None = None):
    """The job must exist and belong to the entrance whose secret was used;
    `require_mode` additionally restricts the endpoint to one entrance."""
    job_id = request.path_params["job_id"]
    mode = mode_of(request)
    if not mode or not is_valid_job_id(job_id):
        return None, not_found()
    if require_mode and mode != require_mode:
        return None, not_found()
    state = load_state(job_id)
    if not state or state.get("mode", "public") != mode:
        return None, not_found()
    return state, None


async def job_page(request: Request):
    state, err = load_job_or_404(request)
    if err:
        return err
    secret = SECRET_BY_MODE[state["mode"]]
    base = f"/{secret}/j/{state['id']}"
    import_link = (f'<a href="{base}/import">→ 匯入 SilverBullet</a>'
                   if state["mode"] == "kb" else "")
    context_html = (f"<h2>背景</h2><pre>{html.escape(state['context'])}</pre>"
                    if state.get("context") else "")
    body = f"""<h1>{html.escape(state['filename'])}</h1>
<p><small>job {state['id']} ・ 轉寫模型 {html.escape(state['transcribe_model'])} ・ 上傳 {html.escape(state['created'])}</small></p>
{context_html}
<div class="row"><span class="status" id="st">載入中…</span></div>
<div id="transcript" style="display:none">
  <div class="row"><a href="{base}/transcript.txt" download>下載 transcript.txt</a>
    <button type="button" data-copy="tx">複製逐字稿</button></div>
  <pre id="tx"></pre>
  <h2>摘要</h2>
  <div class="row" id="sumrow">
    <label>模型 <select id="smodel">{options_html(SUMMARY_MODELS, DEFAULT_SUMMARY)}</select></label>
    <button id="sumbtn">Summary（只能按一次）</button>
  </div>
  <div id="sumout" style="display:none">
    <div class="row"><a href="{base}/summary.md" download>下載 summary.md</a>
      <button type="button" data-copy="sm">複製摘要</button>
      {import_link}</div>
    <pre id="sm"></pre>
  </div>
</div>
<p><a href="/{secret}/">← 再轉一個</a></p>"""
    script = f"""
const st=document.getElementById('st');let timer=null;
async function poll(){{
  const r=await fetch('/{secret}/api/j/{state['id']}',{{cache:'no-store'}});
  if(!r.ok){{st.textContent='讀取失敗 '+r.status;return}}
  const s=await r.json();
  if(s.status==='queued'){{st.textContent='排隊中（前面還有 '+s.queue_size+' 件）'}}
  else if(s.status==='transcribing'){{st.textContent='轉寫中 '+s.chunks_done+'/'+(s.chunks_total||'?')+' 段'+(s.duration?'，音檔 '+Math.round(s.duration/60)+' 分':'')}}
  else if(s.status==='failed'){{st.textContent='失敗：'+s.error;st.className='status err';clearInterval(timer)}}
  else if(s.status==='done'){{
    st.textContent='完成'+(s.duration?'，音檔 '+Math.round(s.duration/60)+' 分':'');clearInterval(timer);
    document.getElementById('transcript').style.display='';
    document.getElementById('tx').textContent=await (await fetch('{base}/transcript.txt',{{cache:'no-store'}})).text();
    if(s.summary){{showSummary()}}
  }}
}}
async function showSummary(){{
  document.getElementById('sumrow').style.display='none';
  document.getElementById('sumout').style.display='';
  document.getElementById('sm').textContent=await (await fetch('{base}/summary.md',{{cache:'no-store'}})).text();
}}
document.querySelectorAll('button[data-copy]').forEach(b=>b.onclick=async()=>{{
  const text=document.getElementById(b.dataset.copy).textContent;const label=b.textContent;
  try{{await navigator.clipboard.writeText(text);b.textContent='已複製';}}
  catch(e){{const ta=document.createElement('textarea');ta.value=text;document.body.appendChild(ta);ta.select();document.execCommand('copy');ta.remove();b.textContent='已複製';}}
  setTimeout(()=>b.textContent=label,1500);
}});
document.getElementById('sumbtn').onclick=async()=>{{
  const b=document.getElementById('sumbtn');b.disabled=true;b.textContent='摘要中…';
  const r=await fetch('/{secret}/api/j/{state['id']}/summary',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{model:document.getElementById('smodel').value}})}});
  if(r.ok||r.status===409){{showSummary()}}else{{b.textContent='失敗：'+await r.text();b.className='err'}}
}};
poll();timer=setInterval(poll,3000);"""
    return page(state["filename"], body, script)


async def job_state_api(request: Request):
    state, err = load_job_or_404(request)
    if err:
        return err
    return JSONResponse(public_state(state), headers=NO_STORE)


async def transcript_file(request: Request):
    state, err = load_job_or_404(request)
    if err:
        return err
    path = os.path.join(job_dir(state["id"]), "transcript.txt")
    if not os.path.exists(path):
        return PlainTextResponse("尚未完成", status_code=404, headers=NO_STORE)
    return FileResponse(path, media_type="text/plain; charset=utf-8", headers=NO_STORE,
                        filename=f"{state['filename'].rsplit('.', 1)[0]}.txt")


async def summary_file(request: Request):
    state, err = load_job_or_404(request)
    if err:
        return err
    path = os.path.join(job_dir(state["id"]), "summary.md")
    if not os.path.exists(path):
        return PlainTextResponse("尚無摘要", status_code=404, headers=NO_STORE)
    return FileResponse(path, media_type="text/markdown; charset=utf-8", headers=NO_STORE,
                        filename=f"{state['filename'].rsplit('.', 1)[0]}-summary.md")


async def context_api(request: Request):
    """kb entrance: replace the job's background text. Allowed any time:
    the summary (if already made) keeps what it used, later proposals read
    the new text."""
    state, err = load_job_or_404(request, require_mode="kb")
    if err:
        return err
    body = await request.json()
    context = str(body.get("context") or "").replace("\r\n", "\n").strip()
    with state_lock:
        state = load_state(state["id"])
        state["context"] = context
        save_state(state)
    job_log(state["id"], f"context updated, {len(context)} chars")
    return JSONResponse({"ok": True}, headers=NO_STORE)


async def summary_api(request: Request):
    state, err = load_job_or_404(request)
    if err:
        return err
    if state["status"] != "done":
        return PlainTextResponse("逐字稿尚未完成", status_code=409, headers=NO_STORE)
    body = await request.json()
    model = body.get("model") or DEFAULT_SUMMARY
    if model not in SUMMARY_MODELS:
        return PlainTextResponse("未知的摘要模型", status_code=400, headers=NO_STORE)
    with state_lock:
        state = load_state(state["id"])
        if state.get("summary"):
            return PlainTextResponse("摘要已存在，只能做一次", status_code=409, headers=NO_STORE)
        state["summary"] = {"model": model, "started": dt.datetime.now().isoformat(timespec="seconds")}
        save_state(state)
    try:
        t0 = time.time()
        text = await asyncio.to_thread(make_summary, state["id"], model)
        job_log(state["id"], f"summary by {model} in {time.time() - t0:.1f}s, {len(text)} chars")
    except Exception as e:
        with state_lock:
            state = load_state(state["id"])
            state["summary"] = None
            save_state(state)
        job_log(state["id"], f"summary FAILED {type(e).__name__}: {e}")
        return PlainTextResponse(f"{type(e).__name__}: {e}"[:300], status_code=502, headers=NO_STORE)
    return JSONResponse({"ok": True}, headers=NO_STORE)


async def import_page(request: Request):
    state, err = load_job_or_404(request, require_mode="kb")
    if err:
        return err
    base = f"/{KB_SECRET}/j/{state['id']}"
    if not os.path.exists(os.path.join(job_dir(state["id"]), "summary.md")):
        return page("匯入", f"<h1>匯入 SilverBullet</h1><p>要先做摘要。<a href='{base}'>回工作頁</a></p>")
    imported = state.get("import")
    done_html = ""
    if imported:
        done_html = ("<h2>已匯入</h2><pre>" + html.escape("\n".join(imported["replies"])) +
                     f"</pre><p>音檔：{html.escape(imported['media'])}　原文頁：{html.escape(imported['source'])}</p>")
    body = f"""<h1>匯入 SilverBullet：{html.escape(state['filename'])}</h1>
<p><small><a href="{base}">← 工作頁</a></small></p>
{done_html}
<div id="work" style="display:{'none' if imported else ''}">
<div class="row"><label style="width:100%">背景／你已知的重點（可改；重新提議會用新的）
<textarea id="ctx" rows="4">{html.escape(state.get('context') or '')}</textarea></label></div>
<div class="row"><button type="button" id="savectx">儲存背景</button><span class="status" id="cst">{'已有背景' if state.get('context') else '尚未填'}</span></div>
<div class="row"><label>提議模型 <select id="pmodel">{options_html(SUMMARY_MODELS, DEFAULT_SUMMARY)}</select></label>
<button id="propose">產生提議</button><span class="status" id="pst">尚未產生</span></div>
<div id="corr"></div>
<form id="form" style="display:none">
<div class="row"><label>name（日期由匯入日補）<input id="name" size="40"></label></div>
<div id="pages"></div>
<div class="row"><button type="button" id="addpage">+ 加一頁</button></div>
<h2>跟模型討論</h2>
<div class="row"><label style="width:100%">修正意見（例：tag 太籠統，要能區分 lofi 混音與編曲；第 2 頁不該是 decision）
<textarea id="feedback" rows="3"></textarea></label></div>
<div class="row"><button type="button" id="revise">重新提議</button><span class="status" id="rst">{len(state.get('proposal_rounds') or [])} 輪修正</span></div>
<div class="row"><button type="submit" id="go">匯入（只能按一次）</button><span class="status" id="ist"></span></div>
</form>
</div>"""
    script = f"""
const api='/{KB_SECRET}/api/j/{state['id']}';
const pst=document.getElementById('pst'),pages=document.getElementById('pages');
function pageHtml(p,i){{
  const sums=(p.summaries||[{{index:'',text:'',tags:[]}}]).map((s,j)=>`
   <tr><td><input class="idx" value="${{esc(s.index)}}" size="22"></td>
   <td><textarea class="txt" rows="2">${{esc(s.text)}}</textarea></td>
   <td><input class="tags" value="${{esc((s.tags||[]).join(' '))}}" size="22"></td></tr>`).join('');
  return `<fieldset class="page" style="margin:1rem 0"><legend>頁 ${{i+1}}
   <select class="kind"><option value="knowledge"${{p.kind==='knowledge'?' selected':''}}>knowledge</option><option value="decision"${{p.kind==='decision'?' selected':''}}>decision</option></select>
   <button type="button" class="del">刪此頁</button></legend>
   <table><tr><th>index（以 / 分層，層數不限）</th><th>總結一句</th><th>tags（空白分隔）</th></tr>${{sums}}</table>
   <button type="button" class="addsum">+ 加一個 index</button>
   <div class="row"><label style="width:100%">理由（decision 必填）<textarea class="reason" rows="3">${{esc(p.reasoning||'')}}</textarea></label></div>
  </fieldset>`;}}
function esc(s){{return String(s??'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;')}}
function render(p){{
  document.getElementById('name').value=p.name||'';
  pages.innerHTML=p.pages.map(pageHtml).join('');
  const corr=document.getElementById('corr');
  corr.innerHTML=(p.corrections&&p.corrections.length)?'<h2>逐字稿修正（模型判讀，請核對）</h2><pre>'+p.corrections.map(c=>esc(c.heard)+' → '+esc(c.corrected)+'　'+esc(c.why)).join('\\n')+'</pre>':'';
  document.getElementById('form').style.display='';
}}
pages.addEventListener('click',e=>{{
  if(e.target.classList.contains('del')){{e.target.closest('.page').remove()}}
  if(e.target.classList.contains('addsum')){{const t=e.target.previousElementSibling;t.insertAdjacentHTML('beforeend','<tr><td><input class="idx" size="22"></td><td><textarea class="txt" rows="2"></textarea></td><td><input class="tags" size="22"></td></tr>')}}
}});
document.getElementById('addpage').onclick=()=>pages.insertAdjacentHTML('beforeend',pageHtml({{kind:'knowledge',summaries:[{{}}]}},pages.children.length));
function collect(){{
  return {{name:document.getElementById('name').value.trim(),pages:[...pages.querySelectorAll('.page')].map(f=>({{
    kind:f.querySelector('.kind').value,
    reasoning:f.querySelector('.reason').value,
    summaries:[...f.querySelectorAll('tr')].slice(1).map(tr=>({{index:tr.querySelector('.idx').value.trim(),text:tr.querySelector('.txt').value.trim(),tags:tr.querySelector('.tags').value.trim().split(/\\s+/).filter(Boolean)}})).filter(s=>s.index||s.text)
  }}))}};
}}
document.getElementById('savectx').onclick=async()=>{{
  const cst=document.getElementById('cst');cst.textContent='儲存中…';
  const r=await fetch(api+'/context',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{context:document.getElementById('ctx').value}})}});
  cst.textContent=r.ok?'背景已儲存':'失敗：'+await r.text();cst.className=r.ok?'status':'status err';
}};
async function propose(extra,btn,label){{
  btn.disabled=true;pst.textContent='模型思考中…';pst.className='status';
  const r=await fetch(api+'/proposal',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(Object.assign({{model:document.getElementById('pmodel').value}},extra))}});
  btn.disabled=false;
  if(!r.ok){{pst.textContent='失敗：'+await r.text();pst.className='status err';return false}}
  pst.textContent=label;render(await r.json());return true;
}}
document.getElementById('propose').onclick=()=>propose({{}},document.getElementById('propose'),'提議已產生，改完按匯入，或在下面提修正意見');
document.getElementById('revise').onclick=async()=>{{
  const fb=document.getElementById('feedback').value.trim();const rst=document.getElementById('rst');
  if(!fb){{rst.textContent='先寫修正意見';rst.className='status err';return}}
  rst.textContent='修正中…';rst.className='status';
  const ok=await propose({{feedback:fb,current:collect()}},document.getElementById('revise'),'已照意見修正，再看一次');
  if(ok){{document.getElementById('feedback').value='';rst.textContent=(parseInt(rst.textContent)||0)+1+' 輪修正'}}else{{rst.textContent='修正失敗，看上面訊息'}}
}};
document.getElementById('form').onsubmit=async e=>{{
  e.preventDefault();const g=document.getElementById('go'),ist=document.getElementById('ist');g.disabled=true;ist.textContent='匯入中…';
  const r=await fetch(api+'/import',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify(collect())}});
  if(r.ok){{location.reload()}}else{{ist.textContent='失敗：'+await r.text();ist.className='status err';g.disabled=false}}
}};
{ "render(" + json.dumps(state["proposal"], ensure_ascii=False) + ");pst.textContent='上次的提議';" if state.get("proposal") and not imported else "" }"""
    return page("匯入 SilverBullet", body, script)


async def proposal_api(request: Request):
    state, err = load_job_or_404(request, require_mode="kb")
    if err:
        return err
    if not os.path.exists(os.path.join(job_dir(state["id"]), "summary.md")):
        return PlainTextResponse("要先做摘要", status_code=409, headers=NO_STORE)
    if state.get("import"):
        return PlainTextResponse("已匯入", status_code=409, headers=NO_STORE)
    body = await request.json()
    model = body.get("model") or DEFAULT_SUMMARY
    if model not in SUMMARY_MODELS:
        return PlainTextResponse("未知的模型", status_code=400, headers=NO_STORE)
    feedback = str(body.get("feedback") or "").strip()
    current = body.get("current") if feedback else None
    if feedback and not isinstance(current, dict):
        return PlainTextResponse("修正要附目前的提議", status_code=400, headers=NO_STORE)
    try:
        t0 = time.time()
        proposal = await asyncio.to_thread(make_proposal, state["id"], model, current, feedback)
        job_log(state["id"], f"proposal by {model} in {time.time() - t0:.1f}s: "
                             f"{len(proposal['pages'])} pages"
                             + (f", revision after feedback ({len(feedback)} chars)" if feedback else ""))
    except Exception as e:
        job_log(state["id"], f"proposal FAILED {type(e).__name__}: {e}")
        return PlainTextResponse(f"{type(e).__name__}: {e}"[:400], status_code=502, headers=NO_STORE)
    return JSONResponse(proposal, headers=NO_STORE)


async def import_api(request: Request):
    state, err = load_job_or_404(request, require_mode="kb")
    if err:
        return err
    if state["status"] != "done" or not os.path.exists(os.path.join(job_dir(state["id"]), "summary.md")):
        return PlainTextResponse("要先完成轉寫與摘要", status_code=409, headers=NO_STORE)
    try:
        proposal = parse_proposal(json.dumps(await request.json(), ensure_ascii=False))
    except (ValueError, json.JSONDecodeError) as e:
        return PlainTextResponse(str(e), status_code=400, headers=NO_STORE)
    with state_lock:
        state = load_state(state["id"])
        if state.get("import"):
            return PlainTextResponse("已匯入，只能做一次", status_code=409, headers=NO_STORE)
        state["import"] = {"when": dt.datetime.now().isoformat(timespec="seconds"),
                           "media": "", "source": "", "replies": ["匯入中…"]}
        state["proposal"] = proposal
        save_state(state)
    try:
        replies = await asyncio.to_thread(do_import, state["id"], proposal)
    except Exception as e:
        with state_lock:
            state = load_state(state["id"])
            state["import"] = None
            save_state(state)
        job_log(state["id"], f"import FAILED {type(e).__name__}: {e}")
        return PlainTextResponse(f"{type(e).__name__}: {e}"[:600], status_code=502, headers=NO_STORE)
    return JSONResponse({"replies": replies}, headers=NO_STORE)


routes = [
    Route("/{secret}/", drop_page, methods=["GET"]),
    Route("/{secret}/upload", upload, methods=["POST"]),
    Route("/{secret}/api/text", text_api, methods=["POST"]),
    Route("/{secret}/api/source", source_api, methods=["POST"]),
    Route("/{secret}/j/{job_id}", job_page, methods=["GET"]),
    Route("/{secret}/j/{job_id}/transcript.txt", transcript_file, methods=["GET"]),
    Route("/{secret}/j/{job_id}/summary.md", summary_file, methods=["GET"]),
    Route("/{secret}/j/{job_id}/import", import_page, methods=["GET"]),
    Route("/{secret}/api/j/{job_id}", job_state_api, methods=["GET"]),
    Route("/{secret}/api/j/{job_id}/context", context_api, methods=["POST"]),
    Route("/{secret}/api/j/{job_id}/summary", summary_api, methods=["POST"]),
    Route("/{secret}/api/j/{job_id}/proposal", proposal_api, methods=["POST"]),
    Route("/{secret}/api/j/{job_id}/import", import_api, methods=["POST"]),
]
app = Starlette(routes=routes)


class MaskSecretFilter(logging.Filter):
    """uvicorn's access log prints the request path; the path secret must
    not end up in container logs."""
    def filter(self, record: logging.LogRecord) -> bool:
        record.args = tuple(
            a.replace(SECRET, "<secret>").replace(KB_SECRET, "<kb-secret>")
            if isinstance(a, str) else a
            for a in (record.args or ()))
        return True


if __name__ == "__main__":
    logging.getLogger("uvicorn.access").addFilter(MaskSecretFilter())
    requeue_unfinished()
    threading.Thread(target=worker_loop, daemon=True).start()
    print(f"kb-audio build {BUILD_TAG}", flush=True)
    print(f"listening on http://0.0.0.0:{PORT} (path /<secret>/, secret hidden)", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", access_log=True)
