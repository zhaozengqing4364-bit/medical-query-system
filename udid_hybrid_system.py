import sqlite3
import pandas as pd
import xml.etree.ElementTree as ET
import time
import random
import os
import json
from datetime import datetime

# ==========================================
# 模块 1: 本地数据湖 (Local Data Lake)
# 职责: 处理 RSS/文件批量数据，提供高速本地查询
# ==========================================
class LocalDataLake:
    def __init__(self, db_path='udid_hybrid_lake.db'):
        self.db_path = db_path
        self.conn = None
        self._init_db()

    def _init_db(self):
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        cursor = self.conn.cursor()
        # 创建增强版产品表（支持更多字段）
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS products (
                di_code TEXT PRIMARY KEY,
                product_name TEXT,
                commercial_name TEXT,
                model TEXT,
                manufacturer TEXT,
                description TEXT,
                publish_date TEXT,
                source TEXT DEFAULT 'RSS',
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                category_code TEXT,
                social_code TEXT,
                cert_no TEXT,
                status TEXT,
                product_type TEXT,
                phone TEXT,
                email TEXT,
                scope TEXT,
                safety_info TEXT
            )
        ''')
        
        # 添加索引提升查询性能
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_product_name ON products(product_name)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_manufacturer ON products(manufacturer)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_category_code ON products(category_code)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_last_updated ON products(last_updated)')
        
        # 创建同步记录表
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sync_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_date TEXT,
                file_name TEXT,
                records_count INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS sync_run (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sync_date TEXT,
                file_name TEXT,
                status TEXT,
                error_message TEXT,
                records_count INTEGER,
                invalid_records INTEGER,
                audit_records INTEGER,
                file_checksum TEXT,
                file_size INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ingest_rejects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_name TEXT,
                di_code TEXT,
                reason TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS data_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                di_code TEXT,
                field_name TEXT,
                old_value TEXT,
                new_value TEXT,
                source TEXT,
                file_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        self.conn.commit()

    def _normalize_date(self, value: str) -> str:
        if not value:
            return ''
        value = value.strip()
        if not value:
            return ''
        if len(value) == 8 and value.isdigit():
            return f"{value[:4]}-{value[4:6]}-{value[6:]}"
        try:
            datetime.strptime(value, '%Y-%m-%d')
            return value
        except ValueError:
            return ''

    def _validate_record(self, data: dict) -> list:
        errors = []
        required_fields = ['di_code', 'product_name', 'manufacturer']
        for field in required_fields:
            if not data.get(field, '').strip():
                errors.append(f"缺少必填字段:{field}")

        if data.get('publish_date') and not self._normalize_date(data['publish_date']):
            errors.append("发布日期格式错误")

        return errors

    def log_sync_run(
        self,
        file_name: str,
        records_count: int,
        status: str,
        error_message: str = None,
        invalid_records: int = 0,
        audit_records: int = 0,
        file_checksum: str = None,
        file_size: int = None,
    ):
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO sync_run (
                sync_date, file_name, status, error_message, records_count,
                invalid_records, audit_records, file_checksum, file_size
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            datetime.now().strftime('%Y-%m-%d'),
            file_name,
            status,
            error_message,
            records_count,
            invalid_records,
            audit_records,
            file_checksum,
            file_size
        ))
        self.conn.commit()

    def ingest_xml(self, xml_file):
        """导入 RSS/XML 数据（增强版：支持更多字段，编码容错）"""
        print(f"[LocalLake] 正在从 XML 文件导入数据: {os.path.basename(xml_file)} ...")
        if not os.path.exists(xml_file):
            print(f"[LocalLake] 错误: 文件不存在 {xml_file}")
            self.log_sync_run(os.path.basename(xml_file), 0, 'failed', '文件不存在')
            return 0

        # 尝试多种编码读取文件
        content = None
        for encoding in ['utf-8', 'gbk', 'gb2312', 'gb18030', 'latin-1']:
            try:
                with open(xml_file, 'r', encoding=encoding, errors='replace') as f:
                    content = f.read()
                break
            except UnicodeDecodeError:
                continue
        
        if not content:
            print(f"[LocalLake] 无法读取文件: {xml_file}")
            self.log_sync_run(os.path.basename(xml_file), 0, 'failed', '无法读取文件')
            return 0
        
        try:
            root = ET.fromstring(content)
        except ET.ParseError as e:
            # 尝试修复常见问题
            try:
                # 移除无效字符
                import re
                content = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', content)
                root = ET.fromstring(content)
            except ET.ParseError as e2:
                print(f"[LocalLake] XML 解析错误: {e2}")
                self.log_sync_run(os.path.basename(xml_file), 0, 'failed', f"XML 解析错误: {e2}")
                return 0
            
        count = 0
        audit_count = 0
        rejected = []
        cursor = self.conn.cursor()
        file_name = os.path.basename(xml_file)
        updatable_fields = [
            'product_name', 'commercial_name', 'model', 'manufacturer', 'description',
            'publish_date', 'category_code', 'social_code', 'cert_no', 'status',
            'product_type', 'phone', 'email', 'scope', 'safety_info'
        ]
        
        device_nodes = root.findall('.//device')
        print(f"[LocalLake] 解析到 device 节点: {len(device_nodes)}")

        for device in device_nodes:
            di_code = device.findtext('zxxsdycpbs', '')
            if not di_code:
                continue
            
            # 提取版本状态（新增/变更）
            version_status = device.findtext('versionStauts', '').strip()
            
            # 提取所有字段
            data = {
                'di_code': di_code,
                'product_name': device.findtext('cpmctymc', ''),
                'commercial_name': device.findtext('spmc', ''),
                'model': device.findtext('ggxh', ''),
                'manufacturer': device.findtext('ylqxzcrbarmc', ''),
                'description': device.findtext('cpms', ''),
                'publish_date': device.findtext('cpbsfbrq', ''),
                'category_code': device.findtext('flbm', ''),
                'social_code': device.findtext('tyshxydm', ''),
                'cert_no': device.findtext('zczbhhzbapzbh', ''),
                'status': version_status,
                'product_type': device.findtext('cplb', ''),
                'phone': device.findtext('qylxrdh', ''),
                'email': device.findtext('qylxryx', ''),
                'scope': device.findtext('syfw', ''),
                'safety_info': device.findtext('cgzmraqxgxx', ''),
            }

            data['publish_date'] = self._normalize_date(data['publish_date'])
            validation_errors = self._validate_record(data)
            if validation_errors:
                rejected.append((di_code, ';'.join(validation_errors)))
                continue

            existing = cursor.execute(
                'SELECT product_name, commercial_name, model, manufacturer, description, '
                'publish_date, category_code, social_code, cert_no, status, product_type, '
                'phone, email, scope, safety_info '
                'FROM products WHERE di_code = ?',
                (di_code,)
            ).fetchone()
            
            # 根据版本状态区分处理
            is_new = version_status == '新增'
            is_change = version_status in ('变更', '纠错')
            
            if is_new and existing:
                # 新增但已存在：跳过，保护原有数据
                rejected.append((di_code, '新增记录但产品已存在'))
                continue
            
            if is_change and not existing:
                # 变更但不存在：数据异常，跳过
                rejected.append((di_code, '变更记录但产品不存在'))
                continue
            
            if existing:
                for idx, field in enumerate(updatable_fields):
                    new_value = data.get(field, '')
                    if new_value and new_value != (existing[idx] or ''):
                        cursor.execute('''
                            INSERT INTO data_audit_log (di_code, field_name, old_value, new_value, source, file_name)
                            VALUES (?, ?, ?, ?, ?, ?)
                        ''', (di_code, field, existing[idx] or '', new_value, 'RSS', file_name))
                        audit_count += 1
            
            cursor.execute('''
                INSERT INTO products 
                (di_code, product_name, commercial_name, model, manufacturer, 
                 description, publish_date, source, last_updated, category_code,
                 social_code, cert_no, status, product_type, phone, email, scope, safety_info)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'RSS', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(di_code) DO UPDATE SET
                    product_name = CASE WHEN excluded.product_name != '' THEN excluded.product_name ELSE products.product_name END,
                    commercial_name = CASE WHEN excluded.commercial_name != '' THEN excluded.commercial_name ELSE products.commercial_name END,
                    model = CASE WHEN excluded.model != '' THEN excluded.model ELSE products.model END,
                    manufacturer = CASE WHEN products.manufacturer IS NULL OR products.manufacturer = '' THEN excluded.manufacturer ELSE products.manufacturer END,
                    description = CASE WHEN excluded.description != '' THEN excluded.description ELSE products.description END,
                    publish_date = CASE WHEN excluded.publish_date != '' THEN excluded.publish_date ELSE products.publish_date END,
                    category_code = CASE WHEN excluded.category_code != '' THEN excluded.category_code ELSE products.category_code END,
                    social_code = CASE WHEN excluded.social_code != '' THEN excluded.social_code ELSE products.social_code END,
                    cert_no = CASE WHEN excluded.cert_no != '' THEN excluded.cert_no ELSE products.cert_no END,
                    status = CASE WHEN excluded.status != '' THEN excluded.status ELSE products.status END,
                    product_type = CASE WHEN excluded.product_type != '' THEN excluded.product_type ELSE products.product_type END,
                    phone = CASE WHEN excluded.phone != '' THEN excluded.phone ELSE products.phone END,
                    email = CASE WHEN excluded.email != '' THEN excluded.email ELSE products.email END,
                    scope = CASE WHEN excluded.scope != '' THEN excluded.scope ELSE products.scope END,
                    safety_info = CASE WHEN excluded.safety_info != '' THEN excluded.safety_info ELSE products.safety_info END,
                    last_updated = excluded.last_updated,
                    source = excluded.source
            ''', (
                data['di_code'],
                data['product_name'],
                data['commercial_name'],
                data['model'],
                data['manufacturer'],
                data['description'],
                data['publish_date'],
                datetime.now().isoformat(),
                data['category_code'],
                data['social_code'],
                data['cert_no'],
                data['status'],
                data['product_type'],
                data['phone'],
                data['email'],
                data['scope'],
                data['safety_info'],
            ))
            count += 1
            
        self.conn.commit()

        for di_code, reason in rejected:
            cursor.execute('''
                INSERT INTO ingest_rejects (file_name, di_code, reason)
                VALUES (?, ?, ?)
            ''', (file_name, di_code, reason))
        self.conn.commit()
        
        # 记录同步日志
        cursor.execute('''
            INSERT INTO sync_log (sync_date, file_name, records_count)
            VALUES (?, ?, ?)
        ''', (datetime.now().strftime('%Y-%m-%d'), os.path.basename(xml_file), count))
        self.conn.commit()

        self.log_sync_run(
            os.path.basename(xml_file),
            count,
            'success',
            invalid_records=len(rejected),
            audit_records=audit_count,
        )
        
        print(
            f"[LocalLake] 导入完成。新增/更新记录数: {count}, "
            f"拒绝记录: {len(rejected)}, 审计记录: {audit_count}"
        )
        if rejected:
            sample = rejected[:5]
            print(f"[LocalLake] 拒绝样例: {sample}")
        return count
    
    def get_last_sync_date(self):
        """获取最后同步日期"""
        cursor = self.conn.cursor()
        cursor.execute('SELECT MAX(sync_date) FROM sync_log')
        result = cursor.fetchone()
        return result[0] if result and result[0] else None

    def search_local(self, keyword):
        """本地高速检索 (支持多关键词空格分隔)"""
        # 简单分词: 按空格拆分，剔除过短词
        tokens = [t.strip() for t in keyword.split() if len(t.strip()) >= 2]
        if not tokens:
            tokens = [keyword.strip()]

        conditions = []
        params = []
        for token in tokens:
            like_query = f"%{token}%"
            conditions.append("(product_name LIKE ? OR manufacturer LIKE ? OR model LIKE ?)")
            params.extend([like_query, like_query, like_query])

        where_clause = " AND ".join(conditions) if conditions else "1=1"
        df = pd.read_sql_query(
            f"SELECT * FROM products WHERE {where_clause}",
            self.conn,
            params=params
        )
        return df

    def save_api_record(self, record):
        """将 API 查到的新数据缓存到本地"""
        cursor = self.conn.cursor()
        cursor.execute('''
            INSERT INTO products 
            (di_code, product_name, commercial_name, model, manufacturer, description, publish_date, source, last_updated)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'API_CACHE', ?)
            ON CONFLICT(di_code) DO UPDATE SET
                product_name = CASE WHEN excluded.product_name != '' THEN excluded.product_name ELSE products.product_name END,
                commercial_name = CASE WHEN excluded.commercial_name != '' THEN excluded.commercial_name ELSE products.commercial_name END,
                model = CASE WHEN excluded.model != '' THEN excluded.model ELSE products.model END,
                manufacturer = CASE WHEN excluded.manufacturer != '' THEN excluded.manufacturer ELSE products.manufacturer END,
                description = CASE WHEN excluded.description != '' THEN excluded.description ELSE products.description END,
                publish_date = CASE WHEN excluded.publish_date != '' THEN excluded.publish_date ELSE products.publish_date END,
                last_updated = excluded.last_updated,
                source = excluded.source
        ''', (
            record['di_code'],
            record['product_name'],
            record.get('commercial_name', ''),
            record['model'],
            record['manufacturer'],
            record.get('description', ''),
            datetime.now().strftime('%Y-%m-%d'),
            datetime.now().isoformat()
        ))
        self.conn.commit()
        print(f"[LocalLake] API 数据已缓存至本地库: {record['di_code']}")

# ==========================================
# 模块 2: 国家局 API 客户端 (Api Client)
# 职责: 模拟实时调用官方接口 (基于 API 文档)
# ==========================================
class NationalPlatformAPI:
    def __init__(self, api_key="TEST_KEY"):
        self.api_key = api_key
        self.base_url = "https://udid.nmpa.gov.cn/api/v3" # 模拟地址

    def query_realtime(self, keyword):
        """
        模拟 API 调用
        实际场景中，这里会发送 HTTP POST 请求，带上签名和 token
        """
        print(f"[OfficialAPI] 正在联网查询国家局数据库: '{keyword}' ...")
        
        # 模拟网络延迟
        time.sleep(1.5) 
        
        # 模拟 API 既然是“查漏补缺”，这里假设如果本地没查到，API 可能会返回一些其它的最新数据
        # 这里为了演示，硬编码一些“最新注册”的数据，这些数据 XML 里可能没有
        mock_new_data = [
            {
                "di_code": "06901234567890",
                "product_name": "一次性使用无菌注射针(新品)",
                "commercial_name": "极细系列",
                "model": "0.1mm x 5mm",
                "manufacturer": "高新医疗科技有限公司",
                "description": "2024年12月24日最新获批产品，采用了纳米涂层技术。",
                "status": "有效"
            }
        ]
        
        # 简单逻辑: 只有搜“高新”或“注射针”时才返回这个“新品”
        if "高新" in keyword or "注射" in keyword:
            print(f"[OfficialAPI] 🟢 成功: 查到 1 条最新实时数据 (Source: API)")
            return mock_new_data
        else:
            print(f"[OfficialAPI] 🟡 响应: 未找到更多匹配数据")
            return []

# ==========================================
# 模块 3: 智能调度核心 (Integration Core)
# 职责: 整合 RSS 和 API，决定查哪里
# ==========================================
class UDIDIntelligenceSystem:
    def __init__(self, xml_path):
        self.lake = LocalDataLake()
        self.api = NationalPlatformAPI()
        self.xml_path = xml_path
        
        # 初始化时检查是否需要加载数据
        # 实际生产中这里会检查文件哈希或日期
        self.lake.ingest_xml(self.xml_path)

    def search(self, keyword, force_refresh=False):
        print(f"\n{'='*20} 开始搜索: {keyword} {'='*20}")
        results = []
        
        # 1. 先查本地 (速度快，0成本)
        print(f"[System] Step 1: 检索本地数据库 (RSS Lake)...")
        df_local = self.lake.search_local(keyword)
        
        if not df_local.empty:
            print(f"[System] ✅ 本地命中: {len(df_local)} 条记录")
            # 将 DataFrame 转为字典列表
            results.extend(df_local.to_dict('records'))
        else:
            print(f"[System] ⚠️ 本地未命中")

        # 2. 决策是否查 API
        # 策略: 如果强制刷新，或者本地没查到，或者关键词包含特定敏感词(如'最新')，则查 API
        should_call_api = force_refresh or df_local.empty or "最新" in keyword
        
        if should_call_api:
            print(f"[System] Step 2: 触发实时联网查询 (API)...")
            api_results = self.api.query_realtime(keyword)
            
            if api_results:
                # 3. 将 API 结果回写到本地 (缓存机制)
                for item in api_results:
                    # 检查是否已存在(避免重复显示，虽然 SQL 有去重，但在展示层也要处理)
                    is_duplicate = any(r['di_code'] == item['di_code'] for r in results)
                    if not is_duplicate:
                        item['source'] = 'API_REALTIME' # 标记来源用于前端高亮
                        results.append(item)
                        # 异步入库
                        self.lake.save_api_record(item)
        else:
            print(f"[System] Step 2: 跳过 API (本地数据已足够，且未要求刷新)")

        # 4. 汇总展示
        self._display_results(results)
        
    def _display_results(self, results):
        if not results:
            print("\n[结果] 未找到任何匹配产品。")
            return
            
        print(f"\n[结果] 共找到 {len(results)} 条产品信息:")
        print("-" * 80)
        # 简单格式化输出
        print(f"{'来源':<12} | {'DI编码':<16} | {'产品名称':<20} | {'规格型号'}")
        print("-" * 80)
        for r in results:
            source_label = r.get('source', 'UNKNOWN')
            # 简单的截断处理
            p_name = (r['product_name'][:18] + '..') if len(r['product_name']) > 20 else r['product_name']
            print(f"{source_label:<12} | {r['di_code']:<16} | {p_name:<20} | {r['model']}")
        print("-" * 80)

# ==========================================
# 模拟运行
# ==========================================
if __name__ == "__main__":
    # 配置
    XML_FILE = '/Users/zhaozengqing/github/AI/test/高新医疗/UDID_INCREMENTAL_DOWNLOAD_PART1_Of_1_2025-12-22.xml'
    
    # 启动系统
    system = UDIDIntelligenceSystem(XML_FILE)
    
    # 场景 1: 查一个本地有的 (普通查询)
    # 假设 XML 里有某款产品 (这里用脚本里之前看到的模拟数据，实际上会查 XML 内容)
    # 我们先看看 XML 解析结果，脚本会自动加载
    
    # 暂停一下方便看日志
    time.sleep(1)
    
    # 场景 2: 查一个本地没有的，或者明确要找“最新”的 (触发 API)
    system.search("高新医疗最新注射针") 
    
    # 场景 3: 再次查同一个词 (演示 API 缓存回填机制)
    # 讲道理，上面的 Step 3 会把 API 数据存入 LocalDB (source=API_CACHE)
    # 这次查应该直接从 LocalDB 出结果，不需要调 API (除非 force_refresh=True)
    print("\n\n>>> 模拟: 10分钟后，业务员再次查询同一产品 (验证缓存) <<<")
    system.search("高新医疗最新注射针")
