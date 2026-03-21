#!/bin/bash
# UDID 同步服务器启动脚本
# 强制使用 8888 端口

echo "=========================================="
echo "UDID 同步服务器启动脚本"
echo "=========================================="

# 检查并处理占用 8888 端口的进程
echo "[1/3] 检查 8888 端口占用..."
PID=$(lsof -ti:8888 2>/dev/null)
if [ -n "$PID" ]; then
    echo "      发现占用进程 PID: $PID"
    for single_pid in $PID; do
        PROC_CMD=$(ps -p "$single_pid" -o command= 2>/dev/null || echo "")
        if echo "$PROC_CMD" | grep -q "sync_server.py"; then
            echo "      尝试优雅停止 sync_server 进程: $single_pid"
            kill "$single_pid" 2>/dev/null || true
            for _ in 1 2 3 4 5; do
                if ! kill -0 "$single_pid" 2>/dev/null; then
                    break
                fi
                sleep 1
            done
            if kill -0 "$single_pid" 2>/dev/null; then
                echo "      ✗ sync_server 进程未在预期时间内退出，拒绝执行 SIGKILL"
                echo "        PID: $single_pid"
                echo "        请先手动排查并终止该进程后再重试"
                exit 1
            fi
        else
            echo "      ✗ 端口 8888 被非 sync_server 进程占用，拒绝强制终止"
            echo "        PID: $single_pid"
            echo "        CMD: $PROC_CMD"
            exit 1
        fi
    done
    echo "      ✓ 已处理端口占用"
else
    echo "      ✓ 端口 8888 未被占用"
fi

# 检查 Python 环境
echo "[2/3] 检查 Python 环境..."
if command -v python3 &> /dev/null; then
    PYTHON_CMD="python3"
elif command -v python &> /dev/null; then
    PYTHON_CMD="python"
else
    echo "      ✗ 错误: 未找到 Python，请先安装 Python 3"
    exit 1
fi
echo "      ✓ 使用 Python: $PYTHON_CMD"

# 检查 sync_server.py 是否存在
echo "[3/3] 检查服务器文件..."
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SERVER_FILE="$SCRIPT_DIR/sync_server.py"
if [ ! -f "$SERVER_FILE" ]; then
    echo "      ✗ 错误: 未找到 sync_server.py"
    exit 1
fi
echo "      ✓ 找到服务器文件"

echo ""
echo "=========================================="
echo "启动同步服务器..."
echo "访问地址: http://localhost:8888"
echo "API 状态: http://localhost:8888/api/status"
echo "按 Ctrl+C 停止服务器"
echo "=========================================="
echo ""

# 启动服务器
cd "$SCRIPT_DIR"
$PYTHON_CMD sync_server.py
