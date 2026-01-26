"""
UDID AI 匹配服务
================

封装 AI API 调用逻辑，实现医疗器械需求与产品的智能匹配。
支持 OpenAI 兼容接口（中转站）。

版本: 1.0.0
"""

import os
import json
import time
import hashlib
from typing import List, Dict, Optional
import requests

# 配置文件路径
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'config.json')

# 默认配置
DEFAULT_CONFIG = {
    'api_base_url': 'https://api.openai.com/v1',
    'api_key': '',
    'model': 'gpt-4o-mini',
    'ai_min_score': 0,
    'ai_retry_count': 1,
    'ai_retry_backoff_sec': 1.0,
    'ai_cache_ttl_sec': 300
}

# 最大候选数量
MAX_CANDIDATES = 100

# 请求超时
REQUEST_TIMEOUT = 60

# 简易缓存与指标
_AI_CACHE: Dict[str, Dict] = {}
_AI_METRICS = {
    'total': 0,
    'success': 0,
    'fail': 0,
    'latency_ms_sum': 0.0
}

# ==========================================
# 配置管理
# ==========================================

def load_config() -> Dict:
    """加载 API 配置"""
    try:
        if os.path.exists(CONFIG_PATH):
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception as e:
        print(f"[AI] 加载配置失败: {e}")
    return DEFAULT_CONFIG

# ==========================================
# Prompt 构建
# ==========================================

def build_prompt(requirement: str, candidates: List[Dict], context_keyword: str = "") -> str:
    """
    构造 AI 匹配 Prompt
    
    Args:
        requirement: 用户需求描述
        candidates: 候选产品列表
    
    Returns:
        完整的 Prompt 字符串
    """
    # 构建产品列表描述 - 包含更完整的产品信息
    products_text = ""
    for i, p in enumerate(candidates[:MAX_CANDIDATES], 1):
        # 组合描述和适用范围，提供更完整的信息
        desc = p.get('description', '') or ''
        scope = p.get('scope', '') or ''
        full_desc = f"{desc} {scope}".strip()[:400]  # 扩展到 400 字符
        
        products_text += f"""
【产品{i}】
- ID: {p.get('di_code', '')}
- 名称: {p.get('product_name', '')}
- 规格型号: {p.get('model', '')}
- 生产企业: {p.get('manufacturer', '')}
- 产品描述: {full_desc}
"""
    
    prompt = f"""你是医疗器械采购专家。客户收到医院招标文件，需要从数据库中找到最匹配的产品和厂家。

## 招标/采购需求
{requirement}

## 候选产品列表
{products_text}

## 评分任务
请仔细对比每个产品与采购需求的匹配程度，重点关注：
1. 产品类型是否一致（如：注射器 vs 输液器 是不同产品）
2. 规格参数是否符合（如：容量、尺寸、材质）
3. 功能特性是否满足（如：是否带针头、是否可灭菌）

## 评分标准
- 90-100: 产品类型完全一致，规格参数完全符合
- 70-89: 产品类型一致，规格参数基本符合，有细微差异
- 50-69: 产品类型相关，但规格或功能有明显差异
- 30-49: 产品类型相近，但不太适合该需求
- 0-29: 产品类型不符，不推荐

## 输出格式 (严格 JSON)
{{
    "matches": [
        {{
            "id": "产品的 di_code",
            "score": 匹配度分数(0-100整数),
            "reason": "简短说明匹配或不匹配的关键原因(15字以内)"
        }}
    ]
}}

要求：
1. 只返回 JSON，不要其他文字
2. 按 score 从高到低排序
3. reason 要具体，如"规格完全符合"或"容量不匹配"
"""
    
    return prompt

def build_expansion_prompt(text: str) -> str:
    """构建关键词扩展 Prompt"""
    return f"""作为医疗器械专家，请从以下用户描述中提取 3-5 个核心关键词，并为每个关键词提供 1-2 个最常用的专业同义词。
    
用户描述: "{text}"

请直接返回 JSON 数组（不要 Markdown），格式如下：
["关键词1", "同义词1", "关键词2", "同义词2"]

例如用户输入"创口贴"，返回 ["创口贴", "敷料", "护创膜"]。只返回列表，不要其他解释。"""

# ==========================================
# API 调用
# ==========================================

def call_ai_api(prompt: str, config: Dict = None) -> Optional[str]:
    """
    调用 OpenAI 兼容接口
    
    Args:
        prompt: 完整的 Prompt
        config: API 配置
    
    Returns:
        AI 返回的文本内容，失败返回 None
    """
    if config is None:
        config = load_config()
    
    api_base = config.get('api_base_url', '').rstrip('/')
    api_key = config.get('api_key', '')
    model = config.get('model', 'gpt-4o-mini')
    retry_count = int(config.get('ai_retry_count', 1))
    backoff_sec = float(config.get('ai_retry_backoff_sec', 1.0))
    cache_ttl = int(config.get('ai_cache_ttl_sec', 0))
    
    if not api_base or not api_key:
        print("[AI] API 配置不完整")
        return None
    
    url = f"{api_base}/chat/completions"
    
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {api_key}'
    }
    
    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': '你是一个专业的医疗器械采购顾问，擅长分析产品与需求的匹配度。'},
            {'role': 'user', 'content': prompt}
        ],
        'temperature': 0.3,
        'max_tokens': 2000,
        'response_format': {'type': 'json_object'}  # 强制 JSON 输出
    }
    
    cache_key = None
    if cache_ttl > 0:
        cache_key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode('utf-8')).hexdigest()
        cached = _AI_CACHE.get(cache_key)
        if cached and time.time() - cached['ts'] <= cache_ttl:
            print("[AI] 命中缓存，直接返回")
            return cached['content']

    attempts = max(1, retry_count + 1)
    for attempt in range(1, attempts + 1):
        start = time.time()
        _AI_METRICS['total'] += 1
        try:
            print(f"[AI] 正在调用 AI API: {model} (attempt {attempt}/{attempts}) ...")
            response = requests.post(url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            
            data = response.json()
            content = data['choices'][0]['message']['content']
            latency_ms = (time.time() - start) * 1000
            _AI_METRICS['latency_ms_sum'] += latency_ms
            _AI_METRICS['success'] += 1
            avg_latency = _AI_METRICS['latency_ms_sum'] / max(1, _AI_METRICS['success'])
            print(f"[AI] API 调用成功，返回 {len(content)} 字符，耗时 {latency_ms:.0f}ms，均值 {avg_latency:.0f}ms")
            if cache_key:
                _AI_CACHE[cache_key] = {'ts': time.time(), 'content': content}
            return content
        
        except requests.Timeout:
            _AI_METRICS['fail'] += 1
            print("[AI] API 请求超时")
        except requests.RequestException as e:
            _AI_METRICS['fail'] += 1
            print(f"[AI] API 请求失败: {e}")
        except (KeyError, IndexError) as e:
            _AI_METRICS['fail'] += 1
            print(f"[AI] 响应解析失败: {e}")

        if attempt < attempts:
            time.sleep(backoff_sec)

    return None

# ==========================================
# 响应解析
# ==========================================

def parse_ai_response(response_text: str) -> List[Dict]:
    """
    解析 AI 返回的 JSON 响应
    
    Args:
        response_text: AI 返回的文本
    
    Returns:
        匹配结果列表 [{'id': str, 'score': int, 'reason': str}, ...]
    """
    if not response_text:
        return []
    
    try:
        # 尝试直接解析
        data = json.loads(response_text)
        
        # 检查结构
        if 'matches' in data and isinstance(data['matches'], list):
            matches = []
            for item in data['matches']:
                if all(k in item for k in ['id', 'score', 'reason']):
                    matches.append({
                        'id': str(item['id']),
                        'score': int(item['score']),
                        'reason': str(item['reason'])[:50]  # 限制长度
                    })
            return sorted(matches, key=lambda x: x['score'], reverse=True)
    
    except json.JSONDecodeError as e:
        print(f"[AI] JSON 解析失败: {e}")
        
        # 尝试提取 JSON 部分
        try:
            import re
            json_match = re.search(r'\{[\s\S]*\}', response_text)
            if json_match:
                return parse_ai_response(json_match.group())
        except:
            pass
    
    except Exception as e:
        print(f"[AI] 响应解析异常: {e}")
    
    return []

# ==========================================
# 主匹配函数
# ==========================================

def ai_match_products(requirement: str, candidates: List[Dict], context_keyword: str = "", retry: bool = True) -> Dict:
    """
    AI 智能匹配产品
    
    Args:
        requirement: 用户需求描述
        candidates: 候选产品列表
        retry: 是否在失败时重试
    
    Returns:
        {
            'success': bool,
            'matches': [{'id': str, 'score': int, 'reason': str}, ...],
            'error': str (可选)
        }
    """
    if not requirement or not requirement.strip():
        return {'success': False, 'error': '请输入需求描述', 'matches': []}
    
    if not candidates:
        return {'success': False, 'error': '没有候选产品', 'matches': []}
    
    config = load_config()
    
    if not config.get('api_key'):
        return {'success': False, 'error': '请先配置 API Key', 'matches': []}
    
    # 构建 Prompt
    prompt = build_prompt(requirement, candidates, context_keyword)
    
    min_score = int(config.get('ai_min_score', 0))

    # 调用 API
    response = call_ai_api(prompt, config)
    
    if not response:
        return {'success': False, 'error': 'AI 服务暂不可用，请稍后重试', 'matches': []}
    
    # 解析响应
    matches = parse_ai_response(response)
    
    if not matches:
        return {'success': False, 'error': '无法解析 AI 响应', 'matches': []}

    if min_score > 0:
        matches = [m for m in matches if m.get('score', 0) >= min_score]
        if not matches:
            return {'success': False, 'error': f'匹配分数低于阈值({min_score})', 'matches': []}
    
    return {'success': True, 'matches': matches}

def merge_match_results(products: List[Dict], matches: List[Dict]) -> List[Dict]:
    """
    将 AI 匹配结果合并到产品列表
    
    Args:
        products: 原始产品列表
        matches: AI 匹配结果
    
    Returns:
        带有 matchScore 和 matchReason 的产品列表
    """
    # 创建匹配字典
    match_dict = {m['id']: m for m in matches}
    
    # 合并结果
    result = []
    for product in products:
        di_code = product.get('di_code', '')
        if di_code in match_dict:
            product['matchScore'] = match_dict[di_code]['score']
            product['matchReason'] = match_dict[di_code]['reason']
            result.append(product)
    
    # 按匹配度排序
    result.sort(key=lambda x: x.get('matchScore', 0), reverse=True)
    
    return result

def expand_search_keywords(text: str) -> List[str]:
    """
    通过 AI 扩展搜索关键词
    """
    if not text:
        return []
    
    config = load_config()
    if not config.get('api_key'):
        return []
        
    prompt = build_expansion_prompt(text)
    
    response = call_ai_api(prompt, config)
    if not response:
        return []
        
    try:
        import json
        # 清理 markdown 标记
        clean_text = response.replace('```json', '').replace('```', '').strip()
        # 寻找数组
        start = clean_text.find('[')
        end = clean_text.rfind(']') + 1
        
        if start != -1 and end != 0:
            json_str = clean_text[start:end]
            keywords = json.loads(json_str)
            if isinstance(keywords, list):
                return [str(k).strip() for k in keywords if k]
    except Exception as e:
        print(f"[AI] 关键词扩展解析失败: {e}")
        
    return []

# ==========================================
# 测试入口
# ==========================================

if __name__ == '__main__':
    # 测试配置
    config = load_config()
    print(f"API Base: {config.get('api_base_url', 'Not set')}")
    print(f"Model: {config.get('model', 'Not set')}")
    print(f"API Key: {'Set' if config.get('api_key') else 'Not set'}")
    
    # 模拟测试
    test_products = [
        {'di_code': '001', 'product_name': '人工膝关节假体', 'model': '标准型', 'manufacturer': '某医疗公司', 'description': '用于全膝关节置换手术'},
        {'di_code': '002', 'product_name': '骨水泥', 'model': '高粘度型', 'manufacturer': '某医疗公司', 'description': '用于关节假体固定'},
    ]
    
    print("\n测试 Prompt 构建:")
    prompt = build_prompt("膝关节置换手术用的假体", test_products)
    print(prompt[:500] + "...")
