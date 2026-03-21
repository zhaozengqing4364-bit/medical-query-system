#!/bin/bash
# UDID 监控服务器启动脚本

cd "$(dirname "$0")"

echo "启动 UDID 同步监控服务器..."
echo "访问地址: http://localhost:8888"
echo "按 Ctrl+C 停止"
echo ""

python3 sync_server.py
