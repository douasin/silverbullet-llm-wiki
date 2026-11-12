# Share Page

頂欄的分享按鈕(share-2 圖示)=「**分享並複製連結**」:

- 頁面未分享 → frontmatter 寫入 `share: true` 並複製分享連結。
- 頁面已分享(`share: true` 或 `share: edit`)→ **只複製連結,不動狀態**——
  想拿連結隨時按,不會誤關分享。

**取消分享**(低頻操作,獨立指令):在目標頁按 Ctrl+/ 跑「Share: Stop Sharing」。
協作頁(`share: edit`)會被擋下提醒手動處理,避免誤斷盟友存取。
只有標了 share 的頁面能被分享連結讀到,其它一律 404。

**頁面歷史**:頂欄時鐘圖示(或指令「History: Toggle Panel」)——右側滑出
目前頁的 git 時間線面板(誰改的、彩色 diff),換頁自動跟隨,再按一次收起。
不離開頁面;想開成完整分頁用「History: View Current Page」。
還原叫 AI 用 restore_page。

**樹狀導航**:頂欄 sidebar 圖示(或指令「Tree View: Toggle」)開關左側頁面樹,
直接點到任何分支頁,支援拖曳搬頁。

```space-lua
local SHARE_BASE = "https://<node>.<tailnet>.ts.net/share/<SHARE_SECRET>/"

local function copyLink(page)
  local enc = js.window.encodeURIComponent(page)
  enc = string.gsub(enc, "%%2F", "/")
  local url = SHARE_BASE .. enc
  local ok = pcall(function()
    editor.copyToClipboard(url)
  end)
  if not ok then
    js.window.navigator.clipboard.writeText(url)
  end
end

command.define {
  name = "Share: Share & Copy Link",
  run = function()
    local page = editor.getCurrentPage()
    local text = editor.getText()
    local fmBody, rest = string.match(text, "^%-%-%-\n(.-)\n%-%-%-\n(.*)$")
    if fmBody and (string.match(fmBody, "share:%s*true")
                   or string.match(fmBody, "share:%s*edit")) then
      copyLink(page)
      editor.flashNotification("已在分享中,連結已複製")
      return
    end
    if fmBody then
      editor.setText("---\nshare: true\n" .. fmBody .. "\n---\n" .. rest)
    else
      editor.setText("---\nshare: true\n---\n" .. text)
    end
    copyLink(page)
    editor.flashNotification("已開啟分享,連結已複製")
  end
}

local HISTORY_BASE = "https://<node>.<tailnet>.ts.net/history/<SHARE_SECRET>/"
local historyPanelOpen = false

local function historyUrl()
  local enc = js.window.encodeURIComponent(editor.getCurrentPage())
  return HISTORY_BASE .. string.gsub(enc, "%%2F", "/")
end

local function showHistoryPanel()
  editor.showPanel("rhs", 1,
    '<style>html,body{margin:0;padding:0;height:100%;overflow:hidden;}' ..
    '</style><iframe src="' .. historyUrl() ..
    '" style="border:0;position:fixed;inset:0;width:100%;height:100%;">' ..
    '</iframe>')
  historyPanelOpen = true
end

command.define {
  name = "History: Toggle Panel",
  run = function()
    if historyPanelOpen then
      editor.hidePanel("rhs")
      historyPanelOpen = false
    else
      showHistoryPanel()
    end
  end
}

-- 面板開著時換頁,歷史跟著切
event.listen {
  name = "editor:pageLoaded",
  run = function()
    if historyPanelOpen then
      showHistoryPanel()
    end
  end
}

command.define {
  name = "History: View Current Page",
  run = function()
    js.window.open(historyUrl(), "_blank")
  end
}

command.define {
  name = "Share: Stop Sharing",
  run = function()
    local text = editor.getText()
    local fmBody, rest = string.match(text, "^%-%-%-\n(.-)\n%-%-%-\n(.*)$")
    if not fmBody or not string.match(fmBody, "share:%s*[a-z]") then
      editor.flashNotification("此頁未在分享中")
      return
    end
    if string.match(fmBody, "share:%s*edit") then
      editor.flashNotification("此頁是協作頁(share: edit),取消請手動編輯 frontmatter", "error")
      return
    end
    local newFm = string.gsub(fmBody .. "\n", "share:%s*true[ \t]*\n", "")
    newFm = string.gsub(newFm, "\n$", "")
    if newFm == "" then
      editor.setText(rest)
    else
      editor.setText("---\n" .. newFm .. "\n---\n" .. rest)
    end
    editor.flashNotification("已取消分享")
  end
}

config.set("actionButtons", {
  {
    icon = "sidebar",
    description = "Toggle page tree",
    command = "Tree View: Toggle",
    priority = 5,
  },
  {
    icon = "clock",
    description = "Toggle page history panel",
    command = "History: Toggle Panel",
    priority = 4.5,
  },
  {
    icon = "moon",
    description = "切換深淺主題",
    command = "Editor: Toggle Dark Mode",
    priority = 4.2,
  },
  {
    icon = "share-2",
    description = "Share & copy link",
    command = "Share: Share & Copy Link",
    priority = 4,
  },
  {
    icon = "home",
    description = "Go to the index page",
    command = "Navigate: Home",
    priority = 3,
    dropdown = false,
  },
  {
    icon = "book",
    description = "Open page",
    command = "Navigate: Page Picker",
    priority = 2,
    dropdown = false,
  },
  {
    icon = "terminal",
    description = "Run command",
    command = "Open Command Palette",
    priority = 1,
  },
  {
    icon = "chevron-left",
    description = "Go back",
    standalone = true,
    priority = 0,
    run = function() editor.goHistory(-1) end,
  },
  {
    icon = "chevron-right",
    description = "Go forward",
    standalone = true,
    priority = -1,
    run = function() editor.goHistory(1) end,
  }
})
```
