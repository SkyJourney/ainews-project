#!/bin/sh
# ainews-service Postgres 每日备份脚本（M7 生产化收尾，04 §2.8）。
#
# 跑在独立的 postgres-backup 容器里，直接复用 postgres:16-alpine 镜像自带的 pg_dump
# （与 db 服务同版本，避免 pg_dump/服务端版本不一致的兼容问题），不需要额外镜像。
# 决策：只做 pg_dump 定期快照，不上 WAL 归档做 PITR——纯资讯聚合内容，可以接受
# "最多丢失一天"的 RPO，见 .claude/memory/decisions.md。
#
# 全部时间计算走 UTC epoch 秒数算术，不依赖 GNU date 的 `-d` 扩展语法（Alpine busybox
# date 不支持），可移植。BACKUP_HOUR_UTC 默认 19（=Asia/Shanghai 03:00，避开每日
# 09:00 Asia/Shanghai 的 pipeline 批次运行时间）。

set -eu

BACKUP_DIR="${BACKUP_DIR:-/backups}"
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-14}"
BACKUP_HOUR_UTC="${BACKUP_HOUR_UTC:-19}"
POSTGRES_DB="${POSTGRES_DB:-ainews_content}"
POSTGRES_USER="${POSTGRES_USER:-ainews}"

mkdir -p "$BACKUP_DIR"

log() {
    echo "[pg_backup] $(date -u '+%Y-%m-%dT%H:%M:%SZ') $*"
}

run_backup() {
    ts="$(date -u '+%Y-%m-%dT%H%M%SZ')"
    out="$BACKUP_DIR/${POSTGRES_DB}-${ts}.dump"
    log "开始备份 -> $out"
    if pg_dump -h db -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "$out"; then
        log "备份成功：$(du -h "$out" | cut -f1)"
    else
        log "备份失败，清理不完整文件"
        rm -f "$out"
        return 1
    fi
    find "$BACKUP_DIR" -name "${POSTGRES_DB}-*.dump" -mtime "+${RETENTION_DAYS}" -exec sh -c 'echo "[pg_backup] 清理过期备份：$1"; rm -f "$1"' _ {} \;
}

sleep_until_next_run() {
    now=$(date -u +%s)
    today_midnight=$(( now - (now % 86400) ))
    target=$(( today_midnight + BACKUP_HOUR_UTC * 3600 ))
    if [ "$target" -le "$now" ]; then
        target=$(( target + 86400 ))
    fi
    sleep_seconds=$(( target - now ))
    log "下一次备份将在 ${sleep_seconds} 秒后（UTC epoch ${target}）"
    sleep "$sleep_seconds"
}

# 容器启动时先跑一次，立即验证配置正确（凭证/网络/挂载路径），之后按每日固定时间点循环。
run_backup || log "首次备份失败，将在下一个预定时间点重试"

while true; do
    sleep_until_next_run
    run_backup || log "本次备份失败，将在下一个预定时间点重试"
done
