# Archive Page

封存機制:`Archive/` 底下的頁面=**封存區**——AI 一律看不到、讀不到、寫不到
(MCP 工具與 sb.py 都會拒絕;你明確允許後 AI 才能帶參數存取),
share 分享對封存頁一律 404。目的:省 token+防誤動。

操作(在**目標頁**上按 Ctrl+/ 執行):

- **Archive: Archive Page** — 把目前頁搬進 `Archive/` 並跳轉過去。
  記得自己把首頁/hub 上指向它的連結拿掉(連結會變 missing 色)。
- **Archive: Restore Page** — 在封存頁上執行,搬回原位置(解封)。

```space-lua
command.define {
  name = "Archive: Archive Page",
  run = function()
    local page = editor.getCurrentPage()
    if string.match(page, "^Archive/") then
      editor.flashNotification("此頁已在封存區", "error")
      return
    end
    if page == "index" or string.match(page, "^Config/") then
      editor.flashNotification("index 與 Config/ 不可封存", "error")
      return
    end
    local text = editor.getText()
    space.writePage("Archive/" .. page, text)
    space.deletePage(page)
    editor.navigate({ kind = "page", page = "Archive/" .. page })
    editor.flashNotification("已封存:Archive/" .. page .. ",記得清掉目錄連結")
  end
}

command.define {
  name = "Archive: Restore Page",
  run = function()
    local page = editor.getCurrentPage()
    local original = string.match(page, "^Archive/(.+)$")
    if not original then
      editor.flashNotification("此頁不在封存區", "error")
      return
    end
    local text = editor.getText()
    space.writePage(original, text)
    space.deletePage(page)
    editor.navigate({ kind = "page", page = original })
    editor.flashNotification("已解封:" .. original)
  end
}
```
