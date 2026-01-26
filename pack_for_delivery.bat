@echo off
chcp 65001 >nul
REM ============================================
REM UDID 系统打包脚本 (Windows)
REM ============================================

echo ========================================
echo   UDID 医疗器械系统 - 打包工具
echo ========================================
echo.

set SCRIPT_DIR=%~dp0
set TIMESTAMP=%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%
set TIMESTAMP=%TIMESTAMP: =0%
set PACKAGE_NAME=UDID_System_%TIMESTAMP%
set OUTPUT_DIR=%SCRIPT_DIR%..
set PACKAGE_PATH=%OUTPUT_DIR%\%PACKAGE_NAME%

echo 源目录: %SCRIPT_DIR%
echo 输出目录: %OUTPUT_DIR%
echo 包名: %PACKAGE_NAME%
echo.

REM 检查数据库文件
if not exist "%SCRIPT_DIR%udid_hybrid_lake.db" (
    echo [警告] 数据库文件 udid_hybrid_lake.db 不存在！
    echo 是否继续? (Y/N)
    set /p response=
    if /i not "%response%"=="Y" exit /b 1
)

REM 创建打包目录
echo 正在创建打包目录...
mkdir "%PACKAGE_PATH%"
mkdir "%PACKAGE_PATH%\data\faiss_index"
mkdir "%PACKAGE_PATH%\data\embedding_batch"

REM 复制核心 Python 文件
echo 正在复制 Python 文件...
copy "%SCRIPT_DIR%udid_server.py" "%PACKAGE_PATH%\"
copy "%SCRIPT_DIR%udid_hybrid_system.py" "%PACKAGE_PATH%\"
copy "%SCRIPT_DIR%udid_sync.py" "%PACKAGE_PATH%\"
copy "%SCRIPT_DIR%ai_service.py" "%PACKAGE_PATH%\"
copy "%SCRIPT_DIR%embedding_service.py" "%PACKAGE_PATH%\"
copy "%SCRIPT_DIR%embedding_faiss.py" "%PACKAGE_PATH%\"
copy "%SCRIPT_DIR%embedding_batch.py" "%PACKAGE_PATH%\"

REM 复制前端文件
echo 正在复制前端文件...
copy "%SCRIPT_DIR%udid_viewer.html" "%PACKAGE_PATH%\"
copy "%SCRIPT_DIR%login.html" "%PACKAGE_PATH%\"
copy "%SCRIPT_DIR%admin.html" "%PACKAGE_PATH%\"

REM 复制配置和依赖
copy "%SCRIPT_DIR%requirements.txt" "%PACKAGE_PATH%\"

REM 复制启动脚本
copy "%SCRIPT_DIR%start.bat" "%PACKAGE_PATH%\"
copy "%SCRIPT_DIR%start.sh" "%PACKAGE_PATH%\"

REM 复制文档
copy "%SCRIPT_DIR%README.md" "%PACKAGE_PATH%\" 2>nul
copy "%SCRIPT_DIR%DEPLOYMENT_GUIDE.md" "%PACKAGE_PATH%\" 2>nul
copy "%SCRIPT_DIR%SYSTEM_ARCHITECTURE.md" "%PACKAGE_PATH%\" 2>nul

REM 创建 config.json 模板
echo 正在创建配置模板...
(
echo {
echo   "api_base_url": "https://api.example.com/v1",
echo   "api_key": "your-api-key-here",
echo   "model": "gpt-4",
echo   "embedding_api_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
echo   "embedding_api_key": "your-embedding-api-key-here",
echo   "embedding_model": "text-embedding-v4"
echo }
) > "%PACKAGE_PATH%\config.json"

REM 复制数据库
if exist "%SCRIPT_DIR%udid_hybrid_lake.db" (
    echo 正在复制数据库文件 (可能需要几分钟)...
    copy "%SCRIPT_DIR%udid_hybrid_lake.db" "%PACKAGE_PATH%\"
)

REM 复制 FAISS 索引（性能模式 B 需要 index.faiss + id_map.pkl）
if exist "%SCRIPT_DIR%data\faiss_index\index.faiss" (
    if exist "%SCRIPT_DIR%data\faiss_index\id_map.pkl" (
        echo 正在复制 FAISS 索引...
        xcopy /E /I "%SCRIPT_DIR%data\faiss_index" "%PACKAGE_PATH%\data\faiss_index"
    ) else (
        echo [重要提醒] 缺少 FAISS 映射文件: data\faiss_index\id_map.pkl
        echo 若客户需要极致性能（模式 B），请先生成索引后再打包：
        echo   venv\Scripts\activate
        echo   python embedding_faiss.py --build
    )
) else (
    echo [重要提醒] 缺少 FAISS 索引文件: data\faiss_index\index.faiss
    echo 若客户需要极致性能（模式 B），请先生成索引后再打包：
    echo   venv\Scripts\activate
    echo   python embedding_faiss.py --build
)

echo.
echo ========================================
echo 打包完成！
echo ========================================
echo.
echo 打包目录: %PACKAGE_PATH%
echo.

REM 显示文件列表
echo 包含文件:
dir "%PACKAGE_PATH%"
echo.

echo 如需压缩，请使用 7-Zip 或 WinRAR 压缩该目录。
echo.

pause
