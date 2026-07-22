"""Retry decorator with exponential backoff and jitter."""

import asyncio
import random
import logging
from functools import wraps
from typing import Callable, Any

logger = logging.getLogger(__name__)


def with_retry(max_retries: int = 3, base_delay: float = 1.0):
    def decorator(func: Callable) -> Callable:
        @wraps(func)
        async def wrapper(*args, **kwargs) -> Any:
            last_exception = None
            for attempt in range(max_retries + 1):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if hasattr(e, 'response') and e.response is not None:
                        status = e.response.status_code
                        if status in (400, 401, 403, 404):
                            raise
                    if attempt < max_retries:
                        delay = base_delay * (2 ** attempt) + random.uniform(0, 0.5)
                        logger.info(f"Retry {attempt + 1}/{max_retries} after {delay:.1f}s: {e}")
                        await asyncio.sleep(delay)
                    else:
                        logger.error(f"All {max_retries + 1} attempts failed: {e}")
            raise last_exception
        return wrapper
    return decorator
