#!/bin/bash
# 高新医疗科技有限公司 - 自动备份脚本
# 版本: 2.0
# 部署日期: 2026-02-02
# 建议添加到crontab: 0 2 * * * /opt/gaoxin_medical/backup.sh

set -e

# ==========================================
# 配置
# ==========================================
BACKUP_DIR="/backup/gaoxin_medical"
PROJECT_DIR="/opt/gaoxin_medical"
KEEP_DAYS=7
DATE=$(date +%Y%m%d_%H%M%S)
HOSTNAME=$(hostname)
LOG_DIR="/var/log/gaoxin_medical"
LOG_FILE="$LOG_DIR/backup.log"
ENV_FILE="$PROJECT_DIR/.env"

if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
fi

DB_BACKEND="${DB_BACKEND:-postgres}"

# 远程备份配置（可选）
REMOTE_BACKUP=false
REMOTE_HOST=""
REMOTE_USER=""
REMOTE_PATH=""

# ==========================================
# 日志函数
# ==========================================
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log_error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $1" | tee -a "$LOG_FILE"
}

# ==========================================
# 备份函数
# ==========================================

# 备份数据库（使用SQLite在线备份API）
backup_database() {
    log "[1/4] 备份数据库..."

    if [ "${DB_BACKEND,,}" = "sqlite" ]; then
        local db_path="$PROJECT_DIR/udid_hybrid_lake.db"

        if [ ! -f "$db_path" ]; then
            log_error "SQLite 数据库文件不存在: $db_path"
            return 1
        fi

        # 使用SQLite在线备份API（不锁定数据库）
        local sqlite_backup="$BACKUP_DIR/db_$DATE.db"
        if sqlite3 "$db_path" ".backup '$sqlite_backup'"; then
            log "SQLite 备份成功: $sqlite_backup"
            gzip -f "$sqlite_backup"
            chmod 600 "$sqlite_backup.gz"
            log "SQLite 备份已压缩: ${sqlite_backup}.gz"
            return 0
        fi
        log_error "SQLite 数据库备份失败"
        return 1
    fi

    local pg_host="${POSTGRES_HOST:-127.0.0.1}"
    local pg_port="${POSTGRES_PORT:-5432}"
    local pg_db="${POSTGRES_DB:-udid_db}"
    local pg_user="${POSTGRES_USER:-udid_user}"
    local pg_password="${POSTGRES_PASSWORD:-}"
    local backup_file="$BACKUP_DIR/db_$DATE.pgdump"

    if [ -z "$pg_password" ]; then
        log_error "POSTGRES_PASSWORD 未配置，无法执行 PostgreSQL 备份"
        return 1
    fi

    if PGPASSWORD="$pg_password" pg_dump -h "$pg_host" -p "$pg_port" -U "$pg_user" -d "$pg_db" -Fc -f "$backup_file"; then
        chmod 600 "$backup_file"
        log "PostgreSQL 备份成功: $backup_file"
    else
        log_error "PostgreSQL 备份失败"
        return 1
    fi
}

# 备份配置文件
backup_config() {
    log "[2/4] 备份配置文件..."

    local config_backup="$BACKUP_DIR/config_$DATE.json"
    local env_backup="$BACKUP_DIR/env_$DATE"

    if [ -f "$PROJECT_DIR/config.json" ]; then
        cp "$PROJECT_DIR/config.json" "$config_backup"
        chmod 600 "$config_backup"
        log "配置文件备份成功"
    fi

    if [ -f "$PROJECT_DIR/.env" ]; then
        cp "$PROJECT_DIR/.env" "$env_backup"
        chmod 600 "$env_backup"
        log "环境变量备份成功"
    fi
}

# 备份FAISS索引
backup_faiss() {
    log "[3/4] 备份向量索引..."

    local faiss_dir="$PROJECT_DIR/data/faiss_index"

    if [ -d "$faiss_dir" ]; then
        local faiss_backup="$BACKUP_DIR/faiss_index_$DATE.tar.gz"

        tar czf "$faiss_backup" -C "$PROJECT_DIR/data" faiss_index
        chmod 600 "$faiss_backup"

        log "FAISS索引备份成功: $faiss_backup"
    else
        log "FAISS索引目录不存在，跳过"
    fi
}

# 创建完整备份包
create_full_backup() {
    log "[4/4] 创建完整备份包..."

    cd "$BACKUP_DIR" || exit 1

    # 创建完整备份包
    local full_backup="full_backup_$DATE.tar.gz"

    tar czf "$full_backup" db_*.pgdump db_*.db.gz config_*.json env_* faiss_index_*.tar.gz 2>/dev/null || \
    tar czf "$full_backup" db_*.pgdump db_*.db.gz config_*.json env_* 2>/dev/null || \
    tar czf "$full_backup" db_*.pgdump db_*.db.gz

    chmod 600 "$full_backup"

    # 删除临时文件
    rm -f db_*.pgdump db_*.db.gz config_*.json env_* faiss_index_*.tar.gz

    log "完整备份包创建成功: $full_backup"
}

# 验证备份完整性
verify_backup() {
    log "验证备份完整性..."

    local full_backup="$BACKUP_DIR/full_backup_$DATE.tar.gz"

    if [ -f "$full_backup" ]; then
        if tar tzf "$full_backup" > /dev/null 2>&1; then
            log "备份包验证通过"
        else
            log_error "备份包验证失败"
            return 1
        fi
    else
        log_error "备份包不存在"
        return 1
    fi
}

# 清理旧备份
cleanup_old_backups() {
    log "清理旧备份 (保留最近 $KEEP_DAYS 天)..."

    # 清理完整备份包
    find "$BACKUP_DIR" -name "full_backup_*.tar.gz" -mtime +$KEEP_DAYS -delete

    # 清理旧的数据库备份
    find "$BACKUP_DIR" -name "db_*.db.gz" -mtime +$KEEP_DAYS -delete
    find "$BACKUP_DIR" -name "db_*.pgdump" -mtime +$KEEP_DAYS -delete

    # 清理旧的配置备份
    find "$BACKUP_DIR" -name "config_*.json" -mtime +$KEEP_DAYS -delete
    find "$BACKUP_DIR" -name "env_*" -mtime +$KEEP_DAYS -delete

    # 清理旧的FAISS备份
    find "$BACKUP_DIR" -name "faiss_index_*.tar.gz" -mtime +$KEEP_DAYS -delete

    log "旧备份清理完成"
}

# 远程备份（可选）
remote_backup() {
    if [ "$REMOTE_BACKUP" = true ] && [ -n "$REMOTE_HOST" ]; then
        log "上传备份到远程服务器..."

        local full_backup="$BACKUP_DIR/full_backup_$DATE.tar.gz"

        if scp "$full_backup" "$REMOTE_USER@$REMOTE_HOST:$REMOTE_PATH/"; then
            log "远程备份成功"
        else
            log_error "远程备份失败"
        fi
    fi
}

# 显示备份信息
show_backup_info() {
    log "========================================"
    log "备份完成"
    log "========================================"

    local full_backup="$BACKUP_DIR/full_backup_$DATE.tar.gz"

    if [ -f "$full_backup" ]; then
        log "备份文件: $full_backup"
        log "文件大小: $(du -h "$full_backup" | cut -f1)"
    fi

    log ""
    log "备份列表:"
    ls -lh "$BACKUP_DIR"/*.tar.gz 2>/dev/null | tail -5 || log "(无备份文件)"

    log ""
    log "磁盘使用情况:"
    df -h "$BACKUP_DIR"
}

# ==========================================
# 主程序
# ==========================================
main() {
    # 创建必要的目录
    mkdir -p "$BACKUP_DIR"
    mkdir -p "$LOG_DIR"

    log "========================================"
    log "高新医疗科技有限公司 - 数据备份"
    log "时间: $(date)"
    log "========================================"

    # 执行备份
    backup_database
    backup_config
    backup_faiss
    create_full_backup
    verify_backup
    cleanup_old_backups
    remote_backup
    show_backup_info
}

# 执行主程序
main "$@"
