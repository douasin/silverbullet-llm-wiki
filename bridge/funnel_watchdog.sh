#!/bin/sh
# funnel_watchdog.sh — DSM 任務排程器每分鐘以 root 執行。
# A) SilverBullet 頁面按鈕寫入 Trigger/funnel-restart.md => 執行 funnel 重啟
# B) 看門狗:本地 funnel 設定存在但 ts.net 公共 DNS 消失(雙解析器都 NXDOMAIN)
#    => 自動重啟,10 分鐘冷卻防抖動
# C) 每日 04:00 後首個分鐘:space+git 備份到 NAS(保留 14 份)+ push GitLab
#    + 回寫 Trigger/backup-status.md
# 觸發檔內容一律不解讀,只認存在與否;指令寫死,無注入面。

TS=/var/packages/Tailscale/target/bin/tailscale
DOCKER=/usr/local/bin/docker
SPACE=/volume1/docker/silverbullet/space
TRIG="$SPACE/Trigger/funnel-restart.md"
RESULT="$SPACE/Trigger/funnel-restart-result.md"
COOLDOWN=/tmp/funnel_watchdog_last
HOST="<node>.<tailnet>.ts.net"
FUNNEL_TARGET="proxy http://127.0.0.1:8008"
# 第二個 Funnel:kb-audio 容器(音檔轉逐字稿),https://<node>:8443 → 8009
AUDIO_FUNNEL_PORT=8443
AUDIO_TARGET="proxy http://127.0.0.1:8009"
BACKUP_DIR=/volume1/docker/silverbullet-backups
BACKUP_KEEP=14
BACKUP_STATUS="$SPACE/Trigger/backup-status.md"
GITLAB_TOKEN_FILE=/volume1/docker/silverbullet-mcp/.gitlab-token
GITLAB_REPO="gitlab.com/<account>/<repo>.git"

bounce() {
    reason="$1"
    "$TS" serve reset >/dev/null 2>&1
    "$TS" funnel --bg 8008 >/dev/null 2>&1
    "$TS" funnel --bg --https="$AUDIO_FUNNEL_PORT" 8009 >/dev/null 2>&1
    sleep 2
    status=$("$TS" funnel status 2>/dev/null)
    if echo "$status" | grep -q "$FUNNEL_TARGET"; then
        ok="成功"
        echo "$status" | grep -q "$AUDIO_TARGET" || ok="成功(但 $AUDIO_FUNNEL_PORT→8009 的 kb-audio 代理沒起來)"
    else
        ok="失敗(funnel status 沒有 8008 代理,需人工檢查)"
    fi
    mkdir -p "$SPACE/Trigger"
    {
        echo "# Funnel Restart Result"
        echo ""
        echo "- 時間:$(date '+%Y-%m-%d %H:%M:%S %z')"
        echo "- 原因:$reason"
        echo "- 結果:$ok"
        echo ""
        echo "(此頁由 NAS 的 funnel_watchdog.sh 產生,每次重啟覆寫)"
    } > "$RESULT"
    chown "$(stat -c %u:%g "$SPACE")" "$RESULT" "$SPACE/Trigger" 2>/dev/null
    date +%s > "$COOLDOWN"
}

daily_backup() {
    archive="$BACKUP_DIR/space-$(date +%F).tar.gz"
    [ -f "$archive" ] && return
    [ "$(date +%H)" -ge 4 ] || return
    mkdir -p "$BACKUP_DIR"
    if tar -czf "$archive.part" -C /volume1/docker \
        silverbullet/space silverbullet-mcp/history/git 2>/dev/null; then
        mv "$archive.part" "$archive"
        backup_note="成功($(stat -c %s "$archive") bytes)"
    else
        rm -f "$archive.part"
        backup_note="失敗(tar 錯誤)"
    fi
    ls -1t "$BACKUP_DIR"/space-*.tar.gz 2>/dev/null \
        | tail -n +$((BACKUP_KEEP + 1)) | xargs -r rm -f
    push_note="未設定 token,跳過"
    if [ -s "$GITLAB_TOKEN_FILE" ]; then
        tk=$(cat "$GITLAB_TOKEN_FILE")
        if $DOCKER exec -w /space -e GIT_DIR=/history/git \
            -e GIT_WORK_TREE=/space silverbullet-mcp \
            git push "https://oauth2:${tk}@${GITLAB_REPO}" master \
            >/dev/null 2>&1; then
            push_note="成功"
        else
            push_note="失敗"
        fi
    fi
    keep=$(ls -1 "$BACKUP_DIR"/space-*.tar.gz 2>/dev/null | wc -l)
    mkdir -p "$SPACE/Trigger"
    {
        echo "# Backup Status"
        echo ""
        echo "- 時間:$(date '+%Y-%m-%d %H:%M:%S %z')"
        echo "- NAS 每日備份:$backup_note → $archive"
        echo "- 保留份數:$keep(上限 $BACKUP_KEEP)"
        echo "- GitLab push:$push_note"
        echo ""
        echo "(NAS funnel_watchdog.sh 每日 04:00 後首個分鐘產生;"
        echo "手動離站備份另有 sb_backup.py)"
    } > "$BACKUP_STATUS"
    chown "$(stat -c %u:%g "$SPACE")" "$BACKUP_STATUS" 2>/dev/null
}

# C) 每日備份+push(先跑,避免被 A/B 的 exit 擋掉)
daily_backup

# A) 頁面手動觸發
if [ -f "$TRIG" ]; then
    rm -f "$TRIG"
    bounce "頁面手動觸發"
    exit 0
fi

# B) 看門狗:本地有 funnel 設定才看;DNS 兩個公共解析器都查無才動作
"$TS" funnel status 2>/dev/null | grep -q "$FUNNEL_TARGET" || exit 0
nslookup "$HOST" 8.8.8.8 >/dev/null 2>&1 && exit 0
nslookup "$HOST" 1.1.1.1 >/dev/null 2>&1 && exit 0
now=$(date +%s)
last=$(cat "$COOLDOWN" 2>/dev/null || echo 0)
[ $((now - last)) -lt 600 ] && exit 0
bounce "看門狗:公共 DNS 消失,自動修復"
