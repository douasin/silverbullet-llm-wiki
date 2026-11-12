# silverbullet-llm-wiki

[English](README.md) | **繁體中文**

Karpathy 式 [LLM wiki](https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f) 在
[SilverBullet](https://silverbullet.md) 上的實作:一套自架的知識庫,AI 經 MCP 讀寫,你開的每個 session 共用同一份。

這個 repo 是 starter kit。你在 Synology NAS 上手動裝三樣東西,接著把 [SETUP.md](SETUP.md) 交給 AI
(Claude Code 或同類工具),其餘由它搭完。

## 內容

- **AI 維護的知識庫**(筆記空間裡的 `kb/`)。session 把事實、決策與理由、發生過的事寫進去;之後每個 session
  開場 context 裡就有 index,回答前先查這裡,查不到才用自身知識。
- **MCP 橋**,架在 SilverBullet 前面:頁面工具、kb 工具、每頁的 git 歷史與還原、唯讀分享連結、狀態頁。
- **不會過期的用法說明**。工具用法只放在空間裡的一頁,由 `readme()` 工具即時送出並帶版本;帶舊版本的寫入會被拒收,
  開了很久的 session 因此會被帶去重讀。
- **每日備份與看門狗**,顧對外入口(Tailscale Funnel)。
- **Claude Code 這一側**:載入 kb index 的 SessionStart hook、要放進 `CLAUDE.md` 的規則、CLI skill。
- **選配的音檔入口**(`audio/`):丟錄音檔,拿到逐字稿與摘要,再匯入 kb。

## 與 LLM wiki 的對應

| LLM wiki | 這裡 |
|---|---|
| LLM 只讀不改的原文 | `_source` 頁,與每頁分隔線以下的原文 |
| LLM 寫的 wiki | `kb/<name>` 頁:每個分類一句總結,決策另有理由 |
| schema | 一頁手冊,由 `readme()` 送出 |
| `index.md` | `kb/index` 與每一層分類各一頁 index,靠 tag 找路 |
| `log.md` | `kb/log` |
| ingest | `kb_write`,一次一頁,分類與 tag 經你點頭 |
| query | `kb_index` → 分類頁 → `kb_read` → 總結不夠才 `kb_read_original` |
| 保持最新 | 新頁取代、併入或作廢舊頁;舊頁從此讀不到,並指向新頁 |

和 gist 不同的有兩點。總結與原文分開讀,AI 只在需要時才付原文的 token。完整性(index 行、雙向連結、作廢的頁)
由工具在寫入當下保證,所以沒有另外的 lint。

## 目錄

| 路徑 | 內容 | 放到哪 |
|---|---|---|
| `silverbullet/` | SilverBullet 的 compose | NAS `/volume1/docker/silverbullet/` |
| `bridge/` | MCP 橋容器、看門狗與備份腳本 | NAS `/volume1/docker/silverbullet-mcp/` |
| `audio/` | 選配:音檔 → 逐字稿 → kb 的容器 | NAS `/volume1/docker/kb-audio/` |
| `space/` | 系統需要的頁:手冊、首頁骨架、`Config/` | SilverBullet 空間 |
| `claude/` | hook、`settings.json` 區塊、`CLAUDE.md` 區塊、CLI skill | 你的電腦,`~/.claude/` 底下 |
| `tools/` | 把新頁歸進分類的審核腳本 | 你的電腦 |

## 需求

- Synology NAS,裝好 Container Manager、開 SSH、裝 Tailscale。其它 Docker 主機也行,路徑自己調。
- 電腦上有能跑 shell 的 AI 工具,Python 3.11 以上。
- 要給 claude.ai(網頁或手機)用:需要 Tailscale Funnel,因為它的 connector 只連公網 443 的 HTTPS。
- 要用 `audio/`:一把 OpenRouter API key(轉寫)與一把 Anthropic API key(摘要)。

## 開始

1. 裝 Container Manager、開 SSH、裝 Tailscale 並登入。
2. 在電腦上 clone 這個 repo。
3. 對你的 AI 說:「讀 SETUP.md,照著搭。」只有你能做的幾件事它會來問。

## 語言

手冊頁、工具回覆與拒收訊息是正體中文。工具本身不限語言;想讓 AI 看到的文字換成別的語言,翻譯
`space/AI 守則/SilverBullet 維護手冊.md`。

## 授權

[MIT](LICENSE)。有一頁是第三方內容:`space/Config/MobileTapShield.md` 裡的程式碼來自
[SilverBullet 社群論壇的討論串](https://community.silverbullet.md/t/fixing-workaround-the-silverbullet-codemirror-mobile-keyboard-drama-disable-accidental-taps/3709),
屬於原作者。
