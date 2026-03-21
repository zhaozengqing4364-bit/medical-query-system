#!/bin/bash
# ============================================
# PostgreSQL 迁移脚本
# 高新医疗科技有限公司 SQLite → PostgreSQL
# ============================================

set -e  # 遇到错误立即退出

# 配置
PROJECT_DIR="/Users/zhaozengqing/github/AI/test/高新医疗"
BACKUP_DIR="$PROJECT_DIR/backup/$(date +%Y%m%d_%H%M%S)"
DB_NAME="udid_db"
DB_USER="udid_user"
DB_PASSWORD="${POSTGRES_PASSWORD:-}"
DB_HOST="${POSTGRES_HOST:-127.0.0.1}"
DB_PORT="${POSTGRES_PORT:-5432}"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

if [ -z "$DB_PASSWORD" ] || [ "$DB_PASSWORD" = "your_secure_password" ]; then
    log_error "必须通过环境变量 POSTGRES_PASSWORD 提供数据库密码，且不能使用占位值"
    exit 1
fi

# ============================================
# 步骤 1: 备份
# ============================================
step_backup() {
    log_info "步骤 1: 创建备份..."

    mkdir -p "$BACKUP_DIR"

    # 备份 SQLite
    if [ -f "$PROJECT_DIR/udid_hybrid_lake.db" ]; then
        cp "$PROJECT_DIR/udid_hybrid_lake.db" "$BACKUP_DIR/"
        log_info "SQLite 备份完成"
    fi

    # 备份 FAISS 索引
    if [ -d "$PROJECT_DIR/data/faiss_index" ]; then
        cp -r "$PROJECT_DIR/data/faiss_index" "$BACKUP_DIR/"
        log_info "FAISS 索引备份完成"
    fi

    # 备份配置文件
    cp "$PROJECT_DIR/config.json" "$BACKUP_DIR/" 2>/dev/null || true
    cp "$PROJECT_DIR/.env" "$BACKUP_DIR/" 2>/dev/null || true

    log_info "备份完成: $BACKUP_DIR"
}

# ============================================
# 步骤 2: 检查环境
# ============================================
step_check() {
    log_info "步骤 2: 检查环境..."

    # 检查 PostgreSQL
    if ! command -v psql &> /dev/null; then
        log_error "未找到 psql 命令，请先安装 PostgreSQL"
        exit 1
    fi

    # 检查 pgloader
    if ! command -v pgloader &> /dev/null; then
        log_error "未找到 pgloader 命令，请先安装"
        exit 1
    fi

    # 检查数据库连接
    if ! PGPASSWORD=$DB_PASSWORD psql -h "$DB_HOST" -p "$DB_PORT" -U $DB_USER -d $DB_NAME -c "SELECT 1" &> /dev/null; then
        log_error "无法连接到 PostgreSQL 数据库"
        log_info "请确保数据库已创建: createdb -U postgres $DB_NAME"
        exit 1
    fi

    log_info "环境检查通过"
}

# ============================================
# 步骤 3: 初始化 Schema
# ============================================
step_schema() {
    log_info "步骤 3: 初始化数据库 Schema..."

    PGPASSWORD=$DB_PASSWORD psql -h "$DB_HOST" -p "$DB_PORT" -U $DB_USER -d $DB_NAME -f "$PROJECT_DIR/scripts/setup_postgres.sql"

    log_info "Schema 初始化完成"
}

# ============================================
# 步骤 4: 数据迁移
# ============================================
step_migrate() {
    log_info "步骤 4: 执行数据迁移..."
    log_warn "这可能需要 15-30 分钟，请耐心等待..."

    cd "$PROJECT_DIR"

    # 使用 pgloader 迁移
    PGPASSWORD="$DB_PASSWORD" pgloader "$PROJECT_DIR/scripts/migration.load"

    log_info "数据迁移完成"
}

# ============================================
# 步骤 5: 验证数据
# ============================================
step_verify() {
    log_info "步骤 5: 验证数据完整性..."
    verify_failed=0

    # 获取 SQLite 记录数
    SQLITE_PRODUCTS=$(sqlite3 "$PROJECT_DIR/udid_hybrid_lake.db" "SELECT COUNT(*) FROM products;" 2>/dev/null || echo "0")
    SQLITE_EMBEDDINGS=$(sqlite3 "$PROJECT_DIR/udid_hybrid_lake.db" "SELECT COUNT(*) FROM embeddings;" 2>/dev/null || echo "0")

    # 获取 PostgreSQL 记录数
    PG_PRODUCTS=$(PGPASSWORD=$DB_PASSWORD psql -h "$DB_HOST" -p "$DB_PORT" -U $DB_USER -d $DB_NAME -t -c "SELECT COUNT(*) FROM products;" 2>/dev/null | xargs || echo "0")
    PG_EMBEDDINGS=$(PGPASSWORD=$DB_PASSWORD psql -h "$DB_HOST" -p "$DB_PORT" -U $DB_USER -d $DB_NAME -t -c "SELECT COUNT(*) FROM embeddings;" 2>/dev/null | xargs || echo "0")

    echo ""
    echo "================== 数据对比 =================="
    printf "%-20s %10s %10s %10s\n" "表名" "SQLite" "PostgreSQL" "状态"
    echo "--------------------------------------------"

    # 验证 products
    if [ "$SQLITE_PRODUCTS" -eq "$PG_PRODUCTS" ] 2>/dev/null; then
        printf "%-20s %10s %10s ${GREEN}%10s${NC}\n" "products" "$SQLITE_PRODUCTS" "$PG_PRODUCTS" "✓ 一致"
    else
        printf "%-20s %10s %10s ${RED}%10s${NC}\n" "products" "$SQLITE_PRODUCTS" "$PG_PRODUCTS" "✗ 不一致"
        verify_failed=1
    fi

    # 验证 embeddings
    if [ "$SQLITE_EMBEDDINGS" -eq "$PG_EMBEDDINGS" ] 2>/dev/null; then
        printf "%-20s %10s %10s ${GREEN}%10s${NC}\n" "embeddings" "$SQLITE_EMBEDDINGS" "$PG_EMBEDDINGS" "✓ 一致"
    else
        printf "%-20s %10s %10s ${RED}%10s${NC}\n" "embeddings" "$SQLITE_EMBEDDINGS" "$PG_EMBEDDINGS" "✗ 不一致"
        verify_failed=1
    fi

    echo "============================================"

    # 随机抽样检查
    log_info "随机抽样检查..."
    SAMPLE=$(sqlite3 "$PROJECT_DIR/udid_hybrid_lake.db" "SELECT di_code FROM products ORDER BY RANDOM() LIMIT 1;")
    log_info "抽样 DI: $SAMPLE"

    SQLITE_NAME=$(sqlite3 "$PROJECT_DIR/udid_hybrid_lake.db" "SELECT product_name FROM products WHERE di_code = '$SAMPLE';")
    PG_NAME=$(PGPASSWORD=$DB_PASSWORD psql -h "$DB_HOST" -p "$DB_PORT" -U $DB_USER -d $DB_NAME -t -c "SELECT product_name FROM products WHERE di_code = '$SAMPLE';" | xargs)

    if [ "$SQLITE_NAME" = "$PG_NAME" ]; then
        log_info "抽样验证通过: $PG_NAME"
    else
        log_warn "抽样验证不一致"
        log_info "  SQLite: $SQLITE_NAME"
        log_info "  PostgreSQL: $PG_NAME"
        verify_failed=1
    fi

    if [ "$verify_failed" -ne 0 ]; then
        log_error "迁移验证失败，流程终止"
        exit 1
    fi
}

# ============================================
# 步骤 6: 测试搜索功能
# ============================================
step_test() {
    log_info "步骤 6: 测试搜索功能..."

    # 启动服务（在后台）
    log_info "启动测试服务..."

    # 这里假设已经修改了代码，使用 PostgreSQL
    # cd "$PROJECT_DIR" && python udid_server.py &
    # sleep 3

    # 测试搜索 API
    # curl -s "http://localhost:5000/api/search?keyword=心脏&limit=5" | jq '.'

    log_info "请手动测试搜索功能"
}

# ============================================
# 主流程
# ============================================
main() {
    echo "=========================================="
    echo "    高新医疗科技有限公司 PostgreSQL 迁移脚本"
    echo "=========================================="
    echo ""

    # 确认执行
    read -p "确定要执行迁移吗？这将修改数据库。 (yes/no): " confirm
    if [ "$confirm" != "yes" ]; then
        log_info "已取消"
        exit 0
    fi

    # 执行步骤
    step_backup
    step_check
    step_schema
    step_migrate
    step_verify
    step_test

    echo ""
    echo "=========================================="
    log_info "迁移完成！"
    echo "=========================================="
    echo ""
    echo "后续步骤:"
    echo "  1. 确认 .env 中 DB_BACKEND=postgres"
    echo "  2. 重启服务"
    echo "  3. 验证关键 API 与同步链路"
    echo ""
    echo "备份位置: $BACKUP_DIR"
}

# 执行
main "$@"
