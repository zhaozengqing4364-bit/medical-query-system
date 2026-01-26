#!/bin/bash
echo "========================================"
echo "  UDID 医疗器械智能查询系统"
echo "========================================"
echo ""
echo "正在启动服务..."
echo ""

cd "$(dirname "$0")"

if [ ! -f "venv/bin/activate" ]; then
    echo "[错误] 虚拟环境不存在！"
    echo "请先运行以下命令创建虚拟环境："
    echo "  python3 -m venv venv"
    echo "  source venv/bin/activate"
    echo "  pip install -r requirements.txt"
    exit 1
fi

source venv/bin/activate

echo "虚拟环境已激活"
echo "服务地址: http://localhost:8080"
echo "按 Ctrl+C 停止服务"
echo ""

python udid_server.py
