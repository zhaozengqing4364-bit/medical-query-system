#!/bin/bash
# ============================================
# UDID 系统打包脚本 (macOS/Linux)
# ============================================

set -e

echo "========================================"
echo "  UDID 医疗器械系统 - 打包工具"
echo "========================================"
echo ""

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

# 打包输出目录
OUTPUT_DIR="$(dirname "$SCRIPT_DIR")"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
PACKAGE_NAME="UDID_System_${TIMESTAMP}"
PACKAGE_PATH="${OUTPUT_DIR}/${PACKAGE_NAME}"

echo "源目录: $SCRIPT_DIR"
echo "输出目录: $OUTPUT_DIR"
echo "包名: $PACKAGE_NAME"
echo ""

# 检查数据库文件
if [ ! -f "udid_hybrid_lake.db" ]; then
    echo "[警告] 数据库文件 udid_hybrid_lake.db 不存在！"
    echo "是否继续? (y/n)"
    read -r response
    if [ "$response" != "y" ]; then
        exit 1
    fi
fi

# 创建临时目录
echo "正在创建打包目录..."
mkdir -p "$PACKAGE_PATH"

# 复制必要文件
echo "正在复制文件..."

# 核心 Python 文件
cp udid_server.py "$PACKAGE_PATH/"
cp udid_hybrid_system.py "$PACKAGE_PATH/"
cp udid_sync.py "$PACKAGE_PATH/"
cp ai_service.py "$PACKAGE_PATH/"
cp embedding_service.py "$PACKAGE_PATH/"
cp embedding_faiss.py "$PACKAGE_PATH/"
cp embedding_batch.py "$PACKAGE_PATH/"

# 前端文件
cp udid_viewer.html "$PACKAGE_PATH/"
cp login.html "$PACKAGE_PATH/"
cp admin.html "$PACKAGE_PATH/"

# 配置和依赖
cp requirements.txt "$PACKAGE_PATH/"

# 启动脚本
cp start.bat "$PACKAGE_PATH/"
cp start.sh "$PACKAGE_PATH/"
chmod +x "$PACKAGE_PATH/start.sh"

# 文档
cp README.md "$PACKAGE_PATH/" 2>/dev/null || true
cp DEPLOYMENT_GUIDE.md "$PACKAGE_PATH/" 2>/dev/null || true
cp SYSTEM_ARCHITECTURE.md "$PACKAGE_PATH/" 2>/dev/null || true

# 创建空的 config.json 模板
cat > "$PACKAGE_PATH/config.json" << 'EOF'
{
  "api_base_url": "https://api.example.com/v1",
  "api_key": "your-api-key-here",
  "model": "gpt-4",
  "embedding_api_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
  "embedding_api_key": "your-embedding-api-key-here",
  "embedding_model": "text-embedding-v4"
}
EOF

# 创建 data 目录
mkdir -p "$PACKAGE_PATH/data/faiss_index"
mkdir -p "$PACKAGE_PATH/data/embedding_batch"

# 复制数据库（如果存在）
if [ -f "udid_hybrid_lake.db" ]; then
    echo "正在复制数据库文件 (可能需要几分钟)..."
    cp udid_hybrid_lake.db "$PACKAGE_PATH/"
fi

# 复制 FAISS 索引（性能模式 B 需要 index.faiss + id_map.pkl）
FAISS_INDEX_FILE="data/faiss_index/index.faiss"
FAISS_MAP_FILE="data/faiss_index/id_map.pkl"

if [ -f "$FAISS_INDEX_FILE" ] && [ -f "$FAISS_MAP_FILE" ]; then
    echo "正在复制 FAISS 索引..."
    cp -r data/faiss_index/* "$PACKAGE_PATH/data/faiss_index/"
else
    echo "[重要提醒] 未检测到完整的 FAISS 索引文件："
    echo "  - $FAISS_INDEX_FILE"
    echo "  - $FAISS_MAP_FILE"
    echo "若客户需要极致性能（模式 B），请先在本机生成索引后再打包："
    echo "  source venv/bin/activate"
    echo "  python embedding_faiss.py --build"
    echo "（生成后会写入 data/faiss_index/）"
fi

# 复制批量处理状态（可选）
if [ -f "data/embedding_batch/pipeline_state.json" ]; then
    cp data/embedding_batch/pipeline_state.json "$PACKAGE_PATH/data/embedding_batch/"
fi

echo ""
echo "========================================"
echo "打包完成！"
echo "========================================"
echo ""
echo "打包目录: $PACKAGE_PATH"
echo ""

# 计算大小
TOTAL_SIZE=$(du -sh "$PACKAGE_PATH" | cut -f1)
echo "总大小: $TOTAL_SIZE"
echo ""

# 列出文件
echo "包含文件:"
ls -lh "$PACKAGE_PATH"
echo ""

# 提示压缩
echo "如需压缩，请运行："
echo "  cd \"$OUTPUT_DIR\""
echo "  tar -czvf ${PACKAGE_NAME}.tar.gz ${PACKAGE_NAME}"
echo ""
echo "或使用 zip："
echo "  zip -r ${PACKAGE_NAME}.zip ${PACKAGE_NAME}"
