#!/bin/sh
# funnel_watchdog_hourly.sh — DSM 任務排程器**每小時**執行(DSM 最小間隔限制)。
# 進場先以 flock 防重疊,然後在本小時內每 60 秒跑一次 funnel_watchdog.sh,
# 約 58 分鐘後退出交棒給下一輪;若中途死亡,最多一小時後下一輪自癒。
# WD_INTERVAL / WD_DURATION 環境變數僅供測試縮短用。

LOCK=/tmp/funnel_watchdog_hourly.lock
WD=/volume1/docker/silverbullet-mcp/funnel_watchdog.sh
INTERVAL="${WD_INTERVAL:-60}"
DURATION="${WD_DURATION:-3480}"
DBG=/tmp/funnel_watchdog_wrapper.log

# 防重疊:flock 自行開鎖檔並重新執行本腳本(fd 交給 flock 管理——
# synoschedtask 環境下 shell 的 exec 9> 會拿到壞掉的 fd,別用)。
# flock 需用絕對路徑:synoschedtask 的 PATH 極簡。
if [ "$1" != "locked" ]; then
    echo "$(date '+%F %T') enter pid=$$" >> "$DBG"
    exec /usr/bin/flock -n "$LOCK" sh "$0" locked
fi
echo "$(date '+%F %T') lock acquired pid=$$" >> "$DBG"

end=$(( $(date +%s) + DURATION ))
while [ "$(date +%s)" -lt "$end" ]; do
    sh "$WD"
    date +%s > /tmp/funnel_watchdog_hb
    sleep "$INTERVAL"
done
echo "$(date '+%F %T') normal end pid=$$" >> "$DBG"
