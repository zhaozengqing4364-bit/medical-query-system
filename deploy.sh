#!/bin/bash
# 高新医疗UDID查询系统 - 生产环境部署脚本
# 版本: 2.0
# 部署日期: 2026-02-02
# 使用方法: sudo ./deploy.sh [选项]
#
# 选项:
#   --with-ssl    启用SSL/HTTPS配置（需要域名）
#   --domain      指定域名（用于SSL证书申请）

set -e

# ==========================================
# 配置变量
# ==========================================
PROJECT_DIR="/opt/gaoxin_medical"
BACKUP_DIR="/backup/gaoxin_medical"
LOG_DIR="/var/log/gaoxin_medical"
SERVICE_NAME="gaoxin-medical"
NGINX_SERVICE_NAME="gaoxin-medical"
PORT=8080
USE_SSL=false
DOMAIN=""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# ==========================================
# 函数定义
# ==========================================
log_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

log_success() {
    echo -e "${GREEN}[SUCCESS]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

# 检查root权限
check_root() {
    if [ "$EUID" -ne 0 ]; then
        log_error "请使用 root 权限运行此脚本"
        echo "请使用: sudo ./deploy.sh"
        exit 1
    fi
}

# 解析命令行参数
parse_args() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --with-ssl)
                USE_SSL=true
                shift
                ;;
            --domain)
                DOMAIN="$2"
                shift 2
                ;;
            --help)
                echo "使用方法: sudo ./deploy.sh [选项]"
                echo ""
                echo "选项:"
                echo "  --with-ssl    启用SSL/HTTPS配置（需要域名）"
                echo "  --domain      指定域名（用于SSL证书申请）"
                echo "  --help        显示此帮助信息"
                exit 0
                ;;
            *)
                log_warn "未知选项: $1"
                shift
                ;;
        esac
    done

    if [ "$USE_SSL" = true ] && [ -z "$DOMAIN" ]; then
        log_error "使用 --with-ssl 时必须指定 --domain"
        exit 1
    fi
}

# 更新系统
update_system() {
    log_info "[1/10] 更新系统..."
    apt update && apt upgrade -y
    log_success "系统更新完成"
}

# 安装基础依赖
install_dependencies() {
    log_info "[2/10] 安装基础依赖..."
    apt install -y \
        python3 python3-pip python3-venv \
        git wget curl unzip \
        nginx postgresql-client \
        logrotate \
        software-properties-common

    # 安装Certbot（如果需要SSL）
    if [ "$USE_SSL" = true ]; then
        apt install -y certbot python3-certbot-nginx
    fi

    log_success "基础依赖安装完成"
}

# 创建目录结构
create_directories() {
    log_info "[3/10] 创建目录结构..."

    # 项目目录
    mkdir -p $PROJECT_DIR
    mkdir -p $PROJECT_DIR/logs
    mkdir -p $PROJECT_DIR/data/embedding_batch

    # 备份目录
    mkdir -p $BACKUP_DIR

    # 日志目录
    mkdir -p $LOG_DIR

    # 创建日志文件
    touch $LOG_DIR/access.log
    touch $LOG_DIR/error.log
    touch $LOG_DIR/monitor.log
    touch $LOG_DIR/backup.log
    touch $LOG_DIR/sync.log

    log_success "目录结构创建完成"
}

# 检查必需文件
check_required_files() {
    log_info "[4/10] 检查必需文件..."

    local required_files=(
        "udid_server.py"
        "udid_hybrid_system.py"
        "ai_service.py"
        "embedding_service.py"
        "embedding_faiss.py"
        "requirements.txt"
        "config.json"
        ".env"
        "udid_viewer.html"
        "admin.html"
        "login.html"
    )

    local missing_files=()

    for file in "${required_files[@]}"; do
        if [ ! -f "$file" ]; then
            missing_files+=("$file")
        else
            echo "  ✓ $file"
        fi
    done

    if [ ${#missing_files[@]} -gt 0 ]; then
        log_error "缺少以下必需文件:"
        for file in "${missing_files[@]}"; do
            echo "    - $file"
        done
        echo ""
        echo "请确保所有项目文件已上传到当前目录"
        exit 1
    fi

    # 检查数据库文件
    local db_backend
    db_backend=$(grep -E '^DB_BACKEND=' .env 2>/dev/null | tail -n1 | cut -d'=' -f2 | tr '[:upper:]' '[:lower:]')
    if [ -z "$db_backend" ]; then
        db_backend="postgres"
    fi

    if [ "$db_backend" = "sqlite" ]; then
        if [ ! -f "udid_hybrid_lake.db" ]; then
            log_warn "SQLite 数据库文件 udid_hybrid_lake.db 不存在"
            echo "      当前 DB_BACKEND=sqlite，请手动上传数据库文件到 $PROJECT_DIR"
        else
            echo "  ✓ udid_hybrid_lake.db"
        fi
    else
        echo "  ✓ DB_BACKEND=$db_backend（将使用 PostgreSQL）"
    fi

    # 检查FAISS索引
    if [ ! -f "data/faiss_index/index.faiss" ]; then
        log_warn "FAISS索引文件不存在"
        echo "      请手动上传索引文件到 $PROJECT_DIR/data/faiss_index/"
    else
        echo "  ✓ data/faiss_index/index.faiss"
    fi

    log_success "必需文件检查完成"
}

# 复制项目文件
copy_project_files() {
    log_info "[5/10] 复制项目文件..."

    # 复制所有Python文件
    cp *.py $PROJECT_DIR/

    # 复制HTML文件
    cp *.html $PROJECT_DIR/

    # 复制配置文件
    cp config.json $PROJECT_DIR/
    cp .env $PROJECT_DIR/
    cp requirements.txt $PROJECT_DIR/

    # 复制脚本文件
    cp deploy.sh $PROJECT_DIR/ 2>/dev/null || true
    cp monitor.sh $PROJECT_DIR/ 2>/dev/null || true
    cp backup.sh $PROJECT_DIR/ 2>/dev/null || true
    cp -r scripts $PROJECT_DIR/ 2>/dev/null || true

    # 复制数据目录
    if [ -d "data" ]; then
        cp -r data $PROJECT_DIR/
    fi

    # 复制数据库文件
    if [ -f "udid_hybrid_lake.db" ]; then
        cp udid_hybrid_lake.db $PROJECT_DIR/
    fi

    log_success "项目文件复制完成"
}

# 创建Python虚拟环境并安装依赖
setup_python_env() {
    log_info "[6/10] 创建Python虚拟环境..."

    cd $PROJECT_DIR

    if [ ! -d "venv" ]; then
        python3 -m venv venv
    fi

    source venv/bin/activate

    log_info "安装Python依赖..."
    pip install --upgrade pip
    pip install -r requirements.txt

    log_success "Python环境配置完成"
}

# 初始化数据库
init_database() {
    log_info "[7/10] 初始化数据库..."
    local db_backend
    db_backend=$(grep -E '^DB_BACKEND=' "$PROJECT_DIR/.env" 2>/dev/null | tail -n1 | cut -d'=' -f2 | tr '[:upper:]' '[:lower:]')
    if [ -z "$db_backend" ]; then
        db_backend="postgres"
    fi

    if [ "$db_backend" = "sqlite" ]; then
        if [ -f "$PROJECT_DIR/udid_hybrid_lake.db" ]; then
            log_warn "当前仍配置为 SQLite。建议切换 DB_BACKEND=postgres 后重新部署。"
        else
            log_warn "SQLite 数据库文件不存在，跳过数据库初始化"
        fi
        return
    fi

    local pg_host pg_port pg_db pg_user pg_password
    pg_host=$(grep -E '^POSTGRES_HOST=' "$PROJECT_DIR/.env" 2>/dev/null | tail -n1 | cut -d'=' -f2 || true)
    pg_port=$(grep -E '^POSTGRES_PORT=' "$PROJECT_DIR/.env" 2>/dev/null | tail -n1 | cut -d'=' -f2 || true)
    pg_db=$(grep -E '^POSTGRES_DB=' "$PROJECT_DIR/.env" 2>/dev/null | tail -n1 | cut -d'=' -f2 || true)
    pg_user=$(grep -E '^POSTGRES_USER=' "$PROJECT_DIR/.env" 2>/dev/null | tail -n1 | cut -d'=' -f2 || true)
    pg_password=$(grep -E '^POSTGRES_PASSWORD=' "$PROJECT_DIR/.env" 2>/dev/null | tail -n1 | cut -d'=' -f2- || true)

    pg_host=${pg_host:-127.0.0.1}
    pg_port=${pg_port:-5432}
    pg_db=${pg_db:-udid_db}
    pg_user=${pg_user:-udid_user}

    if [ -z "$pg_password" ]; then
        log_error "POSTGRES_PASSWORD 不能为空，拒绝继续部署"
        exit 1
    fi

    log_info "检查 PostgreSQL 连通性..."
    if ! PGPASSWORD="$pg_password" psql -h "$pg_host" -p "$pg_port" -U "$pg_user" -d "$pg_db" -c "SELECT 1;" >/dev/null 2>&1; then
        log_error "PostgreSQL 连接失败: ${pg_user}@${pg_host}:${pg_port}/${pg_db}"
        exit 1
    fi

    log_info "执行 PostgreSQL 统计优化..."
    PGPASSWORD="$pg_password" psql -h "$pg_host" -p "$pg_port" -U "$pg_user" -d "$pg_db" -c "ANALYZE;" >/dev/null 2>&1 || \
        log_warn "ANALYZE 执行失败，可稍后手动执行"
    log_success "PostgreSQL 初始化检查完成"
}

# 创建专用用户
create_service_user() {
    log_info "创建专用系统用户..."

    if ! id -u gaoxin &>/dev/null; then
        useradd -r -s /bin/false -d $PROJECT_DIR -M gaoxin
        log_success "创建用户: gaoxin"
    else
        log_info "用户 gaoxin 已存在"
    fi

    # 设置目录权限
    chown -R gaoxin:gaoxin $PROJECT_DIR
    chown -R gaoxin:gaoxin $LOG_DIR
    chown -R gaoxin:gaoxin $BACKUP_DIR

    chmod 750 $PROJECT_DIR
    chmod 600 $PROJECT_DIR/.env
    chmod 600 $PROJECT_DIR/config.json
    chmod -R 755 $PROJECT_DIR/data 2>/dev/null || true

    log_success "用户权限设置完成"
}

# 创建systemd服务
create_systemd_service() {
    log_info "[8/10] 创建systemd服务..."
    local SYNC_SERVICE_NAME="${SERVICE_NAME}-sync"

    # 从.env文件读取环境变量
    local secret_key
    local sync_api_key
    local gunicorn_workers
    secret_key=$(grep "SECRET_KEY=" "$PROJECT_DIR/.env" 2>/dev/null | tail -n1 | cut -d'=' -f2- || true)
    sync_api_key=$(grep "SYNC_API_KEY=" "$PROJECT_DIR/.env" 2>/dev/null | tail -n1 | cut -d'=' -f2- || true)
    gunicorn_workers=$(grep "GUNICORN_WORKERS=" "$PROJECT_DIR/.env" 2>/dev/null | tail -n1 | cut -d'=' -f2- || true)
    if ! [[ "$gunicorn_workers" =~ ^[0-9]+$ ]] || [ "${gunicorn_workers:-0}" -lt 1 ]; then
        gunicorn_workers=4
    fi

    # 如果.env中没有，生成新的
    if [ -z "$secret_key" ] || [ "$secret_key" = "your_secret_key_here_change_in_production" ]; then
        secret_key=$(openssl rand -hex 32)
        sed -i "s/SECRET_KEY=.*/SECRET_KEY=$secret_key/" $PROJECT_DIR/.env
        log_warn "已生成新的SECRET_KEY"
    fi

    if [ -z "$sync_api_key" ] || [ "$sync_api_key" = "your_random_32char_string_here" ]; then
        sync_api_key=$(openssl rand -hex 16)
        sed -i "s/SYNC_API_KEY=.*/SYNC_API_KEY=$sync_api_key/" $PROJECT_DIR/.env
        log_warn "已生成新的SYNC_API_KEY"
    fi

    cat > /etc/systemd/system/$SERVICE_NAME.service << EOF
[Unit]
Description=高新医疗UDID查询系统
After=network.target

[Service]
Type=simple
User=gaoxin
Group=gaoxin
WorkingDirectory=$PROJECT_DIR
Environment=PATH=$PROJECT_DIR/venv/bin
Environment=PYTHONPATH=$PROJECT_DIR
EnvironmentFile=-$PROJECT_DIR/.env
Environment=SECRET_KEY=$secret_key
Environment=SYNC_API_KEY=$sync_api_key

# 安全加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$PROJECT_DIR/data $PROJECT_DIR/logs $LOG_DIR

# Gunicorn配置
ExecStart=$PROJECT_DIR/venv/bin/gunicorn \\
    -w $gunicorn_workers \\
    -k sync \\
    -b 127.0.0.1:$PORT \\
    --timeout 120 \\
    --access-logfile $LOG_DIR/access.log \\
    --error-logfile $LOG_DIR/error.log \\
    --capture-output \\
    --enable-stdio-inheritance \\
    udid_server:app

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    cat > /etc/systemd/system/$SYNC_SERVICE_NAME.service << EOF
[Unit]
Description=高新医疗UDID同步监控服务
After=network.target $SERVICE_NAME.service

[Service]
Type=simple
User=gaoxin
Group=gaoxin
WorkingDirectory=$PROJECT_DIR
Environment=PATH=$PROJECT_DIR/venv/bin
Environment=PYTHONPATH=$PROJECT_DIR
EnvironmentFile=-$PROJECT_DIR/.env
Environment=SYNC_API_KEY=$sync_api_key

NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=$PROJECT_DIR/data $PROJECT_DIR/logs $LOG_DIR

ExecStart=$PROJECT_DIR/venv/bin/python sync_server.py

Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable $SERVICE_NAME
    systemctl enable $SYNC_SERVICE_NAME

    log_success "systemd服务创建完成（应用 + 同步监控）"
}

# 配置Nginx
configure_nginx() {
    log_info "[9/10] 配置Nginx..."

    # 创建Nginx配置文件
    cat > /etc/nginx/sites-available/$NGINX_SERVICE_NAME << EOF
server {
    listen 80;
    server_name ${DOMAIN:-_};

    # 安全响应头
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;
    add_header X-XSS-Protection "1; mode=block" always;
    add_header Referrer-Policy "strict-origin-when-cross-origin" always;
    add_header Permissions-Policy "geolocation=(), microphone=(), camera=()" always;

    # 日志配置
    access_log $LOG_DIR/nginx_access.log;
    error_log $LOG_DIR/nginx_error.log;

    # 静态文件缓存
    location ~* \\.(html|htm)$ {
        root $PROJECT_DIR;
        expires -1;
        add_header Cache-Control "no-store, no-cache, must-revalidate";
    }

    location ~* \\.(js|css|png|jpg|jpeg|gif|ico|svg)$ {
        root $PROJECT_DIR;
        expires 1y;
        add_header Cache-Control "public, immutable";
    }

    # 同步监控 API 代理（sync_server）
    location ^~ /api/sync/ {
        proxy_pass http://127.0.0.1:8888;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    location = /api/status {
        proxy_pass http://127.0.0.1:8888;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    location = /api/logs {
        proxy_pass http://127.0.0.1:8888;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    # 主 API 代理（udid_server）
    location /api/ {
        proxy_pass http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
        proxy_connect_timeout 60s;
        proxy_send_timeout 60s;
        proxy_read_timeout 60s;
    }

    # 主页面
    location / {
        root $PROJECT_DIR;
        try_files \$uri \$uri/ /udid_viewer.html;
    }
}
EOF

    # 启用配置
    ln -sf /etc/nginx/sites-available/$NGINX_SERVICE_NAME /etc/nginx/sites-enabled/

    # 删除默认配置
    rm -f /etc/nginx/sites-enabled/default

    # 测试配置
    nginx -t

    # 重启Nginx
    systemctl restart nginx
    systemctl enable nginx

    log_success "Nginx配置完成"
}

# 配置SSL证书
configure_ssl() {
    if [ "$USE_SSL" = true ]; then
        log_info "配置SSL证书..."

        # 申请Let's Encrypt证书
        certbot --nginx -d $DOMAIN --non-interactive --agree-tos --email admin@$DOMAIN

        # 设置自动续期
        systemctl enable certbot.timer

        log_success "SSL证书配置完成"
    fi
}

# 配置防火墙
configure_firewall() {
    log_info "[10/10] 配置防火墙..."

    if command -v ufw &> /dev/null; then
        # 配置UFW
        ufw default deny incoming
        ufw default allow outgoing
        ufw allow ssh
        ufw allow http
        ufw allow https

        # 限制8080端口仅允许本地访问
        ufw deny 8080

        # 启用防火墙（如果未启用）
        if ! ufw status | grep -q "Status: active"; then
            echo "y" | ufw enable
        fi

        log_success "UFW防火墙配置完成"
    fi

    if command -v firewall-cmd &> /dev/null; then
        # 配置Firewalld
        firewall-cmd --permanent --add-service=ssh
        firewall-cmd --permanent --add-service=http
        firewall-cmd --permanent --add-service=https
        firewall-cmd --permanent --remove-port=8080/tcp 2>/dev/null || true
        firewall-cmd --reload

        log_success "Firewalld防火墙配置完成"
    fi
}

# 配置日志轮转
configure_logrotate() {
    log_info "配置日志轮转..."

    cat > /etc/logrotate.d/$SERVICE_NAME << EOF
$LOG_DIR/*.log {
    daily
    rotate 30
    compress
    delaycompress
    missingok
    notifempty
    create 644 gaoxin gaoxin
    sharedscripts
    postrotate
        systemctl reload $SERVICE_NAME >/dev/null 2>&1 || true
    endscript
}
EOF

    log_success "日志轮转配置完成"
}

# 启动服务
start_services() {
    log_info "启动服务..."
    local SYNC_SERVICE_NAME="${SERVICE_NAME}-sync"

    systemctl start $SERVICE_NAME
    systemctl start $SYNC_SERVICE_NAME
    sleep 3

    if systemctl is-active --quiet $SERVICE_NAME && systemctl is-active --quiet $SYNC_SERVICE_NAME; then
        log_success "服务启动成功!"
        systemctl status $SERVICE_NAME --no-pager
        systemctl status $SYNC_SERVICE_NAME --no-pager
    else
        log_error "服务启动失败，请检查日志"
        journalctl -u $SERVICE_NAME -n 50 --no-pager
        journalctl -u $SYNC_SERVICE_NAME -n 50 --no-pager
        exit 1
    fi
}

# 显示部署信息
show_deployment_info() {
    echo ""
    echo "========================================"
    echo -e "${GREEN}  部署完成!${NC}"
    echo "========================================"
    echo ""
    echo "项目目录: $PROJECT_DIR"
    echo "日志目录: $LOG_DIR"
    echo "备份目录: $BACKUP_DIR"
    echo "服务名称: $SERVICE_NAME"
    echo "运行用户: gaoxin"
    echo ""
    echo "访问地址:"
    if [ "$USE_SSL" = true ]; then
        echo "  HTTPS: https://$DOMAIN"
    else
        echo "  HTTP:  http://$(curl -s ifconfig.me 2>/dev/null || echo 'your-server-ip')"
    fi
    echo ""
    echo "常用命令:"
    echo "  启动服务:   systemctl start $SERVICE_NAME"
    echo "  停止服务:   systemctl stop $SERVICE_NAME"
    echo "  重启服务:   systemctl restart $SERVICE_NAME"
    echo "  查看状态:   systemctl status $SERVICE_NAME"
    echo "  查看日志:   journalctl -u $SERVICE_NAME -f"
    echo "  Nginx日志:  tail -f $LOG_DIR/nginx_error.log"
    echo ""
    echo "安全提示:"
    echo "  - 服务以 gaoxin 用户运行，非 root 权限"
    echo "  - 防火墙已配置，仅开放 80/443 端口"
    echo "  - 配置文件权限已设置为 600"
    echo ""

    local db_backend
    db_backend=$(grep -E '^DB_BACKEND=' "$PROJECT_DIR/.env" 2>/dev/null | tail -n1 | cut -d'=' -f2 | tr '[:upper:]' '[:lower:]')
    if [ -z "$db_backend" ]; then
        db_backend="postgres"
    fi

    if [ "$db_backend" = "sqlite" ]; then
        if [ ! -f "$PROJECT_DIR/udid_hybrid_lake.db" ]; then
            log_warn "注意: SQLite 数据库文件未找到，请手动上传"
        fi
    else
        log_info "数据库后端: PostgreSQL（$db_backend）"
    fi

    if [ ! -f "$PROJECT_DIR/data/faiss_index/index.faiss" ]; then
        log_warn "注意: FAISS索引文件未找到，请手动上传"
    fi
}

# ==========================================
# 主程序
# ==========================================
main() {
    echo "========================================"
    echo "  高新医疗UDID查询系统 - 生产部署脚本"
    echo "  版本: 2.0"
    echo "========================================"
    echo ""

    parse_args "$@"
    check_root
    update_system
    install_dependencies
    create_directories
    check_required_files
    copy_project_files
    setup_python_env
    init_database
    create_service_user
    create_systemd_service
    configure_nginx
    configure_ssl
    configure_firewall
    configure_logrotate
    start_services
    show_deployment_info
}

# 执行主程序
main "$@"
