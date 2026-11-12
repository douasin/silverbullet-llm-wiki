# SilverBullet 操作鐵則（MCP 工具）

把這一節貼進 Claude Code 的全域規則檔 `~/.claude/CLAUDE.md`，每個 session 都會載入。

1. write_page 是整頁覆寫：必先 read_page 拿全文，只改目標段落，其餘一字不動寫回。絕不憑記憶重寫整頁。
2. `${...}` 開頭的行是 widget，原樣保留；widget 可橫跨多行，區塊內插空行會弄壞。`index` 頁上半部整片 widget 別碰。
3. 保留原始格式：逐字保留換行、空行、縮排；禁止整頁重排版。
4. delete_page 只在明確要求刪除時用。
5. `Archive/` 是封存區，未經使用者在本對話允許不得讀寫；需要時先問「已封存，要打開嗎？」。封存／解封由使用者操作。
6. 路徑用 `/` 分層、不加 .md。`Library/` 是系統頁不動；`Trigger/` 由排程覆寫不手編。
7. write_page／restore_page 寫入後要看到 `OK ... verified`；kb 工具（kb_write 等）照設計不回讀，回以 `OK` 開頭的結果就是成功。沒拿到 OK 就是失敗，回報，不重試迴圈。
8. 直寫 HTTP `/.fs/` 的內容必須是 LF，含 `\r` 一律視為錯誤。
9. 版控：page_history 列修訂、page_diff 看差異、restore_page 還原。改壞東西用 restore，不手工重建。
10. 每一頁都必須有從首頁點得進去的路徑（index→hub→頁），建頁當下就補目錄連結，不留孤兒頁。`kb/` 下的頁例外：由 kb 工具登記進 kb 的 index，不手動加進首頁。
11. share 兩級：`share: true` 對外唯讀，`share: edit` 只有使用者明確指定才能設。更新內容時 frontmatter 原樣保留。
12. 文件型頁面頁首 `# 標題` 後放 `${widgets.commandButton("📋 複製整頁", "Copy Page Content")}`，內文用 `[[頁名|顯示文字]]` 互連。
13. 動別條線的頁面先抓現行版再改，別的 session 或使用者可能剛寫過。
14. **kb 知識庫**：SessionStart hook 會把 `kb_index` 的結果裝進 context；context 裡沒有就自己呼叫一次 `kb_index`。問題主題對得上 `kb_index` 結果裡某一條就先走 kb，讀法與寫法全部照維護手冊「知識庫」節（呼叫 MCP 工具 `readme()` 取得現行版），答案註明來源頁；對不上才用自身知識並標明未命中。
