# kb-audio — 音檔 → 逐字稿 → 摘要 → 匯入 kb

NAS 容器 `kb-audio`（Container Manager 專案 `/volume1/docker/kb-audio`），Tailscale Funnel 8443 → 8009，
區網 `http://<NAS-IP>:8009/…`。**兩個入口、兩把 path secret、同一個容器**，秘密只在 NAS 的 `.env`：

| 入口 | 網址 | 給誰 | 有什麼 |
|---|---|---|---|
| 公用 | `https://<node>.<tailnet>.ts.net:8443/<AUDIO_PATH_SECRET>/` | 任何拿到連結的人 | 拖音檔 → 逐字稿 → 一次性摘要 → 下載；看不到匯入 |
| kb | `https://<node>.<tailnet>.ts.net:8443/<KB_PATH_SECRET>/` | 使用者本人 | 同上 + 拖檔前的「背景」欄位 + 匯入 SilverBullet |

job 屬於建立它的入口：用另一把 secret 開同一個 uuid 一律 404；匯入三個 API 只認 kb 入口。

## 用法

1. 開入口頁，選轉寫模型（預設 `qwen/qwen3-asr-1.7b`，可切 `openai/whisper-large-v3`）；kb 入口多一個
   「背景／你已知的重點（選填）」textarea，拖檔前填，內容隨 job 存檔，摘要與匯入提議的 prompt 都會帶上
   （prompt 寫明：用背景理解錄音與校正術語，不可把背景裡逐字稿沒有的事寫成錄音內容）。
   然後拖音檔進去（wav / mp3 / m4a / flac / ogg / webm / aac；公用入口上限 `MAX_UPLOAD_MB`＝2048，
   kb 入口 `KB_MAX_UPLOAD_MB`＝0 不限）。拖檔即上傳、轉寫立刻開始。
2. 公用入口上傳完自動跳到 `/<secret>/j/<uuid>`；kb 入口**不跳頁**，停在原地讓你把背景寫完，按
   「儲存背景並前往轉檔頁」才過去（背景在匯入頁還能再改）。重整不丟，狀態存在 `jobs/<uuid>/state.json`。
   頁面每 3 秒輪詢：排隊 → 轉寫中 n/m 段 → 完成。完成後顯示逐字稿，可下載 `transcript.txt` 或按「複製逐字稿」
   進剪貼簿（摘要同樣有「複製摘要」；非 https 的區網頁面走 execCommand 備援）。
3. 「Summary」只能按一次（第二次伺服器回 409）；模型預設 `claude-fable-5-1`，可切 Opus 5.5 / Sonnet 5.5 /
   Haiku 5.5；可下載 `summary.md`。摘要末尾有「逐字稿修正」節：轉寫的誤聽（同音、近音、外文拆字）由模型依背景與
   前後文校正，摘要用校正後的詞並列出修正清單。
4. （kb 入口）「→ 匯入 SilverBullet」到 `/<KB_PATH_SECRET>/j/<uuid>/import`：按「產生提議」，模型拿背景 + summary + 逐字稿 +
   `kb_index`（現有分類與 tag）回 JSON：name、每頁 kind（knowledge / decision）、index path、tags、
   總結一句（硬上限 `SUMMARY_MAX_CHARS`＝80 字，細節進理由或原文）、decision 的理由，以及 `corrections`
   （產品名／人名／術語的誤聽修正，最多 10 條，拿不準的標「待確認」）——頁面在表單上方列出讓你核對。
   表單每欄可改；不想手改就在「跟模型討論」寫修正意見按「重新提議」：
   模型拿目前表單（含你手改的）+ 這輪意見 + 之前每輪意見重出一版，次數不限，每輪存在
   `state.json` 的 `proposal_rounds`。頁上也有背景欄位可填可改（「儲存背景」），重新提議會用新背景。
   滿意了按「匯入」= 點頭；只能匯入一次。
   匯入動作：音檔複製到 `/volume1/kb-media/<YYYYMMDD>_<name>.<ext>` → 橋 `kb_write(kind=source, file=…)`
   原文 = 逐字稿 → 每頁 `kb_write(kind, source={"type":"source"})` → SilverBullet 首頁「## kb 音檔記錄」
   段底下插一行 `- <匯入日> <原檔名>（n 分） → [[原文]] [[1]] [[2]]…`（最新在上；首頁沒有那個標題就略過並在回覆說明）
   → 顯示橋回的 OK 與待審頁名。
   之後照一般週審流程搬行。

## 原文直存 API（無頁面、無 job）

`POST /<KB_PATH_SECRET>/api/source`，multipart `file`（.txt / .md）+ `name`（小寫英數底線，不含日期、
不含 `_source`）+ `index`（之後引用它的頁所在分類，或最近共同母層）。容器直接經橋
`kb_write(kind="source", original=檔案全文, index=…)`，回 `{"page": "kb/<YYYYMMDD>_<name>_source", "reply": …}`；
source 進待審頁（行標 `#source`），週審搬進該分類。不建 job、不摘要、不提議。給「知識頁由呼叫端自己寫、
只有原文太大不能經模型」的情況；寫完各頁用 `source={"type":"source","name":"<YYYYMMDD>_<name>_source"}` 連回。

```
curl -F "file=@todo.txt" -F "name=screeps_todo" -F "index=game/screeps" http://<NAS-IP>:8009/<KB_PATH_SECRET>/api/source
```

## 文字檔 API（無頁面）

`POST /<KB_PATH_SECRET>/api/text`，multipart `file`（.txt / .md）+ 選填 `context`。檔案原樣存成 job 的
逐字稿（只做 CRLF→LF、UTF-8 解碼），job 直接是完成態，回 `{"id","url"}`；之後在 `url` 那頁走摘要 → 匯入，
與音檔完全同一條鏈（匯入時原檔也會複製一份到 kb-media）。用途：原文大到模型單次輸出放不下時，
讓檔案不經模型進 kb。公用 secret 打這個端點 404；副檔名不對 400；kb 入口上限（預設不限）照 `KB_MAX_UPLOAD_MB`。
既有拖檔頁與 upload 流程沒有改動。

```
curl -F "file=@todo.txt" -F "context=這是 screeps 的待辦清單" http://<NAS-IP>:8009/<KB_PATH_SECRET>/api/text
```

## 實作重點

- Starlette + uvicorn，單一背景 worker 依序轉寫；容器重啟時 `queued` / `transcribing` 的 job 重排，
  已轉完的段（`chunks/NNN.txt`）不重轉。
- 轉寫：ffmpeg 先切 10 分鐘一段（16 kHz mono wav），逐段 POST OpenRouter
  `/api/v1/audio/transcriptions`（multipart）；每段寫完就更新進度；逐字稿每段前綴 `[hh:mm:ss]`
  （模型有回 segments 就用 segment 時間，否則用段起點）。
- 摘要與提議：Anthropic Messages API（`ANTHROPIC_API_KEY`），prompt 在 `server.py`
  的 `SUMMARY_PROMPT` / `PROPOSAL_PROMPT`；提議 JSON 在伺服器端做格式檢查（name、kind、index、
  tags 小寫、decision 要 reasoning），不合就回 502 讓你重按。每輪成本（36 分錄音、17k token 進）：
  Fable 約 $0.45、Opus 約 $0.12、Sonnet 約 $0.06、Haiku 約 $0.003。
- 背景欄位是正名的唯一可靠來源：產品名、人名、術語寫進去，模型才校得準（2026-11-08 實測：沒寫產品名時
  三個模型都照逐字稿寫「Cloud Tag」；寫了「產品是 Claude Code」才全篇正名）。
- 匯入走橋的 MCP（`MCP_URL`，fastmcp client），不直寫 SilverBullet。
- 安全：uuid4 不可列舉、沒有列表頁；secret 錯 404；每個回應 `X-Robots-Tag: noindex`、
  `Cache-Control: no-store`；uvicorn access log 把 secret 遮成 `<secret>`。
- 觀測：`jobs/<uuid>/log.txt` 每段耗時、摘要／提議耗時與字數、匯入結果；容器 stdout 同步一行一事件。

## 部署與維運（Container Manager）

- 程式碼 `server.py` 由 compose 以 volume 唯讀掛進 `/app`：改程式 = 推檔到
  `/volume1/docker/kb-audio/server.py` → 專案 stop 再「建置」（只改掛載檔時 compose 認為設定沒變，單按建置不會重建容器；
  API 的 `start` 在 stop 之後也不會真的起容器，stop → build 才會用新檔重建並啟動；UI 的重啟按鈕以實機為準）。改 `requirements.txt` / `Dockerfile` 要重建 image：
  Container Manager 的建置 API（`SYNO.Docker.Project build`）實測只重建容器不重建 image，
  要先在專案目錄 `docker compose build`，再按建置讓它用新 image 重建容器（2026-11-08 實測）。
  專案 id 用 `SYNO.Docker.Project list` 查（只 grep id／name，不印整包）；`build` 偶爾回 `success: false` 但容器已重建，
  以 `docker ps` 與 log 的 build 行為準。
- 選檔框的 `accept` 由 `ALLOWED_EXT` 生成副檔名清單再加 `audio/*,audio/mp4,audio/x-m4a`：只寫 `audio/*` 時
  iOS 的檔案選取器會把語音備忘錄的 .m4a 反灰。
- 看容器 log 或 `docker inspect` / DSM 專案 API 時注意：Env 含全部密值，只挑欄位印、不印整包。
- `.env`：`AUDIO_PATH_SECRET`（公用）、`KB_PATH_SECRET`（kb）、`OPENROUTER_API_KEY`、`ANTHROPIC_API_KEY`、
  `MCP_URL`、`MAX_UPLOAD_MB`（公用上限）、`KB_MAX_UPLOAD_MB`（kb 上限，0 = 不限）（樣板 `.env.example`）。輪替 secret = 改 `.env` 那行 → 專案建置（env 變了要重建容器）。
- Funnel：`tailscale funnel --bg --https=8443 8009`；橋的 `funnel_watchdog.sh` bounce 時會連 8443 一起重掛。

## 已知限制

- 轉寫模型常吐簡體字；逐字稿每段都過 OpenCC `s2twp`（簡→繁台灣用詞，软件→軟體、网络→網路）
  再落盤。只轉逐字稿，摘要本來就是正體。
- 轉寫 API 不回 segments 時，時間戳只有每 10 分鐘一個（段起點）。
- 切段在固定秒數，段界會切斷句子，接縫處可能重複或漏字。
- kb 還沒有任何分類時，提議模型會自己編大類；分類骨架建起來後它會優先用既有的。
