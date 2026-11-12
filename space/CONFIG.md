# CONFIG

SilverBullet 全域設定頁(plug 設定)。v2 的 plug 安裝=把 `.plug.js` 檔
放進空間的 `_plug/` 資料夾 → `Plugs: Reload` → `System: Reload`
(`Plugs: Update` 是 v1 的指令,v2 沒有)。設定改完跑 `System: Reload`。

```space-lua
config.set {
  plugs = {
    "github:joekrill/silverbullet-treeview/treeview.plug.js"
  },
  treeview = {
    position = "lhs",
    size = 1,
    dragAndDrop = {
      enabled = true,
      confirmOnRename = true,
    },
    exclusions = {
      { type = "regex", rule = "^(Library|Trigger)/", negate = false },
    },
  },
}
```
