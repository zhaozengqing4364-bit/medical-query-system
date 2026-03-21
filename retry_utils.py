"""
共享重试工具
================

为网络请求提供指数退避 + 抖动重试装饰器。
"""

import random
import time
from functools import wraps
from typing import Callable

import requests


def retry_with_backoff(max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 60.0):
    """
    装饰器：为函数添加指数退避重试机制

    Args:
        max_retries: 最大重试次数
        base_delay: 基础延迟（秒）
        max_delay: 最大延迟（秒）
    """

    def decorator(func: Callable):
        @wraps(func)
        def wrapper(*args, **kwargs):
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return func(*args, **kwargs)
                except requests.exceptions.RequestException as e:
                    last_exception = e
                    if attempt < max_retries:
                        delay = min(base_delay * (2 ** attempt), max_delay)
                        delay = delay * (0.5 + random.random())

                        status_code = None
                        if hasattr(e, 'response') and e.response is not None:
                            status_code = e.response.status_code
                            if 400 <= status_code < 500 and status_code not in [429, 408]:
                                raise

                        print(f"[Retry] {func.__name__} 第 {attempt + 1} 次尝试失败 (状态码: {status_code})，{delay:.1f}秒后重试...")
                        time.sleep(delay)
                    else:
                        raise
                except Exception:
                    raise
            raise last_exception

        return wrapper

    return decorator

