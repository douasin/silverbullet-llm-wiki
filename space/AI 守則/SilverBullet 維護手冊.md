# SilverBullet 維護手冊

${widgets.commandButton("📋 複製整頁", "Copy Page Content")}

給透過 MCP connector 操作這個筆記空間的 AI。這頁就是 `readme()` 回的內容,是這個空間與所有工具用法的唯一說明;
各工具自己的說明只有一句用途。工具:readme / list_pages / read_page / write_page / delete_page,
版控三件組 page_history / page_diff / restore_page,
知識庫六件 kb_index / kb_read / kb_read_original / kb_write / kb_deprecate / kb_append_index_tags。
**動手前把這頁讀完**。

`readme()` 回的第一行是版本,這頁一改版本就變。`kb_write` 與 `kb_deprecate` 的 `readme` 參數填這個版本。
工具拒收說版本不是現行的,或 `kb_index` 結果第一行的版本和你讀到的不同,表示這頁改過:重新呼叫 `readme()`
讀完,照新的做。

## 這個空間是什麼

使用者的個人筆記空間:筆記與文件、AI 守則、AI 知識庫(kb)、系統與配置頁。
首頁是 `index`,上半是 widget,下半是分類段落(AI 守則 / 知識庫 / kb 音檔記錄 / 系統,以及使用者自己加的段落),
每個頂層主題一個 hub 頁,子頁用 `/` 分層(如 `專案名/Documents/主題`)。
「kb 音檔記錄」段由 kb-audio 容器自動寫入,不手編。

## 工具使用鐵則

1. **write_page 是整頁覆寫**——必先 read_page 拿全文,改動目標段落,
   其餘內容**一字不動**地寫回。絕不憑記憶或摘要重寫整頁。
2. **`${...}` 開頭的行是 widget**(按鈕、查詢),不是文字內容——
   編輯時原樣保留,尤其 `index` 頁上半部整片都是 query widget,別碰。
   widget 可以橫跨多行(`${query[[` 到 `]]}`),**區塊內插入任何空行都會把它弄壞**。
3. **保留原始格式**:寫回時逐字保留原有的換行、空行結構與縮排,
   只動目標段落。**禁止整頁重排版**(如每行後補空行、重新斷行)。
4. **delete_page 只在使用者明確要求刪除時使用**,不確定就先問。
5. **封存頁不得讀寫**:`Archive/` 底下=使用者封存區。工具已強制——
   list 不顯示、read/write/delete 一律拒絕;**經使用者在本對話明確允許後**,
   才能帶 `allow_archived=true` 存取。
   需要其內容先問「已封存,要打開嗎?」;封存/解封由使用者按
   `Config/ArchivePage` 的指令操作,AI 不代行。
6. 路徑規則:`/` 分層、**不加 .md**(自動補)、中文頁名正常使用。
   `Library/` 開頭的是系統頁,一律不動。
7. 寫入成功的判定:`write_page` / `restore_page` 回 `OK ... verified`;kb 工具回以 `OK` 開頭的結果,
   不回讀、沒有 verified 字樣,這是設計。失敗一律是工具直接回錯誤(含原因);沒拿到 OK 就回報使用者,
   不要重試迴圈。
8. 不經 MCP 工具、直接寫 HTTP `/.fs/` API 的 AI:頁面內容**從產生那一刻就必須是
   LF**(用不會插入 CRLF 的工具產檔,如 python `open(..., newline='\n')`;
   別用 PowerShell here-string / Set-Content)。含 `\r` 的寫入一律視為錯誤,
   先正規化再寫——CRLF 會弄壞 frontmatter 解析與 share 判定。

## 版本控制(page_history / page_diff / restore_page)

整個空間在 git 版控下:每次 MCP 寫入自動 commit(訊息含工具與來源端點),
瀏覽器手改由每 10 分鐘的 sweep 收錄。

- `page_history(path)`:列該頁修訂(rev、時間、動作來源),新到舊。
- `page_diff(path, rev)`:該修訂與現況的差異。
- `restore_page(path, rev)`:還原到該修訂——走正常 API 寫回,
  還原本身也入版控,可再反悔。
- 用途:使用者說「這頁被改壞了」→ history 找出壞掉前的 rev → diff 確認 →
  restore。**改壞東西不要企圖手工重建,用 restore**。
- 只處理 .md 頁;rev 用 history 列出的短 hash。

## 常見任務的正確做法

**更新既有頁的一段**:read_page → 只改該段 → write_page 全文寫回。

**新增一般筆記頁**:直接 write_page 到適當的階層下
(如 `專案名/Documents/新主題`);快速隨手筆記放 `Inbox/`。

**新增頂層主題頁**:建頁後,read_page `index` → 在對應的分類段落加一行 `- [[頁名]]` → 寫回 index。
同理,hub 頁下新增子頁時,把連結補進 hub 頁清單。
**鐵則:每一頁都必須有從首頁點得進去的路徑**(index → hub → 頁),建頁當下就補目錄連結,不留孤兒頁。
`kb/` 底下的頁例外:它們由 kb 工具登記進 kb 的 index,不手動加進首頁。

**share 有兩級**:`share: true` = 對外唯讀;`share: edit` = 協作頁,
外部盟友端點(ally)可讀寫——這級只有使用者明確指定才能設。

**開啟/取消頁面的公開分享**:在該頁 frontmatter 加/移除 `share: true`
(整份 frontmatter 其它欄位保留)。標了的頁面可經公開分享連結被外人唯讀瀏覽,
所以使用者要求分享才加,取消分享=移除該行。分享連結格式由伺服器管理,
使用者頁面上的 share 按鈕會自動複製,你不需要組 URL。
**更新頁面內容時 share 狀態原樣保留**——frontmatter 是頁面狀態不是內容,
只有使用者明確要求分享/取消分享時才動它。

**文件型頁面的慣例**:頁首 `# 標題` 後放一行複製按鈕 widget:
`${widgets.commandButton("📋 複製整頁", "Copy Page Content")}`,
內文用 `[[頁名|顯示文字]]` 互連。

## 知識庫(kb/)

`kb/` 由 kb 工具管理。能直接 `read_page` 的只有分類頁(`kb/index/...`,用來找路);根 index 看 `kb_index` 的結果,
知識頁一律經工具讀寫。不要 list_pages 掃 `kb/`、不要 write_page 改 `kb/` 下的頁。
index 頁上每一行是兩種之一:子分類行 `- [[kb/index/<路徑>]] #tag…`(tag 是路由用的主題與子分類名),
或知識頁行 `- [[kb/<name>]] #<型態> — <總結>`(型態是 knowledge / decision / record / source 之一)。
所有 kb 工具的 `name` 不含 `kb/`(行上顯示 `[[kb/<name>]]`,傳 `<name>`)。

**讀**
1. session 開頭要有一份 `kb_index` 的結果(Claude Code session 由 hook 放進 context;context 裡沒有就自己呼叫一次;
   之後要看最新的內容再呼叫)。
2. 問題主題對得上根 index 哪一行,就 `read_page` 那行的 `[[kb/index/…]]` 連結,一層層往下:每一頁上,
   子分類行比 tag、知識頁行比總結文字;上一頁對上的 tag 若是子分類名,這一頁點連結路徑以該名結尾的那一行。
   根 index 的「已廢止」行是作廢清單,不用來回答。
3. 對上的知識頁 `kb_read(<name>)` 拿總結;不夠才 `kb_read_original(<name>)`。作答註明來源頁 `kb/<name>`。
   已作廢的頁 `kb_read` 會拒讀並說明;被取代或併入別頁的會指出改讀哪一頁,照它說的讀。
4. `kb_index` 結果最後一區是近期新增的頁,分類頁上還看不到:每次找知識這一區也要比對(行上 `→` 後面是它的分類),
   對得上直接 `kb_read`。
5. 要推翻或偏離既有設計前,先在相關分類找型態為 `#decision` 的行、`kb_read` 讀理由,回報時引用。
6. 對不上就用自身知識作答並標明未命中;問題明明屬於某分類但 tag 沒把你帶過去,徵得使用者許可後
   `kb_append_index_tags(index, tags)`(`index` 是那個分類的路徑,`tags` 是要補的詞)。

**寫**:kb 裡的頁寫了就不能直接改。要動任何一頁(不論是誰、什麼時候寫的,包含你剛寫錯的)只有下面四種做法,
照「舊頁的東西要留下什麼」選:

| 要的結果 | 做法 | 舊頁的總結與理由 | 舊頁的原文 |
|---|---|---|---|
| 舊頁錯了或過時,由新頁接手 | 寫新頁,`merge_target` 用 `replace` | 不留 | 不留 |
| 兩頁講同一件事,併成一頁 | 寫新頁,`merge_target` 用 `merge` | 不留,以新頁的為準 | 留,併進新頁 |
| 舊頁不成立,新頁講的是另一件事 | 寫新頁,`merge_target` 用 `deprecate` | 不留 | 不留 |
| 舊頁不成立,沒有新頁 | `kb_deprecate` | 不留 | 不留 |

「不留」是那些內容從此讀不到。所以用 `replace` 時,舊頁裡還要的內容與原文,必須由新頁自己的總結與 `source` 帶齊;
舊頁的原文還要就用 `merge`(原樣併入,不能挑)。新頁用另一個 `name`,同名會被拒收。工具回 `OK` 就是完成,
不必回頭確認舊頁。

1. 先向使用者報:現有分類(kb_index 的清單)、這篇放哪(可多個;沒合適的就提新分類,任一層)、哪些 tag、
   型態(knowledge / decision / record)。知識頁的分類不得提 `inbox`,那是使用者想不到分類時自己選的。
   kb 裡已經有同一對象的知識或決策時(分類頁與近期新增區的行看得出來),一併報上表哪一種做法。
   使用者點頭才 `kb_write`,一次寫一頁。新分類直接填在參數裡,不必先建。
2. 沒有新頁、只是某條既有知識該作廢:使用者點頭後 `kb_deprecate(name, reason, readme)`(`readme` 填版本)。
   成功回以 `OK kb/<name>` 開頭。
3. 一篇原文產多頁:先 `kb_write(kind="source", …)` 存整篇一次,各頁 `source={"type":"source","name":"<那個 source 頁名>"}`
   連回同一頁;不可把片段用 `type=text` 分貼各頁。source 頁的 index 照規則填:各頁同分類就填那個分類,
   不同分類填最近共同母層,連大類都不同填 `inbox`。source 頁本身不會被取代或作廢。
4. 原文大到一次輸出不完(幾十 KB 以上,不論產幾頁):不分段、不貼片段,檔案本身送 `api/source`,之後各頁用
   `type=source` 連回它回的頁名:
   `curl -F "file=@<檔案.txt|.md>" -F "name=<小寫英文字根,不含日期與 _source>" -F "index=<分類或最近共同母層>" <kb入口>api/source`
   → `{"page": "kb/<頁名>"}`。`<kb入口>` 是首頁「知識庫」段「匯入 kb」那條網址。能跑指令的 session 自己跑;
   沒有檔案與 HTTP 能力的 AI(claude.ai 等)把 curl 填好交給使用者跑,拿到頁名再寫各頁。(使用者在瀏覽器用
   拖檔頁或 `api/text` 匯入時,容器會自己建 source 頁與各頁,AI 不再 kb_write。)

**`kb_write` 的參數**(成功回以 `OK kb/<name>` 開頭;被拒收會列出要改的每一項)
- `readme`:`readme()` 回的版本。
- `name`:8 位日期(寫入當天)+ `_` + 小寫英數字根,例 `20260101_nas_backup_schedule`;source 頁以 `_source` 結尾。
- `kind` 決定還要填哪些:
  - `knowledge`(事實、做法)、`record`(發生過的事):`summaries` + `source`
  - `decision`(使用者定案的選擇,要留下為什麼):`summaries` + `source` + `reasoning`
  - `source`(多頁共用的一份原文):`original` + `index`
- `summaries`:`[{"index": "<分類路徑,如 infra/nas/backup>", "text": "<一句總結>", "tags": ["<詞>", …]}, …]`,
  這頁屬於幾個分類就幾筆;分類是 kb_index 清單裡的,或使用者點頭的新分類。decision 的 `text` 寫定案結果本身
  (「<對象> 採 <結果>」)。
- `reasoning`:為何這樣定、考慮過什麼、為何否決。
- `source`:這頁的原文在哪,三選一。
  `{"type": "page", "page": "<SilverBullet 頁名>"}` 原文是空間裡既有的頁;
  `{"type": "text", "original": "<全文>"}` 原文只產這一頁(一篇文章、一段逐字稿、談定這件事的那段對話);
  `{"type": "source", "name": "<source 頁 name>"}` 原文已存成 source 頁(一篇產多頁,或經 `api/source` 送進來的大檔)。
- `original`、`index`:整篇原文,與第 3 點定出的分類。
- `merge_target`(選填):這頁對一頁既有的 knowledge / decision / record 的處理,三選一,結果見上表。
  `{"replace": "<舊頁 name>", "reason": "…"}`、`{"deprecate": "<舊頁 name>", "reason": "…"}`、
  `{"merge": "<舊頁 name>"}`(不帶 reason)。`replace` 與 `merge` 要新舊兩頁同型態、同分類。

## 寫作風格

- 正體中文、簡潔列點、結論先行;表格用於可枚舉的對照。
- 技術內容照使用者原用語(按鍵名、參數名保留英文)。
- 不確定使用者要的層級或位置時,先列出你打算放哪裡再動手。

## 空間地圖(依首頁分類段落;使用者加了新的頂層主題就補一行)

- `index` — 首頁(上半 widget 區勿動,下半分類段落)
- AI 守則:`AI 守則/…`(給 AI 的守則,本頁在此)
- 知識庫:`kb/…`(經 kb 工具與 index 頁存取,見「知識庫」節);首頁同段還有 kb-audio 的入口網址
- kb 音檔記錄:沒有自己的頁,是首頁上由 kb-audio 容器自動寫的一段,每次匯入一行連到 `kb/` 下的頁;不手編
- 系統:`Config/SystemStatus`、`Config/FunnelRestart`、`Config/ArchivePage`(封存機制,見鐵則 5)
- 其它:`Inbox/`、`Journal/`(SilverBullet 內建快速筆記與日誌)、`Archive/…`(封存區,見鐵則 5)、
  `Trigger/…`(由排程與工具覆寫,不要手編)、`Library/`(系統頁,不動)

