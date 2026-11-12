# silverbullet-mcp — 手機 Claude 直連 SilverBullet

把 SilverBullet 的頁面操作(list / read / write / delete)包成 remote MCP server,
給 claude.ai 的自訂 connector 用。部署在 NAS 上之後,手機 Claude 的**普通聊天**
就能直接讀寫筆記,不需要任何電腦開著。

## 安全模型(為什麼這樣設計)

claude.ai 自訂 connector 只能填一個 URL,不能帶自訂 header,所以驗證分三層:

1. **路徑密鑰(必備)**:MCP 端點只存在於 `/<MCP_PATH_SECRET>/mcp`,
   密鑰是 32 bytes 隨機字串(capability URL)。掃描器打其它路徑一律 404。
2. **HTTPS(必備)**:容器以 DSM 匯出的憑證直接講 TLS(或走反向代理),
   密鑰不落明文網路;未設憑證時不得對外開 port。
3. **Bearer token(選配)**:`MCP_BEARER_TOKEN` 設了就強制檢查
   `Authorization` header——claude.ai connector 用不到,留給日後
   API/自寫客戶端當第二層。

SilverBullet 的帳密只存在 NAS 上這個容器的 `.env`,不經過 Claude 對話,
MCP 介面也只暴露頁面 CRUD,碰不到 NAS 檔案系統。

## 部署(現行:Tailscale Funnel 模式)

claude.ai connector 後端**只連 443**(其它 port 在 TCP 層被 reset,伺服器連 log
都不會有)。本機 443 不可用時,用 Tailscale Funnel 讓 443 終結在 Tailscale 邊緣:
NAS 只有 outbound 連線,router 零變動。

1. NAS 裝 Tailscale 套件並登入(裝置名即 ts.net 子網域)。
2. `.env` 的 `SSL_CERTFILE` / `SSL_KEYFILE` 留空(TLS 交給 Funnel,容器講明文)。
3. 起容器後,SSH 跑:
   ```
   tailscale funnel --bg 8008
   ```
   (Synology CLI 完整路徑 `/var/packages/Tailscale/target/bin/tailscale`;
   首次會要求到 admin console 授權 Funnel。)
4. connector URL:`https://<裝置名>.<tailnet>.ts.net/<MCP_PATH_SECRET>/mcp`
5. Funnel 開著時原本對外的 port forward(如 3002)可關閉,縮小攻擊面。

**已知故障:ts.net 網域全球 NXDOMAIN,但 NAS 上 `funnel status` 一切正常**
——控制面把 DNS 記錄撤了、節點不知情(2026-07-29 發生過;嫌疑:Tailscale
client 版本過舊)。處置:
```
tailscale serve reset && tailscale funnel --bg 8008
```
DNS 立即重發布;客戶端可能還有幾分鐘 NXDOMAIN 負快取。診斷順序:
先 `nslookup <ts.net 網域> 8.8.8.8`(NXDOMAIN=此問題),再看容器與 funnel status。

**第二個 Funnel(2026-11-08 起)**:`tailscale funnel --bg --https=8443 8009` 把
kb-audio 容器(`infra/audio/`)掛在 `https://<node>.ts.net:8443`;watchdog 的 bounce 會
連這條一起重掛,重啟結果裡若寫「kb-audio 代理沒起來」就手動再下一次上面那行。

**自動化(funnel_watchdog.sh,DSM 任務排程器每分鐘以 root 執行)**:
- 看門狗:本地 funnel 設定在、但 8.8.8.8 與 1.1.1.1 都查無 ts.net 網域
  → 自動執行上述重啟(10 分鐘冷卻)。
- 手動:SilverBullet 首頁「系統」→ Funnel 重啟按鈕(`Config/FunnelRestart`)
  → 寫入 `Trigger/funnel-restart` 觸發頁 → 下一分鐘被腳本消費執行;
  結果寫在 `Trigger/funnel-restart-result` 頁。觸發頁內容不被解讀。
- 腳本位置:`/volume1/docker/silverbullet-mcp/funnel_watchdog.sh`
  (本資料夾同名檔為原稿);改 HOST/port 記得同步改腳本開頭常數。
- **每日備份+GitLab push(同腳本的 C 段)**:每日 04:00 後首個分鐘,
  space+history git 打包到 `/volume1/docker/silverbullet-backups/`
  (保留 14 份自動輪替),並 push 到腳本開頭 `GITLAB_REPO` 指的私有 repo
  (token 在 `/volume1/docker/silverbullet-mcp/.gitlab-token`,600 root;
  輪替=覆寫該檔)。結果回寫 `Trigger/backup-status.md`(狀態端點會顯示)。
- **排程接法(DSM 最小間隔=每小時)**:DSM 任務設**每小時**執行
  `bash /volume1/docker/silverbullet-mcp/funnel_watchdog_hourly.sh`——
  wrapper 以 flock 防重疊,在小時內每 60 秒跑一次 funnel_watchdog.sh,
  約 58 分鐘後退出交棒;中途死亡最多一小時後自癒。心跳:
  `/tmp/funnel_watchdog_hb`(unix 秒,新鮮=循環活著)。
  不要把任務直接指到 funnel_watchdog.sh——每小時只跑一發的話,
  按鈕反應變一小時、且 00:00 整點發會被備份的 04:00 門檻擋掉。

## 公開分享連結(share)

單頁唯讀分享:`GET /share/<SHARE_SECRET>/<頁名>` 會把頁面渲染成 HTML
(濾掉 `${...}` widget、`[[wikilink]]` 攤平為文字、表格/程式碼正常呈現)。

- **逐頁 opt-in**:只有 frontmatter 含 `share: true` 的頁面可讀,
  其它頁就算網址正確也是 404;`Library/` 與非 .md 檔一律 404。
- `SHARE_SECRET` 在 `.env` 設定(與 MCP 密鑰分開、可獨立輪替);留空=整個功能關閉。
- SilverBullet 端的開關:`Config/SharePage` 定義了頂欄 share 按鈕與
  「Share: Toggle Page Share」指令——切 frontmatter 並把連結複製到剪貼簿。
  **輪替 SHARE_SECRET 時要同步改該頁裡的 `SHARE_BASE`。**
- 分享頁面回應帶 `noindex` 與 `Cache-Control: no-store`,取消分享即刻生效。

## API token 認證

SilverBullet 容器設 `SB_AUTH_TOKEN`(compose.yaml,已備份 .bak),
橋(`.env` 的 `SB_TOKEN`)、sb.py、sb_backup.py 全改走 Bearer——
無過期、不觸發登入鎖定;密碼只留給人登入 UI。輪替:改兩邊+重啟。
token 未設時各工具自動回退帳密 cookie 模式。

## 封存區(Archive/)

`Archive/` 前綴=使用者封存頁,AI 工具強制隔離:MCP 與 sb.py 的
list 隱藏、read/write/delete 拒絕(使用者允許後 `allow_archived=true` /
`--archived` 才放行);share 對封存頁一律 404;ally 拒絕。
封存/解封=SilverBullet 上 `Config/ArchivePage` 的兩個指令(搬頁)。
真正繞過所有工具手寫 HTTP 的 session 攔不住——那是違反手冊的行為,
版控會留下證據。備份(sb_backup.py)包含封存頁。

## 系統狀態端點

`GET /status/<SHARE_SECRET>`:HTML 一頁看 build 版本、容器運行時間、
空間統計、最近 5 筆 commit、funnel 最後重啟結果、最後備份狀態
(後兩者讀自 `Trigger/…` 頁;備份狀態由 sb_backup.py 每次成功後回寫)。
SilverBullet 的 `Config/SystemStatus` 頁放著入口連結。

## SilverBullet 端 UI 註記

- treeview plug(`_plug/treeview.plug.js`)是**修補版**:面板 iframe
  完全吃不到 space-style(變數與 class 規則都進不去),未修補時所有
  顏色都跟 SB 內建預設的深淺跑。修補=把內嵌 `assets/treeview.css`
  (base64 打包在 plug js 裡)中的**全部**外部變數引用烤成固定
  mod_tcsoft 配色:底 floralwhite、黑字、棕資料夾、當前頁 #d1cdc5 灰底
  +橘框、工具列米白。側欄因此不分深淺恆定同色。
  **更新 treeview 版本=重跑本資料夾的 `patch_treeview.py`**(自動:
  下載最新版 → 套配色 → 上傳空間;上游 css 改版對不上時會列警告)。
  修補後的 plug 在 space 的 `_plug/`,隨版控進 GitLab master。

## 頁面歷史檢視端點

`GET /history/<SHARE_SECRET>/<頁名>`:該頁 git 時間線(最近 20 筆修訂,
來源標籤+彩色 diff,可摺疊),唯讀;Archive/ 與 Library/ 一律 404。
SilverBullet 端入口:指令「History: View Current Page」(定義在
`Config/SharePage`,輪替 SHARE_SECRET 時要同步改該頁的 HISTORY_BASE)。
share/status/history 三端點共用主題模板(右上 ◐ 切換深淺,深=blackbeauty、
淺=同語彙日間版,記憶在訪客 localStorage)。

## 頁面版本控制(git history)

space 資料夾唯讀掛進容器(`/space`),git 倉庫在 space 外(`./history/git`,
SilverBullet 看不到)。commit 時機:

- 每次橋寫入成功後自動 commit,訊息含工具+頁名+來源端點
  (main / ally / edit / restore)——哪個端點改的,`git log` 直接可查。
- 背景 sweep 每 10 分鐘收錄瀏覽器手改(`GIT_SWEEP_SECONDS` 可調)。
- 主 MCP 工具:`page_history(path)` / `page_diff(path, rev)` /
  `restore_page(path, rev)`(還原走 HTTP API 寫回並再 commit,可反悔)。
- 手查:`docker exec -w /space -e GIT_DIR=/history/git -e GIT_WORK_TREE=/space
  silverbullet-mcp git log --oneline -20`。
- 坑:`GIT_DIR` 與 `GIT_WORK_TREE` 必須**同時**設定,只設前者會
  fatal「must be run in a work tree」。
- 每日全量備份另有工具:`python ~/.claude/skills/silverbullet/sb_backup.py`
  (拉到本機 `~/silverbullet_backups/silverbullet-<日期>.tar.gz`,
  內含全空間頁面+`history-git.tar.gz`(space 的 git 倉庫,SSH 從 NAS 抓,
  需在家用網路跑);還原 git=解壓回 NAS 的 `silverbullet-mcp/history/`)。

## 盟友協作寫入(ally)

`share: edit` 的頁面(唯讀含在內)開放給盟友端點讀寫,密鑰 `ALLY_SECRET`
(`.env`,與其它密鑰獨立輪替;留空=功能關閉):

- **MCP**:`/ally/<ALLY_SECRET>/mcp` — 工具 read_page / append_page / write_page,
  範圍僅限 `share: edit` 頁,list 不存在、其它頁 404。盟友的 Claude 加 connector 即用。
- **raw HTTP**:`GET/PUT /edit/<ALLY_SECRET>/<頁名>` — 排程腳本用,規則同上。
- 防呆:write 的內容必須保留 `share: edit` frontmatter(防止協作者把自己鎖在門外);
  寫入內容一律 LF 正規化;寫後驗證。
- **注意**:協作頁本身寫著 ally 連線方式(含密鑰)——這種頁的閱讀連結
  等同交出寫入權,只能給盟友本人。輪替 ALLY_SECRET 時同步更新頁面上的 URL。

## 部署(替代:router 直通模式,需 443)

容器自己講 TLS,router 把對外 port 直接轉到容器 8008,不經反向代理。
沒設憑證的話容器只講明文 HTTP——**不可在那個狀態下對外開 port**(密鑰會裸奔,
claude.ai 也只收 HTTPS)。

1. 整個資料夾丟到 NAS(如 `/volume1/docker/silverbullet-mcp/`)。
2. **匯出憑證**:DSM 控制台 → 安全性 → 憑證 → 自有網域那張 → 匯出,
   得到 `cert.pem`、`chain.pem`、`privkey.pem`。在本資料夾建 `certs/`:
   ```
   mkdir certs
   cat cert.pem chain.pem > certs/fullchain.pem   # 串上中繼鏈,缺鏈會驗證失敗
   cp privkey.pem certs/
   ```
3. `cp .env.example .env`,填:
   - `SB_PASSWORD`
   - `MCP_PATH_SECRET`:先在任何機器跑
     `python -c "import secrets;print(secrets.token_urlsafe(32))"`
   - `SSL_CERTFILE=/certs/fullchain.pem`、`SSL_KEYFILE=/certs/privkey.pem`
4. 起容器:`docker compose up -d --build`
   (或 Container Manager → 專案 → 從 docker-compose 建立)。
5. router 設 port forward:對外 port(如 3002)→ NAS IP:8008。
6. 自測:`curl https://<自有網域>:3002/<密鑰>/mcp` 有 MCP 回應(非 404)即通;
   `http://` 打同一 port 必須失敗(TLS 握手擋掉)。

**憑證更新**:憑證換發後重做步驟 2 並重啟容器(到期日看 DSM 憑證頁)。

(替代模式:若改用 DSM 反向代理 HTTPS→localhost:8008,`.env` 兩行 SSL 留空、
router 轉到反向代理的 port 即可,其餘相同。)

## 接上 Claude(手機)

1. Claude app → Settings → **Connectors** → **Add custom connector**。
2. URL 填:`https://<node>.<tailnet>.ts.net/<MCP_PATH_SECRET>/mcp`
   (Funnel 模式;router 直通模式則為 `https://<自有網域>/<MCP_PATH_SECRET>/mcp`,必須 443)
3. 加好後在任何對話開啟該 connector,直接說
   「讀某一頁」「把某頁的某段改成…」即可。

工具介面(Claude 看到的):

| 工具 | 行為 |
|---|---|
| `readme()` | 回手冊頁全文與版本;呼叫端用法的唯一出處,見「呼叫端用法的出處」 |
| `list_pages(prefix)` | 列頁面(size + name,略過 Library/) |
| `read_page(path)` | 讀整頁(path 不用加 .md) |
| `write_page(path, content)` | 整頁覆寫+寫後驗證(同 sb.py 的 verified 行為) |
| `delete_page(path)` | 刪頁(工具說明已註明需先向使用者確認) |
| `kb_index()` | 一次回手冊版本、`kb/index` 根 index、現有分類清單、近期新增的頁,session 開頭呼叫 |
| `kb_read(name)` | 讀知識頁分隔線以上(總結段) |
| `kb_read_original(name)` | 讀知識頁分隔線以下(原文或來源 link) |
| `kb_write(name, kind, summaries, source, original, reasoning, index, merge_target, readme)` | 寫一頁知識並登記進待審頁,見下節 |
| `kb_deprecate(name, reason, readme)` | 提議作廢一頁既有知識,登記進待審頁 |
| `kb_append_index_tags(index, tags)` | 既有分類補路由 #tag(小類行與祖先行),讀端 AI 徵得使用者許可後用 |

不在工具面、只走 `POST /<MCP_PATH_SECRET>/internal/<操作>`(body 是參數的 JSON 物件,回純文字,拒收回 400)的操作:

| 操作 | 行為 |
|---|---|
| `kb_move_pending_to_index` `{"name", "final_index_by_proposed"}` | 週審:把待審行搬進分類 index,缺的 index 連祖先一起建,行上的 #tag append 到 index 行,並執行該頁登記的取代/作廢/合併 |
| `kb_create_index` `{"index"}` | 建分類 index 與缺的祖先,單獨開分類用 |
| `kb_register_backlink` `{"name", "from_page"}` | kb-audio 寫首頁連結後登記 `被連` |

## 知識庫(kb/)

(本節給維護橋的人:工具的內部格式與實作。呼叫端怎麼用只寫在 space 頁 `AI 守則/SilverBullet 維護手冊`,
這裡不重複。)

版面:`kb/<name>` 知識頁與 `_source` 原文頁(扁平、路徑不變;分類路徑如 `game/screeps/capture`
只用在 index 參數與 index 頁,不是頁名);
`kb/index/<大類>/<小類>/<子類>…` 分類 index(以 `/` 分層,**層數不限**,每一層一頁,例
`game`、`game/screeps`、`game/screeps/capture`;只在使用者點頭後由工具建:週審搬行時
目標缺就建,或 `kb_create_index` 單獨建;缺的祖先一路建到 `kb/index` 根並在父頁
補 link);`kb/index/week/<ISO 週>` 當週待審頁;`kb/log` 時間序。

### 呼叫端用法的出處(readme 接口)

呼叫端 AI 會看到的文字有三層:connector `instructions`、各工具的說明、手冊頁。前兩層由 client 在連線時載入,
之後橋怎麼改,已開著的 session 都看不到;所以用法只放在手冊頁,前兩層不放會變動的內容。

- `instructions` 只有一句:第一步呼叫 `readme()`,照它回的做。
- 各工具的說明只寫那個工具是什麼,參數格式與流程一律指向 `readme()`。改流程、改參數裡面的形狀都只改手冊頁,
  不必動 `server.py`。
- `readme()` 即時讀 `AI 守則/SilverBullet 維護手冊`(`README_PAGE`),第一行 `readme 版本:<8 碼>`,
  版本是全文 sha256 的前 8 碼,頁一改就變(瀏覽器手改也算)。
- `kb_write`、`kb_deprecate` 的 `readme` 參數必須等於現行版本(`_raise_when_readme_version_is_not_current`,
  在其它檢查之前);不符就拒收並要求重新呼叫 `readme()`,拒收訊息不帶現行版本。開著的 session 就是在這裡被帶去
  重讀手冊。`kb_index` 結果第一行也帶版本,只讀不寫的 session 再呼叫時看得到。
- 做不到的:工具清單與參數名本身仍是連線時載入的。新增工具或新增頂層參數後,在那之前開的 session 要重連才看得到;
  拒收訊息有寫「工具清單裡沒有 readme 就請使用者重連」。
- 非 AI 的呼叫者(kb-audio)寫入前先呼叫 `readme` 取第一行的版本再帶上。

大原文經 kb-audio `api/source` 進 kb 的維護面(主機、secret)見 `infra/audio/README.md`。

### 型態(kind)

型態是 `kind` 的值,不是結構;四種頁長相相同,差別只在幾行:

| kind | 用途 | 多出來的東西 | 總結那一句怎麼寫 |
|---|---|---|---|
| `knowledge` | 事實、做法 | 無 | 一句講清這頁能回答什麼 |
| `decision` | 使用者定案的設計或選擇,存**為何這樣定**,給之後只知道結論的 session 看,免得它拿結論當起點重新推翻 | 必填 `reasoning`(多行):為何這樣定、考慮過哪些替代、為何否決;寫成 `## 理由` 段放分隔符以上,`kb_read` 會一起回 | **必須是定案結果本身**,寫成可對照的一句「<對象> 採 <結果>」,例「kb 分類採扁平頁 + index 登記,不做目錄搬移」;這樣想改那個結果的人在 index 掃到這行就能點進去看理由 |
| `record` | 發生過的事 | 無 | 一句講發生了什麼(name 開頭的日期就是時間序) |
| `source` | 原文頁 | `original` + `index`(知識的分類或最近共同母層),不帶 summaries;待審行 `#source → <index> — 原文:<檔名或首行>` | 無 |

型態記在三處:知識/決策/記錄頁 `# name` 下一行 `類型:<kind>`(source 頁沒有這行,`# name` 下一行是
選填的 `檔案:…`);待審行與 index 行 link 後第一個
tag `#<kind>`;`kb/log` 的 `write(<kind>)`。

### 規則與格式(接口保證的部分)

- 路由靠 tag,不靠自然語句:index 頁每一行行尾掛原生 `#tag`,**分層**:一個分類在父頁的那一行,
  帶「直屬於它的頁面的主題 tag」加上「它的子分類名」——兩種可以同時存在,因為一個分類可以同時有
  子分類與直屬頁(根的 `game` 行是 `#screeps`;`game` 頁的 `screeps` 行是 `#capture #loot …`;
  `game/screeps` 頁的 `capture` 行是 `#軍醫 #特遣隊 …`)。AI 從根一層一層照 tag 往下走;
  一個分類頁可能同時列子分類行與頁行。頁行只帶型態 tag。
  tag 只增不刪,所以可以一直 append 不重寫。解析不混:link 指向 `kb/index/*` 的是分類行,
  指向 `kb/*` 的是頁行。(祖先行若累加所有葉 tag,根的 game 行會長到上百個 tag,所以只帶子分類名。)
- `kb_write` 只做入參格式檢查(檔名 `^\d{8}_[a-z0-9_]+$`、kind 四選一、分類為 `大類` 或
  以 `/` 分層的路徑(層數不限)、無空白、首段不得為 `week`、總結單行、tags 為字串陣列且每個
  `^[^\s#]+$` 不重複、decision 必帶 reasoning 其它不得帶、來源欄位齊全、被參照的來源頁存在、`kb/<name>` 尚不存在(已存在拒收,不覆寫)——
  都是對入參本身的檢查,不看內容語意),任一不過整筆拒收、什麼都不寫;tag 與分類路徑一律小寫,四個接口(kb_write、搬行、
  kb_create_index、kb_append_index_tags)都在橋內先 lower 再用,caller 不必處理;
  通過後寫頁 → 當週待審頁每個提議分類加一行 → `kb/log` 加一行,**不回讀、不 lint**。
  換行一律在橋內正規化成 LF。
- 知識頁:`# name` → `類型:<kind>` → 每個提議分類一段 `## 總結|<分類>` 單行 →
  (decision)`## 理由` 多行 → 分隔符 `-----以下為原文,非必要不讀-----` →
  原文(type=text)、`來源:[[頁]]`(type=page,SilverBullet 既有頁只 link)或
  `原文:[[kb/<name>_source]]`(type=source)。
- 每次寫入也確保根 `kb/index` 存在並有一行 `- [[kb/index/week/<週>]] 待審`(沒有才補),
  所以搬行前知識頁就已經是 首頁 → kb/index → 待審頁 → 知識頁 可點到。
- 待審頁行格式:`- [[kb/<name>]] #<kind> → <分類> #tag1 #tag2 — <總結>`(tag 可無)。
  訂案 = 使用者看待審頁,分類或 tag 不對就改那一行,改完才搬。
- 週審:`kb_move_pending_to_index(name, final_index_by_proposed={"<提議>": "<最終>"})`
  把該頁所有待審行搬進 `kb/index/<最終>`(行變成 `- [[kb/<name>]] #<kind> — <總結>`),
  目標 ≠ 提議時連帶把知識頁 `## 總結|<提議>` 改成最終分類;目標 index 缺就建;
  行上的 tag append 到該分類在父頁的行、祖先行補子分類名(已有的跳過);待審頁刪行;`kb/log` 加
  `move` 行。拒收:name 格式、知識頁或待審頁不存在、待審頁沒有該頁的行、
  final 的鍵不在提議裡、final 值格式錯、最終分類重複。
- `kb_write(kind="source", file="kb-media/<檔>")`:原文是放在 NAS `/volume1/kb-media/`
  的媒體檔(音檔逐字稿等)時帶 `file`,source 頁 `# name` 下一行寫 `檔案:kb-media/<檔>`;
  只檢格式(`kb-media/` 開頭、無空白),橋看不到那個資料夾、不檢查存在。只有 source 能帶。
  kb-audio 容器(`infra/audio/`)用這個匯入。
- `kb_create_index(index)`:已存在拒收;`kb/log` 加 `create` 行。根 `kb/index`
  不存在時工具會以 `# kb index` 標頭生出來;根頁全由工具生,沒有手寫段。
- `kb_append_index_tags(index, tags)`:index 必須已存在;tag 同格式檢查、不得為空;
  append 到該分類在父頁的行、祖先行補子分類名,已有的跳過、不刪;全部已有回 `OK … (no new tags)`,否則
  `kb/log` 加 `tags` 行。
- 待審頁的週次由 name 的 8 碼日期算 ISO 週;`kb_index` 列出**所有**還有待審行的週頁,不限本週,
  所以沒有「寫進下週頁而 kb_index 看不到」的情況。
- **呼叫端不感知審核**:對呼叫端來說寫了就是寫了,一頁是已入庫還是待審、一個修改是已執行還是只登記,都與它無關,
  接口行為不得因此不同。落實在五處:
  - 手冊、工具說明、工具回覆、拒收訊息都不出現待審頁、週審、提議。
  - 週審操作不在 MCP 工具面(見上表 internal 端點),呼叫端的工具清單裡沒有它們。
  - `kb_index`:根 index 的待審連結行不列;各週待審頁的頁行併成「近期新增的頁」一區,⇒ 行不列,已被登記取代/作廢/合併的頁
    不列。
  - 已登記但還沒執行的取代/作廢/合併,對呼叫端一律當作已完成(`_kb_changes_registered_against_pages` 掃各週待審頁的
    ⇒ 行):`kb_read`/`kb_read_original` 對被指到的頁拒讀,用和已廢止頁同一句話(`_kb_gone_note`)並指向新頁;
    `merge_target` 與 `kb_deprecate` 再指到它拒收;登記了 merge 的新頁,`kb_read_original` 回的就是合併後的樣子
    (`## 原文｜<新>` + `## 原文｜<舊>`)。
  - 改任何一頁(含還在待審的)都是同一條路:新 name + `merge_target`,或 `kb_deprecate`;同名寫入一律拒收。
- **修改現有知識**:任何改動既有頁的動作都只在週審執行;寫入端只能提議,提議一律登記在待審頁。兩種提議:
  - 隨新頁提:`kb_write(..., merge_target={"replace"|"deprecate"|"merge": "<id>", "reason": …})`。入參檢查:三鍵互斥
    (可加 reason);replace / deprecate 必帶 reason、merge 不帶;target 須存在於 `kb/<id>`(已廢止的不算)且不是
    source 頁;不可指向自己;source 頁不能帶 merge_target;replace / merge 須同型態、同分類(target 的分類取其
    `登記於` 記錄,還在待審的取提議分類)。待審頁在該頁的行後多一行
    `  ⇒ [[kb/<new>]] <replace|deprecate|merge> [[kb/<id>]] — <reason>`。
  - 沒有新頁的純作廢:`kb_deprecate(name, reason)` 只登記,不動任何頁:在當週(呼叫當天的 ISO 週)待審頁加一行
    `  ⇒ deprecate [[kb/<name>]] — <reason>`(沒有新頁當主詞,和上一種形狀不同),根 index 連到該待審頁,
    log 記 `deprecate-proposed`;該頁已被登記任何取代/作廢/合併時拒收。
  執行一律在 `kb_move_pending_to_index(name)`:
  - name 是新頁:搬它的待審行進分類,再執行它帶的 ⇒ 標記。target 已不存在(先被別的操作廢止或合併)→ 搬行前
    整筆拒收,請使用者改待審頁上的 ⇒ 行。
  - name 是被提議作廢的舊頁:在所有待審頁找它的 `⇒ deprecate` 標記,retire 後刪標記、log 記 `deprecate`。
    舊頁自己還在待審又被提作廢:先搬它的行再 retire,一次呼叫做完。沒有待審行也沒有標記才拒收。
  - replace / deprecate → `_kb_retire`:target 搬到 `kb/deprecated/<id>`,頁首 `狀態：replaced|deprecated`、
    `原因：`、(replace)`取代：[[kb/<new>]]`、`原分類：<path>`(每個一行,來自 `登記於`);從所有登記的 index 頁
    拔行(待審中的連 ⇒ 行一起拔);依 `被連`/`連到` 記錄反向改每個引用處的連結:
    `[[kb/<id>|別名]]` → `[[kb/deprecated/<id>|別名]] #replaced [[kb/<new>]]`(deprecated 無尾巴);列入
    `kb/index/deprecated`(`- [[kb/deprecated/<id>]] #<狀態> — 原分類：…；原因：…；取代：…`),根 index 連
    `- [[kb/index/deprecated]] 已廢止`。它 append 過的路由 tag 不動(只增不刪)。
  - merge → `_kb_merge`:新頁上半部不動,下半部改成 `## 原文｜<new>` + 新頁原文、`## 原文｜<id>` + 舊頁原文;
    新頁繼承舊頁的 `連到`/`被連` 記錄;舊頁從 index 拔行後刪除(沒有 deprecated 頁);引用舊頁的地方改
    `[[kb/<new>]] #merged`。
  待審頁「還有東西要審」的判定 = 有 `- [[` 行或 `  ⇒` 行(`_kb_has_pending`);kb_index 列哪些週頁用它,
  搬行、retire、merge 拔完行後也用它:還有東西就寫回,空了就**刪掉該週頁**並從根 index 拔連結
  (`_kb_save_week_page_or_remove_when_empty`),下一筆 kb_write 再重建。
  `POST /<MCP_PATH_SECRET>/internal/kb_register_backlink`(JSON `{"name", "from_page"}`,不是 MCP 工具,呼叫端 AI
  看不到):kb-audio 寫首頁「kb 音檔記錄」那行後,替每個連到的頁登記 `被連：[[index]]`。
  deprecated 的 name 不占用:kb_write 只查 `kb/<name>`。
- **連結記錄(雙向鏈)**:每個 kb 頁尾 `-----連結記錄，工具維護-----` 之後一行一筆:
  `登記於：<分類路徑 | week/<週>>`(列了它的 index 頁)、`連到：[[kb/x]]`(它引用的頁:source 頁、merge_target 的
  target)、`被連：[[頁]]`(引用它的頁,含首頁 `[[index]]`)。kb_write 同時寫兩端;搬行把 `登記於：week/…` 換成分類;
  retire / merge 只靠這些記錄找 index 行與引用處,**不掃描空間**。`kb_read` / `kb_read_original` 不回這段;對
  `kb/deprecated/` 下的頁拒讀並附狀態與原因(replaced 附新 id)。
- 字面常數在 `server.py`:`KB_SEPARATOR`、`KB_SUMMARY_HEADING`、`KB_REASONING_HEADING`、
  `KB_KIND_LINE`、`KB_KINDS`,讀寫共用。

## 維運

- **輪替密鑰**:改 `.env` 的 `MCP_PATH_SECRET` → `docker compose up -d`
  → Claude connector URL 跟著換。懷疑外洩時做這個就好,成本近零。
- SilverBullet 改帳密:同步改 `.env` 重啟。
- 容器崩潰自動重啟(`restart: unless-stopped`);SilverBullet 的
  session cookie 過期會自動重登(server.py 內建)。
- 日誌:`docker logs silverbullet-mcp`。

## 已知邊界

- 只處理文字頁面;附件(png 等)list 看得到,read/write 不支援
  (手機情境用不到,要加再說)。
- `write_page` 是整頁覆寫——Claude 端的工具說明已要求它先讀後寫。
