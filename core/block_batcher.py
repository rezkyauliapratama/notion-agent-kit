"""Block Batcher - Handle Notion 100-block limit.

Ordering guarantee (production bug 2026-10-05):
Appending in concurrent waves via asyncio.gather reordered blocks across
batches. A 104-block document (batches of 100 + 4) landed its 4 trailing
blocks at the TOP of the page, because batches ran in parallel and completed
out of order. The default is now strictly SEQUENTIAL: each batch is awaited
before the next one is sent, so block order matches the input exactly.
"""

import asyncio
import logging
import time
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)

BATCH_SIZE = 100
MAX_RETRIES = 3
CONCURRENT_WAVE = 3


class BlockBatcher:
    """Split, batch, and append blocks to Notion."""

    def __init__(self, client):
        self.client = client

    async def append_blocks(self, parent_block_id: str, blocks: List[Dict],
                            concurrent: bool = False,
                            after: Optional[str] = None) -> Dict[str, Any]:
        """Append blocks in batches of BATCH_SIZE, preserving input order.

        By default batches are sent SEQUENTIALLY (await each batch before the
        next). This is required for correct block ordering: Notion appends at
        the end, so overlapping requests can finish out of order and scramble
        the document (observed with a 104-block doc landing 100 + 4 reversed).

        Args:
            parent_block_id: Page or block ID to append to.
            blocks: Raw Notion block JSON, in the desired final order.
            concurrent: If True, send up to CONCURRENT_WAVE batches in parallel
                via asyncio.gather. WARNING: this does NOT guarantee block order
                and can interleave batches; only enable it when order does not
                matter (e.g. order-insensitive bulk import).
            after: Optional sibling block ID. Only the FIRST batch carries
                `after`; subsequent batches are appended without it so the
                batches stay in order (each later batch lands directly after
                the previous one at the end of the parent).

        Returns:
            {"total_blocks", "batches", "duration_ms", "rate_limited"}.
        """
        if not blocks:
            return {"total_blocks": 0, "batches": 0, "duration_ms": 0, "rate_limited": False}
        start_time = time.monotonic()
        total = len(blocks)
        rate_limited = False
        chunks = [blocks[i:i + BATCH_SIZE] for i in range(0, total, BATCH_SIZE)]
        logger.info(f"Splitting {total} blocks into {len(chunks)} batches "
                    f"(mode={'concurrent' if concurrent else 'sequential'})")

        if concurrent:
            for i in range(0, len(chunks), CONCURRENT_WAVE):
                wave = chunks[i:i + CONCURRENT_WAVE]
                tasks = [self._append_with_retry(parent_block_id, chunk,
                                                 after if (i + j) == 0 else None)
                         for j, chunk in enumerate(wave)]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for result in results:
                    if isinstance(result, dict) and result.get("rate_limited"):
                        rate_limited = True
                if i + CONCURRENT_WAVE < len(chunks):
                    await asyncio.sleep(0.5)
        else:
            for idx, chunk in enumerate(chunks):
                chunk_after = after if idx == 0 else None
                result = await self._append_with_retry(parent_block_id, chunk, chunk_after)
                if isinstance(result, dict) and result.get("rate_limited"):
                    rate_limited = True
                # Small pause between batches to stay under the ~3 req/s limit.
                if idx + 1 < len(chunks):
                    await asyncio.sleep(0.5)

        duration = int((time.monotonic() - start_time) * 1000)
        return {"total_blocks": total, "batches": len(chunks), "duration_ms": duration, "rate_limited": rate_limited}

    async def _append_with_retry(self, parent_block_id: str, chunk: List[Dict],
                                 after: Optional[str] = None) -> Dict:
        for attempt in range(MAX_RETRIES):
            try:
                # Only pass `after` when set so minimal fake/legacy clients that
                # implement the two-argument signature keep working.
                if after is not None:
                    return await self.client.append_block_children(parent_block_id, chunk, after=after)
                return await self.client.append_block_children(parent_block_id, chunk)
            except Exception as e:
                logger.warning(f"Batch append failed (attempt {attempt + 1}/{MAX_RETRIES}): {e}")
                if attempt < MAX_RETRIES - 1:
                    await asyncio.sleep(2 ** attempt)
                else:
                    logger.error(f"Batch append failed after {MAX_RETRIES} retries")
                    raise
        return {}
