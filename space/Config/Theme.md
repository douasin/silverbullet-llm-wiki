# Theme: BlackBeauty(深)+ 日間版(淺)

深色=vim blackbeauty 移植(純黑底+高彩度語法色);淺色=同一套色彩語彙的
日間版(暖紙底,各色降亮度保對比)——Editor: Toggle Dark Mode 兩態互切,
色彩語意不變(標題紫紅、wiki 連結青系、外部連結藍系、metadata 綠)。
每行都註了對應角色,想微調直接改色碼後 System: Reload。

```space-style
/* BlackBeauty (vim) → SilverBullet */
html[data-theme="dark"] {
  /* Normal: white on black */
  --root-background-color: #000000;
  --root-color: #f0f0f0;

  /* Statement #ffff60:UI 強調色與游標 */
  --ui-accent-color: #ffff60;
  --ui-accent-text-color: #000000;
  --editor-caret-color: #ffff60;

  /* 指令面板選中列:黃底黑字(vim PmenuSel 式反白) */
  --modal-selected-option-background-color: #ffff60;
  --modal-selected-option-color: #000000;
  --modal-selected-option-description-color: #303000;

  /* Title (Magenta):標題 */
  --editor-heading-color: #ff00ff;

  /* Comment #80a0ff:外部連結與註解 */
  --link-color: #80a0ff;
  --editor-link-color: #80a0ff;
  --editor-code-comment-color: #80a0ff;
  --link-missing-color: #ff8080;

  /* Identifier #40ffff:wiki 內部連結與程式變數 */
  --editor-wiki-link-page-color: #40ffff;
  --editor-code-variable-color: #40ffff;

  /* Constant #ffa0a0:字串 */
  --editor-code-string-color: #ffa0a0;

  /* Type #60ff60:metadata */
  --meta-color: #60ff60;
  --meta-subtle-color: #3a8a3a;

  /* Search (magenta):選取/搜尋高亮 */
  --highlight-color: rgba(255, 0, 255, 0.25);

  /* PreProc #ff80ff:hashtag */
  --editor-hashtag-color: #ff80ff;
  --editor-hashtag-background-color: #1a0a1a;
  --editor-hashtag-border-color: #ff80ff;

  /* 介面層:黑底系 */
  --subtle-color: #9a9a9a;
  --subtle-background-color: #101010;
  --top-background-color: #000000;
  --top-color: #f0f0f0;
  --top-border-color: #2a2a2a;
  /* 側欄容器(樹的內容色已烤死在 plug 裡,見 README) */
  --panel-background-color: #d1cdc5;
  --panel-border-color: #b5b0a4;
  --modal-background-color: #0a0a0a;
  --modal-color: #f0f0f0;
  --button-background-color: #151515;

  /* 區塊元素 */
  --editor-frontmatter-color: #7a7a7a;
  --editor-frontmatter-background-color: #0a0a0a;
  --editor-blockquote-color: #b0b0ff;
  --editor-blockquote-background-color: #0a0a14;
  --editor-table-head-background-color: #101010;
  --editor-table-head-color: #f0f0f0; /* 表頭文字:原廠預設 #eee 只配原廠深底,自訂底色後必須跟著設 */
  --editor-table-even-background-color: #0a0a0a;
}

/* BlackBeauty 日間版:同色彩語彙,暖紙底 */
html[data-theme="light"] {
  /* Normal:近黑字on暖紙 */
  --root-background-color: #fbfaf6;
  --root-color: #1c1c1c;

  /* Statement(亮黃→暗金):UI 強調色與游標 */
  --ui-accent-color: #9a7d00;
  --ui-accent-text-color: #ffffff;
  --editor-caret-color: #9a7d00;

  /* 指令面板選中列:淡黃底黑字 */
  --modal-selected-option-background-color: #ffe066;
  --modal-selected-option-color: #1c1c1c;
  --modal-selected-option-description-color: #5a4d00;

  /* Title(紫紅→深紫紅):標題 */
  --editor-heading-color: #a020a0;

  /* Comment(藍紫→靛藍):外部連結與註解 */
  --link-color: #2f5fd0;
  --editor-link-color: #2f5fd0;
  --editor-code-comment-color: #4a68c8;
  --link-missing-color: #c04848;

  /* Identifier(青→深青):wiki 內部連結與程式變數 */
  --editor-wiki-link-page-color: #007a7a;
  --editor-code-variable-color: #007a7a;

  /* Constant(鮭紅→磚紅):字串 */
  --editor-code-string-color: #b03a3a;

  /* Type(綠→深綠):metadata */
  --meta-color: #2e7d32;
  --meta-subtle-color: #7aa87d;

  /* Search:選取/搜尋高亮 */
  --highlight-color: rgba(160, 32, 160, 0.16);

  /* PreProc(粉紫→深粉紫):hashtag */
  --editor-hashtag-color: #b83db8;
  --editor-hashtag-background-color: #f7e8f7;
  --editor-hashtag-border-color: #b83db8;

  /* 介面層:紙感 */
  --subtle-color: #6b6b6b;
  --subtle-background-color: #f0efe9;
  --top-background-color: #fbfaf6;
  --top-color: #1c1c1c;
  --top-border-color: #d8d6cf;
  /* 側欄容器(樹的內容色已烤死在 plug 裡,見 README) */
  --panel-background-color: #d1cdc5;
  --panel-border-color: #b5b0a4;
  --modal-background-color: #fffefb;
  --modal-color: #1c1c1c;
  --button-background-color: #efeee8;

  /* 區塊元素 */
  --editor-frontmatter-color: #8a8a8a;
  --editor-frontmatter-background-color: #f4f3ee;
  --editor-blockquote-color: #4a4a8a;
  --editor-blockquote-background-color: #f2f2f8;
  --editor-table-head-background-color: #f0efe9;
  --editor-table-head-color: #1c1c1c; /* 表頭文字:原廠預設 #eee 在淺紙底上隱形,這就是表格看不到 header 的原因 */
  --editor-table-even-background-color: #f6f5f0;
}

```

