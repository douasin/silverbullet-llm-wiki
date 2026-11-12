# Mobile Tap Shield

手機防誤觸:平常點頁面不會跳鍵盤(滑動、點連結正常),
**雙擊或長按 0.5 秒**才進入編輯。桌機不受影響。
臨時停用:Ctrl+/ 跑「Mobile: Toggle Accidental Tap Shield」。
來源:SilverBullet 社群論壇的 v7 Streamlined Shield,
[Fixing(Workaround) the SilverBullet/CodeMirror Mobile Keyboard drama - Disable Accidental Taps](https://community.silverbullet.md/t/fixing-workaround-the-silverbullet-codemirror-mobile-keyboard-drama-disable-accidental-taps/3709)。
下面的 `space-lua` 是該討論串作者的程式碼,不是本 repo 的原創內容。

```space-lua
function setupStreamlinedShield()
  if js.window.sbStreamlinedActive then return end

  local scriptContent = [[
    (function() {
      let lastTap = 0;
      let holdTimer = null;
      const DOUBLE_TAP_THRESHOLD = 300;
      const LONG_PRESS_THRESHOLD = 500;
      let isUnlocked = false;

      const unlock = () => { isUnlocked = true; };
      const lock = () => { isUnlocked = false; };

      const handleCapture = (e) => {
        if (window.sbShieldDisabledManually) return;

        const isEditor = e.target.closest('#sb-editor');
        const isUI = e.target.closest('.sb-actions, .sb-top-bar, .sb-sidebar, button, .menu-item');

        if (!isEditor || isUI) return;
        if (isUnlocked && document.activeElement.closest('#sb-editor')) return;

        const now = Date.now();

        if (e.type === 'touchstart') {
          if (now - lastTap < DOUBLE_TAP_THRESHOLD) {
            unlock();
          }
          lastTap = now;

          clearTimeout(holdTimer);
          holdTimer = setTimeout(() => {
            unlock();
            const el = document.querySelector('#sb-main .cm-content');
            if (el) {
              el.setAttribute('inputmode', 'text');
              el.focus();
            }
          }, LONG_PRESS_THRESHOLD);
        }

        if (e.type === 'touchend' || e.type === 'touchmove') {
          clearTimeout(holdTimer);
        }

        if (!isUnlocked) {
          const killZone = ['mousedown', 'mouseup', 'click', 'touchend'];
          if (killZone.includes(e.type)) {
            e.preventDefault();
            e.stopPropagation();
            e.stopImmediatePropagation();
          }
        }
      };

      document.addEventListener('focusout', (e) => {
        if (e.target.closest('#sb-editor')) lock();
      }, true);

      ['touchstart', 'touchend', 'touchmove', 'mousedown', 'mouseup', 'click'].forEach(type => {
        window.addEventListener(type, handleCapture, { capture: true, passive: false });
      });

      window.sbStreamlinedActive = true;
    })();
  ]]

  local scriptEl = js.window.document.createElement("script")
  scriptEl.innerHTML = scriptContent
  js.window.document.body.appendChild(scriptEl)
end

command.define {
  name = "Mobile: Toggle Accidental Tap Shield",
  run = function()
    if not js.window.sbShieldDisabledManually then
      js.window.sbShieldDisabledManually = true
      editor.flashNotification("Shield DISARMED. Normal tapping restored.", "info")
    else
      js.window.sbShieldDisabledManually = false
      setupStreamlinedShield()
      editor.flashNotification("Shield ENGAGED. Double-tap/Hold to edit.", "info")
    end
  end
}

event.listen { name = "editor:pageLoaded", run = function() if editor.isMobile() then setupStreamlinedShield() end end }
```
