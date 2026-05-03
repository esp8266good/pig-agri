from typing import Optional

import asyncpg
from loguru import logger

from config import settings

_pool: Optional[asyncpg.Pool] = None


async def connect() -> None:
    global _pool
    _pool = await asyncpg.create_pool(settings.database_url, min_size=2, max_size=10)
    logger.info("Database pool created")


async def disconnect() -> None:
    global _pool
    if _pool:
        await _pool.close()
        _pool = None
        logger.info("Database pool closed")


def get_pool() -> Optional[asyncpg.Pool]:
    return _pool
