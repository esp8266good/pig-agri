import pytest
import database


@pytest.mark.asyncio
async def test_connect_creates_pool():
    await database.connect()
    assert database.get_pool() is not None
    await database.disconnect()


@pytest.mark.asyncio
async def test_disconnect_clears_pool():
    await database.connect()
    await database.disconnect()
    assert database.get_pool() is None


@pytest.mark.asyncio
async def test_required_tables_exist():
    await database.connect()
    pool = database.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        )
        table_names = {r["tablename"] for r in rows}
    await database.disconnect()
    assert "tracking_logs" in table_names
    assert "health_alerts" in table_names
    assert "pig_notes" in table_names
    assert "user_settings" in table_names


@pytest.mark.asyncio
async def test_user_settings_defaults_inserted():
    await database.connect()
    pool = database.get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT key FROM user_settings")
        keys = {r["key"] for r in rows}
    await database.disconnect()
    assert "analysis_interval_minutes" in keys
    assert "anomaly_std_threshold" in keys
    assert "hls_retention_days" in keys
