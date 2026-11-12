# Copy Page Button

定義「Copy Page Content」指令:複製目前頁面的完整原文到剪貼簿,
會先濾掉 `${...}` widget 行與開頭的 frontmatter 區塊(按鈕、share 標記等
頁面機制不進剪貼簿)。
任何頁面可用 Ctrl+/ 呼叫,或在頁面裡放按鈕:
`${widgets.commandButton("📋 複製整頁", "Copy Page Content")}`

```space-lua
command.define {
  name = "Copy Page Content",
  run = function()
    local text = editor.getText()
    -- 濾掉開頭的 frontmatter 區塊(share: true 等頁面機制)
    text = string.gsub(text, "^%-%-%-\n.-\n%-%-%-\n", "", 1)
    -- 濾掉 widget 行(${...} 整行)與其留下的多餘空行
    text = string.gsub(text, "%${[^\n]*}\n?", "")
    text = string.gsub(text, "\n\n\n+", "\n\n")
    local ok = pcall(function()
      editor.copyToClipboard(text)
    end)
    if not ok then
      js.window.navigator.clipboard.writeText(text)
    end
    editor.flashNotification("已複製整頁內容")
  end
}
```
