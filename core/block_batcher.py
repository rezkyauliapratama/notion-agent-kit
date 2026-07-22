"""Block Batcher - Handle Notion 100-block limit with concurrent batching."""

import asyncio
import logging
import time
from typing import List, Dict, Any

logger = logging.getLogger(__name__)

BATCH_SIZE = 100
MAX_RETRIES = 3
CONCURRENT_WAVE = 3


class BlockBatcher:
    """Split, batch, and concurrently append blocks to Notion."""

    def __init__(self, client):
        self.client = client

    async def append_blocks(self, parent_block_id: str, blocks: List[Dict]) -> Dict[str, Any]:
        if not blocks:
            return {"total_blocks": 0, "batches": 0, "duration_ms": 0, "rate_limited": False}
        start_time = time.monotonic()
        total = len(blocks)
        rate_limited = False
        chunks = [blocks[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
        logger.info(f"Splitting {total} blocks into {len(chunks)} batches")
        for i in range(0, len(chunks), CONCURRENT_WAVE):
            wave = chunks[i:i + CONCURRENT_WAVE]
            tasks = [self._append_with_retry(parent_block_id, chunk) for chunk in wave]
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for result in results:
                if isinstance(result, dict) and result.get("rate_limited"):
                    rate_limited = True
            if i + CONCURRENT_WAVE < len(chunks):
                await asyncio.sleep(0.5)
        duration = int((time.monotonic() - start_time) * 1000)
        return {"total_blocks": total, "batches": len(chunks), "duration_ms": duration, "rate_limited": rate_limited}

    async def _append_with_retry(self, parent_block_id: str, chunk: List[Dict]) -> Dict:
        for attempt in range(MAX_RETRIES):
            try:
                return await self.client.append_block_children(parent_block_id, chunk)
            except Exception as e:
                logger.warning(f"Batch append failed (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.error(f"Batch append failed after {MAX_RETRIES} retries")
                    raise
        return {}
