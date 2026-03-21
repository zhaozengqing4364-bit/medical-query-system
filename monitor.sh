#!/bin/bash
# 高新医疗UDID查询系统 - 服务监控脚本
# 版本: 2.0
# 部署日期: 2026-02-02
# 建议添加到crontab: */5 * * * * /opt/gaoxin_medical/monitor.sh

# ==========================================
# 配置
# ==========================================
PROJECT_DIR="/opt/gaoxin_medical"
SERVICE_NAME="gaoxin-medical"
API_URL="http://localhost:8080/api/stats"
LOG_DIR="/var/log/gaoxin_medical"
LOG_FILE="$LOG_DIR/monitor.log"
ALERT_WEBHOOK=""  # 填写钉钉/企业微信 webhook URL
ALERT_EMAIL=""    # 填写告警邮箱
ENV_FILE="$PROJECT_DIR/.env"

if [ -f "$ENV_FILE" ]; then
    set -a
    . "$ENV_FILE"
    set +a
fi

DB_BACKEND="${DB_BACKEND:-postgres}"

# 阈值配置
DISK_THRESHOLD=85
MEMORY_THRESHOLD=90
CPU_THRESHOLD=90
API_TIMEOUT=10

# ==========================================
# 日志函数
# ==========================================
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a $LOG_FILE
}

log_error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $1" | tee -a $LOG_FILE
}

log_warn() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [WARN] $1" | tee -a $LOG_FILE
}

# ==========================================
# 告警函数
# ==========================================
send_alert() {
    local message="$1"
    local level="${2:-WARNING}"
    log "[$level] $message"

    # 钉钉告警
    if [ -n "$ALERT_WEBHOOK" ]; then
        curl -s "$ALERT_WEBHOOK" \
            -H "Content-Type: application/json" \
            -d "{\"msgtype\": \"text\", \"text\": {\"content\": \"高新医疗系统告警[$level]：$message\"}}" \
            > /dev/null 2>&1
    fi

    # 邮件告警（如果配置了sendmail）
    if [ -n "$ALERT_EMAIL" ] && command -v mail >/dev/null 2>&1; then
        echo "$message" | mail -s "高新医疗系统告警[$level]" "$ALERT_EMAIL"
    fi
}

# ==========================================
# 检查函数
# ==========================================

# 检查服务进程
check_process() {
    if pgrep -f "udid_server.py" > /dev/null; then
        return 0
    else
        return 1
    fi
}

# 检查systemd服务状态
check_systemd_service() {
    if systemctl is-active --quiet $SERVICE_NAME; then
        return 0
    else
        return 1
    fi
}

# 检查API接口
check_api() {
    local response
    response=$(curl -s -f --max-time $API_TIMEOUT "$API_URL" 2>/dev/null)
    if [ $? -eq 0 ] && [ -n "$response" ]; then
        # 检查返回是否为有效JSON
        if echo "$response" | python3 -c "import json,sys; json.load(sys.stdin)" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

# 检查磁盘空间
check_disk() {
    local usage
    usage=$(df "$PROJECT_DIR" | tail -1 | awk '{print $5}' | sed 's/%//')
    if [ "$usage" -gt "$DISK_THRESHOLD" ]; then
        send_alert "磁盘空间不足，使用率: ${usage}%" "CRITICAL"
        return 1
    fi

    # 检查备份目录
    if [ -d "/backup/gaoxin_medical" ]; then
        local backup_usage
        backup_usage=$(df "/backup/gaoxin_medical" | tail -1 | awk '{print $5}' | sed 's/%//')
        if [ "$backup_usage" -gt "$DISK_THRESHOLD" ]; then
            send_alert "备份磁盘空间不足，使用率: ${backup_usage}%" "CRITICAL"
            return 1
        fi
    fi

    return 0
}

# 检查内存
check_memory() {
    local usage
    usage=$(free | grep Mem | awk '{printf "%.0f", $3/$2 * 100.0}')
    if [ "$usage" -gt "$MEMORY_THRESHOLD" ]; then
        send_alert "内存使用率过高: ${usage}%" "WARNING"
        return 1
    fi
    return 0
}

# 检查CPU
check_cpu() {
    local usage
    usage=$(top -bn1 | grep "Cpu(s)" | awk '{print $2}' | cut -d'%' -f1)
    if [ -n "$usage" ] && [ "${usage%.*}" -gt "$CPU_THRESHOLD" ]; then
        send_alert "CPU使用率过高: ${usage}%" "WARNING"
        return 1
    fi
    return 0
}

# 检查日志文件大小
check_log_size() {
    local max_size=$((100 * 1024 * 1024))  # 100MB
    local log_files=("$LOG_DIR/access.log" "$LOG_DIR/error.log")

    for log_file in "${log_files[@]}"; do
        if [ -f "$log_file" ]; then
            local size
            size=$(stat -f%z "$log_file" 2>/dev/null || stat -c%s "$log_file" 2>/dev/null)
            if [ "$size" -gt "$max_size" ]; then
                log_warn "日志文件过大: $log_file (${size} bytes)"
                # 触发日志轮转
                logrotate -f /etc/logrotate.d/$SERVICE_NAME 2>/dev/null || true
            fi
        fi
    done
}

# 检查数据库可访问性
check_database() {
    if [ "${DB_BACKEND,,}" = "sqlite" ]; then
        local db_path="$PROJECT_DIR/udid_hybrid_lake.db"
        if [ -f "$db_path" ]; then
            if ! sqlite3 "$db_path" "SELECT 1;" > /dev/null 2>&1; then
                send_alert "SQLite 数据库无法访问" "CRITICAL"
                return 1
            fi
        fi
        return 0
    fi

    local pg_host="${POSTGRES_HOST:-127.0.0.1}"
    local pg_port="${POSTGRES_PORT:-5432}"
    local pg_db="${POSTGRES_DB:-udid_db}"
    local pg_user="${POSTGRES_USER:-udid_user}"
    local pg_password="${POSTGRES_PASSWORD:-}"

    if [ -z "$pg_password" ]; then
        send_alert "POSTGRES_PASSWORD 未配置，无法执行数据库健康检查" "CRITICAL"
        return 1
    fi

    if ! PGPASSWORD="$pg_password" psql -h "$pg_host" -p "$pg_port" -U "$pg_user" -d "$pg_db" -c "SELECT 1;" >/dev/null 2>&1; then
            send_alert "PostgreSQL 无法访问" "CRITICAL"
            return 1
    fi

    return 0
}

# 检查FAISS索引
check_faiss_index() {
    local index_path="$PROJECT_DIR/data/faiss_index/index.faiss"
    if [ ! -f "$index_path" ]; then
        log_warn "FAISS索引文件不存在"
        return 1
    fi
    return 0
}

# 收集系统统计信息
collect_stats() {
    local stats=""
    stats="时间: $(date '+%Y-%m-%d %H:%M:%S')\n"

    # 系统负载
    stats+="负载: $(uptime | awk -F'load average:' '{print $2}')\n"

    # 内存使用
    stats+="内存: $(free -h | grep Mem | awk '{print $3"/"$2}')\n"

    # 磁盘使用
    stats+="磁盘: $(df -h "$PROJECT_DIR" | tail -1 | awk '{print $5}')\n"

    # 进程数
    stats+="进程: $(pgrep -f "udid_server.py" | wc -l)\n"

    echo -e "$stats"
}

# ==========================================
# 主检查流程
# ==========================================
main() {
    # 创建日志目录
    mkdir -p "$LOG_DIR"

    log "开始服务健康检查..."

    local need_restart=false
    local issues=()

    # 1. 检查systemd服务
    if ! check_systemd_service; then
        log_error "systemd服务未运行"
        issues+=("systemd服务未运行")
        need_restart=true
    else
        log "OK: systemd服务运行正常"
    fi

    # 2. 检查进程
    if ! check_process; then
        log_error "服务进程未运行"
        issues+=("服务进程未运行")
        need_restart=true
    else
        log "OK: 服务进程运行正常"
    fi

    # 3. 检查API
    if ! check_api; then
        log_error "API接口无法访问"
        issues+=("API接口无法访问")
        need_restart=true
    else
        log "OK: API接口正常"
    fi

    # 4. 检查数据库
    if ! check_database; then
        log_error "数据库检查失败"
        issues+=("数据库检查失败")
    else
        log "OK: 数据库可访问"
    fi

    # 5. 检查FAISS索引
    if ! check_faiss_index; then
        log_warn "FAISS索引检查失败"
    else
        log "OK: FAISS索引存在"
    fi

    # 6. 检查磁盘
    if ! check_disk; then
        issues+=("磁盘空间不足")
    fi

    # 7. 检查内存
    if ! check_memory; then
        issues+=("内存使用率过高")
    fi

    # 8. 检查CPU
    if ! check_cpu; then
        issues+=("CPU使用率过高")
    fi

    # 9. 检查日志大小
    check_log_size

    # 收集统计信息
    log "系统统计:\n$(collect_stats)"

    # 尝试重启服务
    if [ "$need_restart" = true ]; then
        send_alert "检测到问题: ${issues[*]}，尝试重启服务..." "CRITICAL"
        log "尝试重启服务..."

        systemctl restart $SERVICE_NAME
        sleep 5

        if check_api; then
            log "OK: 服务重启成功"
            send_alert "服务已自动重启并恢复正常" "RECOVERED"
        else
            log_error "服务重启失败"
            send_alert "服务重启失败，请手动检查" "CRITICAL"
        fi
    fi

    log "健康检查完成"
}

# 执行检查
main "$@"
