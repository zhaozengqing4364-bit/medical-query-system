"""
UDID 数据同步脚本
================

从国家药监局官网自动下载最新的 XML 数据并导入数据库。
支持智能补全：检测遗漏的日期，自动批量下载。

数据源：
- 每日更新：https://udi.nmpa.gov.cn/rss/download.html?files=daily
- 每周更新：https://udi.nmpa.gov.cn/rss/download.html?files=weekly
- 每月更新：https://udi.nmpa.gov.cn/rss/download.html?files=monthly
- 全量数据：https://udi.nmpa.gov.cn/rss/download.html?files=full

版本: 1.0.0
"""

import os
import re
import zipfile
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from typing import List, Dict, Optional
import hashlib
import ssl
import warnings
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# 导入本地数据湖模块
from udid_hybrid_system import LocalDataLake

# ==========================================
# SSL 兼容性适配器（解决 udi.nmpa.gov.cn TLS 问题）
# ==========================================

class _SSLFallbackAdapter(HTTPAdapter):
    """自定义 SSL 适配器，解决部分政府网站 TLS 握手失败的问题"""
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context()
        # 降低安全级别以兼容旧版 TLS 配置
        ctx.set_ciphers('DEFAULT:@SECLEVEL=1')
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        # 支持 TLSv1.2（部分政府站点不支持 TLSv1.3）
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)

def _create_session() -> requests.Session:
    """创建带 SSL 容错的 requests Session"""
    session = requests.Session()
    session.mount('https://', _SSLFallbackAdapter())
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    })
    return session

# 全局 Session 实例（复用连接）
_session = None

def _get_session() -> requests.Session:
    global _session
    if _session is None:
        _session = _create_session()
    return _session

# ==========================================
# 配置常量
# ==========================================

RSS_URLS = {
    'daily': 'https://udi.nmpa.gov.cn/rss/download.html?files=daily',
    'weekly': 'https://udi.nmpa.gov.cn/rss/download.html?files=weekly',
    'monthly': 'https://udi.nmpa.gov.cn/rss/download.html?files=monthly',
    'full': 'https://udi.nmpa.gov.cn/rss/download.html?files=full',
}

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
DB_PATH = os.path.join(os.path.dirname(__file__), 'udid_hybrid_lake.db')

# 请求超时设置
REQUEST_TIMEOUT = 60
REQUEST_RETRIES = 3
REQUEST_BACKOFF = 2

# ==========================================
# RSS 解析函数
# ==========================================

def _request_with_retry(url: str, stream: bool = False, headers: Optional[Dict[str, str]] = None) -> requests.Response:
    session = _get_session()
    last_error = None
    for attempt in range(1, REQUEST_RETRIES + 1):
        try:
            response = session.get(url, timeout=REQUEST_TIMEOUT, stream=stream, headers=headers or {}, verify=False)
            response.raise_for_status()
            return response
        except requests.RequestException as e:
            last_error = e
            print(f"[Sync] 请求失败 (第 {attempt}/{REQUEST_RETRIES} 次): {e}")
            if attempt < REQUEST_RETRIES:
                time.sleep(REQUEST_BACKOFF * attempt)
                # SSL 失败时重建 Session
                if 'SSL' in str(e) or 'EOF' in str(e):
                    global _session
                    _session = None
                    session = _get_session()
    raise last_error


def _extract_date_yyyymmdd(text: str) -> str:
    if not text:
        return ''
    date_match = re.search(r'(\d{8})', text)
    return date_match.group(1) if date_match else ''


def fetch_rss_feed(feed_type: str = 'daily') -> (List[Dict], Optional[str]):
    """
    解析官网 RSS 获取可用文件列表
    
    Args:
        feed_type: 'daily', 'weekly', 'monthly', 'full'
    
    Returns:
        [{
            'title': 'UDID_DAY_UPDATE_20260107.zip',
            'date': '20260107',
            'link': 'https://...',
            'description': '...',
            'count': 3601
        }, ...]
    """
    url = RSS_URLS.get(feed_type, RSS_URLS['daily'])
    print(f"[Sync] 正在获取 RSS 源: {feed_type} ...")
    
    try:
        response = _request_with_retry(url)
        content = response.content
        content_type = response.headers.get('Content-Type', '')
        print(f"[Sync] 响应状态: {response.status_code}, Content-Type: {content_type}, 大小: {len(content)} bytes")
        
        items = []
        
        # 尝试方式1: 标准 RSS XML 解析
        try:
            root = ET.fromstring(content)
            for item in root.findall('.//item'):
                title = item.findtext('title', '')
                link = item.findtext('link', '')
                description = item.findtext('description', '')
                date_str = _extract_date_yyyymmdd(title)
                
                count_match = re.search(r'包含(\d+)产品标识数量', description)
                count = int(count_match.group(1)) if count_match else 0
                
                items.append({
                    'title': title,
                    'date': date_str,
                    'link': link,
                    'description': description,
                    'count': count
                })
        except ET.ParseError:
            print(f"[Sync] 非标准 XML，尝试 HTML 正则解析...")
        
        # 尝试方式2: HTML/文本正则解析（降级方案）
        if not items:
            text = content.decode('utf-8', errors='replace')
            # 匹配常见的下载链接模式
            zip_pattern = re.findall(
                r'href=["\']([^"\']*?(?:UDID[^"\']*?\.zip))["\']',
                text, re.IGNORECASE
            )
            for href in zip_pattern:
                # 补全相对路径
                if href.startswith('/'):
                    href = f"https://udi.nmpa.gov.cn{href}"
                elif not href.startswith('http'):
                    href = f"https://udi.nmpa.gov.cn/{href}"
                
                filename = href.split('/')[-1].split('?')[0]
                date_str = _extract_date_yyyymmdd(filename)
                
                items.append({
                    'title': filename,
                    'date': date_str,
                    'link': href,
                    'description': '',
                    'count': 0
                })
            
            if items:
                print(f"[Sync] 通过 HTML 解析获取到 {len(items)} 个下载链接")
        
        if not items:
            print(f"[Sync] 警告: 未从响应中解析到任何文件，响应前200字符: {content[:200]}")
        
        print(f"[Sync] 获取到 {len(items)} 个可下载文件")
        return items, None
        
    except requests.RequestException as e:
        error_msg = f"RSS 获取失败: {e}"
        print(f"[Sync] {error_msg}")
        return [], error_msg
    except ET.ParseError as e:
        error_msg = f"RSS 解析失败: {e}"
        print(f"[Sync] {error_msg}")
        return [], error_msg

# ==========================================
# 下载函数
# ==========================================

def _calculate_md5(file_path: str) -> str:
    md5 = hashlib.md5()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            md5.update(chunk)
    return md5.hexdigest()


def download_zip(url: str, filename: str) -> Optional[Dict[str, str]]:
    """
    下载 ZIP 文件到 data/ 目录
    
    Args:
        url: 下载链接
        filename: 文件名
    
    Returns:
        {'path': 下载文件路径, 'checksum': md5, 'size': bytes}，失败返回 None
    """
    # 确保目录存在
    os.makedirs(DATA_DIR, exist_ok=True)
    
    filepath = os.path.join(DATA_DIR, filename)
    temp_path = f"{filepath}.part"
    
    # 如果文件已存在，跳过下载
    if os.path.exists(filepath):
        checksum = _calculate_md5(filepath)
        file_size = os.path.getsize(filepath)
        print(f"[Sync] 文件已存在，跳过: {filename}")
        return {'path': filepath, 'checksum': checksum, 'size': file_size}
    
    print(f"[Sync] 正在下载: {filename} ...")
    
    try:
        headers = {}
        resume_bytes = 0
        if os.path.exists(temp_path):
            resume_bytes = os.path.getsize(temp_path)
            if resume_bytes > 0:
                headers['Range'] = f"bytes={resume_bytes}-"
                print(f"[Sync] 发现断点，尝试续传: {resume_bytes} bytes")

        response = _request_with_retry(url, stream=True, headers=headers)

        if response.status_code not in (200, 206):
            print(f"[Sync] 下载失败，响应码异常: {response.status_code}")
            return None

        # 服务器忽略 Range 请求返回 200 时，必须从头重下，避免文件拼接损坏。
        if resume_bytes > 0 and response.status_code == 200:
            print("[Sync] 服务器未支持断点续传，改为全量重下")
            resume_bytes = 0

        total_size = response.headers.get('Content-Length')
        if total_size:
            total_size = int(total_size) + resume_bytes

        mode = 'ab' if resume_bytes > 0 else 'wb'
        with open(temp_path, mode) as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)

        if total_size and os.path.getsize(temp_path) != total_size:
            print(f"[Sync] 下载不完整，已保存断点文件: {temp_path}")
            return None

        os.replace(temp_path, filepath)
        file_size = os.path.getsize(filepath)
        checksum = _calculate_md5(filepath)
        print(f"[Sync] 下载完成: {filename} ({file_size / 1024 / 1024:.2f} MB)")
        return {'path': filepath, 'checksum': checksum, 'size': file_size}

    except requests.RequestException as e:
        print(f"[Sync] 下载失败: {e}")
        return None

# ==========================================
# 解压和导入函数
# ==========================================

def extract_and_import(zip_path: str, data_lake: LocalDataLake, file_meta: Dict[str, str] = None) -> int:
    """
    解压 ZIP 并导入 XML 到数据库
    
    Args:
        zip_path: ZIP 文件路径
        data_lake: 数据湖实例
    
    Returns:
        导入的记录数
    """
    if not os.path.exists(zip_path):
        print(f"[Sync] ZIP 文件不存在: {zip_path}")
        data_lake.log_sync_run(os.path.basename(zip_path), 0, 'failed', 'ZIP 文件不存在')
        return 0
    
    total_count = 0
    
    try:
        with tempfile.TemporaryDirectory() as tmp_dir:
            # 解压 ZIP
            print(f"[Sync] 正在解压: {os.path.basename(zip_path)} ...")
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(tmp_dir)
            
            def _collect_xml_files(search_dir: str) -> list:
                xml_list = []
                for root, dirs, files in os.walk(search_dir):
                    for file in files:
                        if file.lower().endswith('.xml'):
                            xml_list.append(os.path.join(root, file))
                return xml_list

            # 查找所有 XML 文件
            xml_files = _collect_xml_files(tmp_dir)
            print(f"[Sync] 发现 {len(xml_files)} 个 XML 文件")

            # 如果没有 XML，检查是否存在嵌套 zip
            if not xml_files:
                nested_zips = []
                for root, dirs, files in os.walk(tmp_dir):
                    for file in files:
                        if file.lower().endswith('.zip'):
                            nested_zips.append(os.path.join(root, file))

                if nested_zips:
                    print(f"[Sync] 发现 {len(nested_zips)} 个嵌套压缩包，尝试解压")
                    for nested in nested_zips:
                        try:
                            with zipfile.ZipFile(nested, 'r') as nested_zip:
                                nested_zip.extractall(tmp_dir)
                        except zipfile.BadZipFile as e:
                            print(f"[Sync] 嵌套 ZIP 损坏: {os.path.basename(nested)} {e}")

                    xml_files = _collect_xml_files(tmp_dir)
                    print(f"[Sync] 嵌套解压后 XML 数量: {len(xml_files)}")

            if not xml_files:
                try:
                    with zipfile.ZipFile(zip_path, 'r') as zf:
                        sample_files = zf.namelist()[:10]
                        print(f"[Sync] ZIP 内文件示例: {sample_files}")
                except Exception:
                    pass

                data_lake.log_sync_run(
                    os.path.basename(zip_path),
                    0,
                    'failed',
                    '未发现 XML 文件',
                    file_checksum=(file_meta or {}).get('checksum'),
                    file_size=(file_meta or {}).get('size')
                )
                return 0
            
            # 导入每个 XML 文件
            for xml_path in xml_files:
                result = data_lake.ingest_xml(xml_path)
                # ingest_xml 返回 dict，提取 total 字段
                if isinstance(result, dict):
                    count = result.get('total', 0)
                else:
                    count = result if isinstance(result, int) else 0
                print(f"[Sync] 导入 {os.path.basename(xml_path)}: {count} 条")
                total_count += count
    
    except zipfile.BadZipFile as e:
        print(f"[Sync] ZIP 文件损坏: {e}")
        data_lake.log_sync_run(
            os.path.basename(zip_path),
            0,
            'failed',
            f"ZIP 文件损坏: {e}",
            file_checksum=(file_meta or {}).get('checksum'),
            file_size=(file_meta or {}).get('size')
        )
    except Exception as e:
        print(f"[Sync] 解压/导入失败: {e}")
        data_lake.log_sync_run(
            os.path.basename(zip_path),
            0,
            'failed',
            f"解压/导入失败: {e}",
            file_checksum=(file_meta or {}).get('checksum'),
            file_size=(file_meta or {}).get('size')
        )
    
    if total_count == 0:
        data_lake.log_sync_run(
            os.path.basename(zip_path),
            0,
            'failed',
            '导入结果为 0，可能文件为空或解析失败',
            file_checksum=(file_meta or {}).get('checksum'),
            file_size=(file_meta or {}).get('size')
        )

    return total_count

# ==========================================
# 智能同步函数
# ==========================================

def get_missing_dates(last_sync_date: Optional[str], today: str) -> List[str]:
    """
    计算需要补全的日期列表
    
    Args:
        last_sync_date: 上次同步日期 (YYYY-MM-DD)
        today: 今天日期 (YYYY-MM-DD)
    
    Returns:
        需要下载的日期列表 ['20260106', '20260107', ...]
    """
    if not last_sync_date:
        return []  # 首次使用需要手动下载全量包
    
    try:
        last_date = datetime.strptime(last_sync_date, '%Y-%m-%d')
        today_date = datetime.strptime(today, '%Y-%m-%d')
        
        # 计算需要补全的日期
        missing = []
        current = last_date + timedelta(days=1)
        while current <= today_date:
            missing.append(current.strftime('%Y%m%d'))
            current += timedelta(days=1)
        
        return missing
        
    except ValueError:
        return []

def sync_incremental(data_lake: LocalDataLake = None, progress_callback=None) -> Dict:
    """
    智能增量同步
    
    1. 获取最后同步日期
    2. 计算遗漏的日期
    3. 如果遗漏 > 7 天，尝试使用周更新包
    4. 否则下载每日更新包
    5. 导入数据库
    
    Args:
        data_lake: 数据湖实例
        progress_callback: 进度回调函数，接受 dict 参数 {stage, current, total, message}
    
    Returns:
        {
            'success': bool,
            'message': str,
            'synced_days': int,
            'total_records': int
        }
    """
    def _report_progress(stage, current, total, message):
        if progress_callback:
            try:
                progress_callback({'stage': stage, 'current': current, 'total': total, 'message': message})
            except RuntimeError:
                # 允许上层通过 RuntimeError 立即中断同步流程（例如用户主动停止）
                raise
            except Exception as e:
                print(f"[Sync] 进度回调失败（已忽略）: {e}")
    if data_lake is None:
        data_lake = LocalDataLake(db_path=DB_PATH)
    
    today = datetime.now().strftime('%Y-%m-%d')
    today_short = datetime.now().strftime('%Y%m%d')
    
    # 获取最后同步日期
    last_sync = data_lake.get_last_sync_date()
    print(f"[Sync] 上次同步日期: {last_sync or '从未同步'}")
    print(f"[Sync] 当前日期: {today}")
    _report_progress('checking', 5, 100, f'检查同步状态... 上次: {last_sync or "从未"}')
    
    if not last_sync:
        return {
            'success': False,
            'message': '首次使用请先手动下载全量数据包并导入',
            'synced_days': 0,
            'total_records': 0
        }
    
    # 计算需要补全的日期
    missing_dates = get_missing_dates(last_sync, today)
    
    if not missing_dates:
        return {
            'success': True,
            'message': '数据已是最新，无需同步',
            'synced_days': 0,
            'total_records': 0
        }
    
    print(f"[Sync] 需要补全 {len(missing_dates)} 天的数据")
    _report_progress('fetching_rss', 10, 100, f'获取 RSS 源... 需补全 {len(missing_dates)} 天')
    
    # 获取可用的文件列表
    if len(missing_dates) > 7:
        # 尝试使用周更新包
        print("[Sync] 遗漏超过7天，尝试使用周更新包...")
        available_files, fetch_error = fetch_rss_feed('weekly')
    else:
        available_files, fetch_error = fetch_rss_feed('daily')
    
    if not available_files:
        detail = fetch_error or '无法获取可下载文件列表，请检查网络'
        return {
            'success': False,
            'message': detail,
            'synced_days': 0,
            'total_records': 0
        }
    
    # 匹配需要下载的文件
    files_to_download = []
    for item in available_files:
        if item['date'] in missing_dates:
            files_to_download.append(item)
    
    if not files_to_download:
        # 尝试下载最新的文件
        if available_files:
            files_to_download = [available_files[0]]  # 取最新的
    
    print(f"[Sync] 将下载 {len(files_to_download)} 个文件")
    _report_progress('downloading', 20, 100, f'准备下载 {len(files_to_download)} 个文件...')
    
    # 下载并导入
    total_records = 0
    synced_count = 0
    failed_files = []
    
    for i, item in enumerate(files_to_download):
        dl_progress = 20 + int((i / max(len(files_to_download), 1)) * 60)
        _report_progress('downloading', dl_progress, 100, f'下载 {item["title"]} ({i+1}/{len(files_to_download)})')
        file_meta = download_zip(item['link'], item['title'])
        if file_meta:
            _report_progress('importing', dl_progress + 30, 100, f'导入 {item["title"]}...')
            count = extract_and_import(file_meta['path'], data_lake, file_meta)
            total_records += count
            if count > 0:
                synced_count += 1
            else:
                failed_files.append(item['title'])
        else:
            failed_files.append(item['title'])
            data_lake.log_sync_run(item['title'], 0, 'failed', '下载失败')

    if synced_count == 0:
        return {
            'success': False,
            'message': f'同步失败：文件下载成功但未导入记录。失败文件: {", ".join(failed_files[:3])}',
            'synced_days': 0,
            'total_records': total_records
        }

    return {
        'success': True,
        'message': f'已补全 {synced_count} 天的数据，共导入 {total_records} 条记录',
        'synced_days': synced_count,
        'total_records': total_records
    }

# ==========================================
# 主入口
# ==========================================

def main():
    """命令行入口"""
    import argparse
    
    parser = argparse.ArgumentParser(description='UDID 数据同步工具')
    parser.add_argument('--full', action='store_true', help='下载全量数据')
    parser.add_argument('--daily', action='store_true', help='下载最新每日更新')
    parser.add_argument('--sync', action='store_true', help='智能增量同步')
    parser.add_argument('--list', action='store_true', help='列出可用文件')
    
    args = parser.parse_args()
    
    data_lake = LocalDataLake(db_path=DB_PATH)
    
    if args.list:
        print("\n=== 每日更新 ===")
        daily_files, _ = fetch_rss_feed('daily')
        for item in daily_files[:5]:
            print(f"  {item['title']} ({item['count']} 条)")
        
        print("\n=== 每周更新 ===")
        weekly_files, _ = fetch_rss_feed('weekly')
        for item in weekly_files[:3]:
            print(f"  {item['title']}")
        
        print("\n=== 全量数据 ===")
        full_files, _ = fetch_rss_feed('full')
        for item in full_files[:1]:
            print(f"  {item['title']}")
    
    elif args.daily:
        files, _ = fetch_rss_feed('daily')
        if files:
            latest = files[0]
            print(f"下载最新每日更新: {latest['title']}")
            file_meta = download_zip(latest['link'], latest['title'])
            if file_meta:
                count = extract_and_import(file_meta['path'], data_lake, file_meta)
                print(f"导入完成: {count} 条")
    
    elif args.full:
        files, _ = fetch_rss_feed('full')
        if files:
            latest = files[0]
            print(f"下载全量数据: {latest['title']}")
            file_meta = download_zip(latest['link'], latest['title'])
            if file_meta:
                count = extract_and_import(file_meta['path'], data_lake, file_meta)
                print(f"导入完成: {count} 条")
    
    elif args.sync:
        result = sync_incremental(data_lake)
        print(f"\n同步结果: {result['message']}")
    
    else:
        # 默认执行智能同步
        result = sync_incremental(data_lake)
        print(f"\n同步结果: {result['message']}")

if __name__ == '__main__':
    main()
