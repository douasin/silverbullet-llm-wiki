# System Status

一眼看全部健康度的**即時狀態頁**(由 MCP 橋產生,含 build 版本、
容器運行時間、空間統計、最近 commit、funnel 最後重啟、最後備份):

https://<node>.<tailnet>.ts.net/status/<SHARE_SECRET>

- 外網入口掛掉時上面的連結也會斷——這正是訊號:去按 [[Config/FunnelRestart|Funnel 重啟]],
  結果看 [[Trigger/funnel-restart-result]]。
- 備份狀態的來源:每次跑 `sb_backup.py` 會回寫 [[Trigger/backup-status]]。
- 底層資料頁(`Trigger/…`)由系統覆寫,不要手編。
