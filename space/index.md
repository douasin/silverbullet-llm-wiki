# Recent quick notes
${widgets.commandButton("Create quick note", "Quick Note")}

${some(query[[
  from p = index.subPages("Inbox")
  order by p.lastModified desc
  limit 10 select templates.fullPageItem(p)
]]) or "_No quick notes yet!_"}

# Recent journal entries
${widgets.commandButton("Today's entry", "Journal: Today")}

${some(query[[
  from j = index.pages(config.get("journal.tag"))
  where j.tag == "page"
  order by j.date desc
  limit 14
  select templates.pageItem(j)
]]) or "_No journal entries yet!_"}

# Recent incomplete tasks
${some(query[[
  from t = index.tasks()
  where not t.done
  order by t.pageLastModified
  desc limit 10
  select templates.taskItem(t)
]]) or "_All tasks done!_"}

# Recently modified pages
${query[[
  from p = index.contentPages()
  order by p.lastModified desc
  limit 10
  select templates.fullPageItem(p) 
]]}

## AI 守則
- [[AI 守則/SilverBullet 維護手冊|SilverBullet 維護手冊]] — 給 AI 的操作手冊,`readme()` 回的就是這一頁

## 知識庫
- [[kb/index|kb 根 index]] — AI 知識庫入口(時間序在 [[kb/log]])
- [kb-audio 公用](https://<node>.<tailnet>.ts.net:8443/<AUDIO_PATH_SECRET>/) — 拖音檔 → 逐字稿 → 摘要 → 下載
- [kb-audio 匯入 kb](https://<node>.<tailnet>.ts.net:8443/<KB_PATH_SECRET>/) — 先填背景 → 拖音檔 → 逐字稿 → 摘要 → 匯入

## kb 音檔記錄

## 系統
- [[Config/SystemStatus|系統狀態]] — 一眼看健康度(build/版控/funnel/備份)
- [[Config/FunnelRestart|Funnel 重啟]] — 外網入口(share/手機 connector)打不開時按這裡
- [[Config/ArchivePage|封存機制]] — Archive/ 封存區說明與封存/解封指令
- [[Config/SharePage|分享與歷史]] — 頂欄分享按鈕、頁面歷史面板
- [[Config/Theme|主題配色]]
- [[Config/CopyPageButton|複製整頁按鈕]]
- [[Config/MobileTapShield|手機防誤觸]] — 雙擊或長按才進入編輯
- [[CONFIG|plug 設定]] — 側欄頁面樹(treeview)的設定
