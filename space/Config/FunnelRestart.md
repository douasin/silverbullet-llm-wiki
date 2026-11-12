# Funnel Restart

外網入口(Tailscale Funnel)故障時的手動重啟鈕。按下後約 **1 分鐘內**
由 NAS 端排程執行重啟,結果寫在 [[Trigger/funnel-restart-result]]。

${widgets.commandButton("🔄 重啟 Funnel", "Funnel: Restart")}

- 典型症狀:share 分享頁/手機 connector 打不開,但 SilverBullet 本身正常。
- 另有**自動看門狗**:每分鐘檢查公共 DNS,消失會自動重啟(10 分鐘冷卻),
  多數情況不需要按這顆鈕。
- 機制:按鈕寫入 `Trigger/funnel-restart` 觸發頁 → NAS 排程(funnel_watchdog.sh)
  偵測到即執行,觸發頁內容不被解讀。

```space-lua
command.define {
  name = "Funnel: Restart",
  run = function()
    space.writePage("Trigger/funnel-restart", "manual restart request")
    editor.flashNotification("已送出重啟請求,約 1 分鐘內執行;結果見 Trigger/funnel-restart-result")
  end
}
```
