---
name: silverbullet
description: 讀寫 NAS 上的 SilverBullet 筆記空間（新增/更新/讀取/刪除頁面與附件）。當使用者要求「寫文件到 silverbullet」「更新筆記」「把東西存到 silverbullet」時使用。
---

# SilverBullet 筆記空間操作

SilverBullet 跑在 Synology NAS 的 Docker 容器裡，筆記是純 Markdown 檔。
連得到 SilverBullet MCP 工具時優先用 MCP 工具（先呼叫 `readme()`）；本 skill 的 CLI 是不經 MCP 的直連路徑。

## 連線資訊

位址與憑證不寫在這裡，由環境變數提供（Claude Code `settings.json` 的 `env` 區塊）：
`SB_URL`（內網位址）、`SB_URL_EXT`（選填，第二個位址）、`SB_TOKEN`（API token）。
實體檔案位置（NAS）：`/volume1/docker/silverbullet/space/`

## HTTP API（重要規則）

真正的檔案 API 在 **`/.fs/` 路徑**下。直接 PUT/GET `/{path}` 會被前端
catch-all 路由吃掉並回 200 假象，**看似成功實際沒寫入**。

1. **驗證**：帶 header `Authorization: Bearer <SB_TOKEN>`。
2. **所有 `/.fs` 請求都要帶 header `X-Sync-Mode: true`**，否則回 SPA HTML。
3. 端點：
   - 列出全部檔案：`GET /.fs` → JSON（`name`, `size`, `perm` 等；
     `Library/` 開頭且 `perm: ro` 的是內建檔，勿動）
   - 讀檔：`GET /.fs/{path}`
   - 寫檔（新增或覆蓋）：`PUT /.fs/{path}`，body 為 UTF-8 bytes，
     `Content-Type: text/markdown`（圖片用對應 MIME）
   - 刪檔：`DELETE /.fs/{path}`
4. 路徑含中文/空格要 percent-encode（`urllib.parse.quote`，保留 `/`）。
5. **編碼陷阱**：用 Python requests 讀檔時必須用 `r.content.decode('utf-8')`，
   不可用 `r.text`（伺服器不回 charset，requests 會猜成 latin-1 造成亂碼；
   若把這種亂碼再寫回去會永久損壞檔案內容）。
6. 寫入後用 `GET /.fs` 確認檔案出現、大小合理，才算成功。

## 現成 CLI 工具

本 skill 資料夾內附 `sb.py`，已處理驗證、編碼、路徑等細節：

```
python ~/.claude/skills/silverbullet/sb.py list
python ~/.claude/skills/silverbullet/sb.py get "頁面路徑"        # 不含 .md 亦可
python ~/.claude/skills/silverbullet/sb.py put "頁面路徑" 本地檔.md
python ~/.claude/skills/silverbullet/sb.py delete "頁面路徑"
```

`sb_backup.py` 把整個空間與 git 歷史打包到本機（另需 `NAS_CREDENTIALS` 指向 NAS 的 SSH 登入檔）。

## SilverBullet 撰寫慣例

- 頁面路徑 = 檔案路徑，`/` 就是資料夾，例：`專案名/Documents/xxx.md`。
- 頁面間連結用 wiki 語法，**從 space 根目錄解析**：`[[完整/路徑|顯示文字]]`。
- 圖片/附件放 `attachments/` 資料夾，嵌入用 `![[attachments/檔名.png]]`
  （不要用一般 Markdown `![](...)` 相對路徑，會受頁面深度影響）。
- 首頁是 `index.md`，含 Lua 查詢區塊（`${...}`），修改時保留原有區塊。
- `kb/` 底下的頁只經 MCP 的 kb 工具讀寫，不用本 CLI 改。
