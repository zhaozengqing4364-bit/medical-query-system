#!/bin/bash

set -e

echo "========================================"
echo "  UDID 医疗器械智能查询系统"
echo "========================================"
echo ""
echo "正在启动服务..."
echo ""

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

if ! command -v lsof >/dev/null 2>&1; then
    echo "[错误] 缺少 lsof，无法自动清理端口占用。"
    echo "请先安装 lsof 后重试。"
    exit 1
fi

read_port_from_env() {
    local key="$1"
    local default_port="$2"
    local value=""

    if [ -n "${!key:-}" ]; then
        value="${!key}"
    elif [ -f ".env" ]; then
        value="$(grep -E "^${key}=" .env 2>/dev/null | tail -n1 | cut -d'=' -f2- || true)"
    fi

    value="$(echo "${value}" | tr -d '[:space:]')"
    if [[ "$value" =~ ^[0-9]+$ ]]; then
        echo "$value"
    else
        echo "$default_port"
    fi
}

kill_port_processes() {
    local port="$1"
    local pids
    local pid
    local cmd
    local survivors

    pids="$(lsof -tiTCP:${port} -sTCP:LISTEN 2>/dev/null || true)"
    if [ -z "$pids" ]; then
        echo "  ✓ 端口 ${port} 未被占用"
        return 0
    fi

    echo "  ! 端口 ${port} 被占用，开始清理: $pids"
    for pid in $pids; do
        if [ "$pid" = "$$" ]; then
            continue
        fi
        cmd="$(ps -p "$pid" -o command= 2>/dev/null || echo "")"
        echo "    - 尝试优雅终止 PID ${pid}: ${cmd}"
        kill "$pid" 2>/dev/null || true
    done

    for _ in 1 2 3 4 5; do
        survivors=""
        for pid in $pids; do
            if kill -0 "$pid" 2>/dev/null; then
                survivors="${survivors} ${pid}"
            fi
        done
        if [ -z "$survivors" ]; then
            echo "  ✓ 端口 ${port} 已释放"
            return 0
        fi
        sleep 1
    done

    echo "  ! 仍有进程占用端口 ${port}，执行强制终止: ${survivors}"
    for pid in $survivors; do
        kill -9 "$pid" 2>/dev/null || true
    done
    sleep 1

    for pid in $survivors; do
        if kill -0 "$pid" 2>/dev/null; then
            echo "  ✗ 无法终止 PID ${pid}，启动中止"
            exit 1
        fi
    done

    echo "  ✓ 端口 ${port} 已强制释放"
}

APP_PORT="$(read_port_from_env "PORT" "8080")"
SYNC_PORT="$(read_port_from_env "SYNC_PORT" "8888")"

is_weak_pg_password() {
    local v
    v="$(echo "${1:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    case "$v" in
        ""|"your_secure_password"|"password"|"123456"|"admin")
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

is_weak_sync_api_key() {
    local v
    v="$(echo "${1:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
    if [ "${#v}" -lt 24 ]; then
        return 0
    fi
    case "$v" in
        ""|"change-me"|"your_sync_api_key_here"|"your_sync_api_key_here_change_in_production"|"your_random_32char_string_here"|"sync_api_key")
            return 0
            ;;
        *)
            return 1
            ;;
    esac
}

echo "[1/4] 清理占用端口..."
kill_port_processes "$APP_PORT"
if [ "$SYNC_PORT" != "$APP_PORT" ]; then
    kill_port_processes "$SYNC_PORT"
fi
echo ""

echo "[2/4] 检查 Python 虚拟环境..."
if [ ! -f "venv/bin/activate" ]; then
    echo "[错误] 虚拟环境不存在！"
    echo "请先运行以下命令创建虚拟环境："
    echo "  python3 -m venv venv"
    echo "  source venv/bin/activate"
    echo "  pip install -r requirements.txt"
    exit 1
fi
source venv/bin/activate
echo "  ✓ 虚拟环境已激活"
echo ""

echo "[3/4] 加载环境变量..."
if [ -f ".env" ]; then
    set -a
    # shellcheck disable=SC1091
    source ".env"
    set +a
    echo "  ✓ 已加载 .env"
else
    echo "  ! 未找到 .env，将使用系统环境变量"
fi

db_backend="$(echo "${DB_BACKEND:-postgres}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"
if [ "$db_backend" = "postgres" ]; then
    if is_weak_pg_password "${POSTGRES_PASSWORD:-}"; then
        echo "  ✗ POSTGRES_PASSWORD 未设置或为弱值，拒绝启动。"
        echo "    请在 .env 中设置强密码（长度>=16，且混合字符）。"
        exit 1
    fi
fi
echo "  ✓ 数据库后端: ${db_backend}"
echo ""

SYNC_PID=""
cleanup() {
    if [ -n "${SYNC_PID}" ] && kill -0 "${SYNC_PID}" 2>/dev/null; then
        echo ""
        echo "[退出] 正在停止同步服务 (PID ${SYNC_PID})..."
        kill "${SYNC_PID}" 2>/dev/null || true
        wait "${SYNC_PID}" 2>/dev/null || true
        echo "[退出] 同步服务已停止"
    fi
}
trap cleanup EXIT INT TERM

echo "[4/5] 启动同步监控服务..."
if is_weak_sync_api_key "${SYNC_API_KEY:-}"; then
    echo "  ✗ SYNC_API_KEY 未设置或过弱，拒绝启动。"
    echo "    请在 .env 中设置长度>=24的随机强密钥。"
    exit 1
fi
mkdir -p data
SYNC_LOG="data/sync_server.log"
python sync_server.py >"${SYNC_LOG}" 2>&1 &
SYNC_PID=$!
sleep 2
if ! kill -0 "${SYNC_PID}" 2>/dev/null; then
    echo "  ✗ 同步监控服务启动失败，请检查 ${SYNC_LOG}"
    tail -n 40 "${SYNC_LOG}" || true
    exit 1
fi
echo "  ✓ 同步监控服务已启动: http://localhost:${SYNC_PORT}"
echo ""

echo "[5/5] 启动主服务..."
echo "主服务地址: http://localhost:${APP_PORT}"
echo "同步监控:   http://localhost:${SYNC_PORT}"
echo "按 Ctrl+C 停止全部服务"
echo ""

python udid_server.py
