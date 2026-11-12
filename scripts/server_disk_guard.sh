#!/usr/bin/env bash
# Ежемесячная проверка диска и безопасная чистка Docker build cache.
# Запускается от root по cron 1-го числа в 04:00.
set -euo pipefail

PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
THRESHOLD="${DISK_THRESHOLD:-80}"
LOG_TAG="$(date '+%Y-%m-%d %H:%M:%S')"

usage() {
    df -P / | awk 'NR==2 {gsub("%", "", $5); print $5}'
}

before="$(usage)"
echo "$LOG_TAG start: / used ${before}% (threshold ${THRESHOLD}%)"

if [ "$before" -lt "$THRESHOLD" ]; then
    echo "$LOG_TAG ok: cleaning not needed"
    exit 0
fi

if [ "${DRY_RUN:-0}" = "1" ]; then
    echo "$LOG_TAG dry-run: would run docker builder prune -af"
    exit 0
fi

echo "$LOG_TAG cleaning: docker builder prune -af"
docker builder prune -af || echo "$LOG_TAG warning: docker builder prune failed"

# Дополнительно подчищаем системные кэши, если диск всё ещё забит.
after="$(usage)"
if [ "$after" -ge 90 ]; then
    echo "$LOG_TAG still ${after}%: journalctl/apt clean"
    journalctl --vacuum-size=200M >/dev/null 2>&1 || true
    apt-get clean || true
    rm -rf /root/.cache/pip
fi

after="$(usage)"
echo "$LOG_TAG done: / used ${after}% (was ${before}%)"
