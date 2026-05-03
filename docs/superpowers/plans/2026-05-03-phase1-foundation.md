# Phase 1 — 地基 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立系統地基：Docker Compose PostgreSQL + schema、FastAPI 骨架（含 lifespan 優雅關閉）、ZMQ daemon thread（只 log 收到的 frame）

**Architecture:** ZMQ receiver 跑在獨立 daemon thread（沿用現有 `run_realtime_track.py` 架構），FastAPI endpoint 使用 asyncpg async pool。lifespan 管理啟動與關閉順序。PostgreSQL 由 Docker Compose named volume 管理，schema 在首次啟動時由 `sql/init.sql` 自動執行。

**Tech Stack:** Python 3.11+, uv, FastAPI 0.115+, uvicorn, asyncpg 0.29+, pyzmq 26+, pydantic-settings 2+, loguru, pytest + pytest-asyncio + httpx, Docker Compose, PostgreSQL 16

---

## File Map

| 檔案 | 職責 |
|------|------|
| `pyproject.toml` | uv 依賴 + pytest 設定 |
| `.gitignore` | 排除 .env、快取、venv |
| `docker-compose.yml` | PostgreSQL 容器 + named volume |
| `sql/init.sql` | 四張資料表 + 預設設定 INSERT |
| `.env.example` | 設定範本（入 git） |
| `.env` | 實際設定，含真實 RPI_IP（不入 git） |
| `config.py` | pydantic-settings Settings，含逗號分隔 topics 解析 |
| `database.py` | asyncpg pool：connect / disconnect / get_pool |
| `zmq_receiver.py` | ZMQReceiver class + 模組級 singleton `zmq_receiver` |
| `main.py` | FastAPI app + lifespan + `GET /health` |
| `hls_manager.py` | 空骨架（Phase 2 填入） |
| `inference/__init__.py` | 空骨架（Phase 3 填入） |
| `analysis/__init__.py` | 空骨架（Phase 5 填入） |
| `routers/__init__.py` | router package |
| `routers/stream.py` | /stream stubs |
| `routers/tracking.py` | /tracking stub |
| `routers/alerts.py` | /alerts stubs |
| `routers/settings.py` | /settings stubs |
| `routers/notes.py` | /notes stubs（spec 備註功能） |
| `tests/__init__.py` | pytest package |
| `tests/test_config.py` | Settings 單元測試 |
| `tests/test_database.py` | asyncpg 整合測試（需 Docker PostgreSQL） |
| `tests/test_zmq_receiver.py` | ZMQReceiver 生命週期單元測試 |
| `tests/test_main.py` | FastAPI endpoint 測試（mock lifespan） |

---

## Task 1: uv 專案初始化

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`

- [ ] **Step 1: 建立 pyproject.toml**

```toml
[project]
name = "pig-monitoring"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi>=0.115",
    "uvicorn[standard]>=0.30",
    "asyncpg>=0.29",
    "pyzmq>=26",
    "pydantic-settings>=2.0",
    "loguru>=0.7",
    "opencv-python-headless>=4.9",
]

[project.optional-dependencies]
dev = [
    "pytest>=8",
    "pytest-asyncio>=0.24",
    "httpx>=0.27",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"
pythonpath = ["."]
```

- [ ] **Step 2: 建立 .gitignore**

```gitignore
.env
__pycache__/
*.pyc
.venv/
*.egg-info/
/data/
.pytest_cache/
```

- [ ] **Step 3: 安裝依賴**

```bash
uv sync --extra dev
```

Expected output 末尾類似：
```
Installed N packages in Xs
```

- [ ] **Step 4: 驗證 Python 環境**

```bash
uv run python -c "import fastapi, asyncpg, zmq, pydantic_settings; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add pyproject.toml .gitignore
git commit -m "chore: init uv project with Phase 1 dependencies"
```

---

## Task 2: Docker Compose + SQL Schema

**Files:**
- Create: `docker-compose.yml`
- Create: `sql/init.sql`

- [ ] **Step 1: 建立 docker-compose.yml**

```yaml
services:
  postgres:
    image: postgres:16
    environment:
      POSTGRES_USER: pig
      POSTGRES_PASSWORD: pig_password
      POSTGRES_DB: pig_monitoring
    volumes:
      - pg_data:/var/lib/postgresql/data
      - ./sql/init.sql:/docker-entrypoint-initdb.d/init.sql
    ports:
      - "15432:5432"
    restart: unless-stopped

volumes:
  pg_data:
```

- [ ] **Step 2: 建立 sql/init.sql**

```sql
CREATE TABLE IF NOT EXISTS tracking_logs (
    id                BIGSERIAL PRIMARY KEY,
    camera_id         VARCHAR(16) NOT NULL,
    timestamp         DOUBLE PRECISION NOT NULL,
    frame_id          BIGINT NOT NULL,
    object_id         INTEGER NOT NULL,
    bb_left           REAL,
    bb_top            REAL,
    bb_width          REAL,
    bb_height         REAL,
    confidence        REAL,
    thermal_intensity REAL
);
CREATE INDEX IF NOT EXISTS idx_tracking ON tracking_logs (camera_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS health_alerts (
    id            BIGSERIAL PRIMARY KEY,
    camera_id     VARCHAR(16) NOT NULL,
    object_id     INTEGER NOT NULL,
    triggered_at  TIMESTAMPTZ DEFAULT NOW(),
    metric        VARCHAR(32) NOT NULL,
    current_value REAL,
    mean_value    REAL,
    std_value     REAL,
    is_read       BOOLEAN DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS pig_notes (
    id          BIGSERIAL PRIMARY KEY,
    camera_id   VARCHAR(16) NOT NULL,
    object_id   INTEGER,
    note_time   TIMESTAMPTZ NOT NULL,
    content     TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_settings (
    key        VARCHAR(64) PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO user_settings (key, value, updated_at) VALUES
    ('jpeg_quality', '70', NOW()),
    ('analysis_interval_minutes', '30', NOW()),
    ('anomaly_std_threshold', '3.0', NOW()),
    ('hls_retention_days', '90', NOW())
ON CONFLICT (key) DO NOTHING;
```

- [ ] **Step 3: 啟動 PostgreSQL**

```bash
docker compose up -d
```

Expected:
```
✔ Container pig-agri-postgres-1  Started
```
（容器名稱依目錄名稱而定）

- [ ] **Step 4: 驗證資料表**

```bash
docker compose exec postgres psql -U pig -d pig_monitoring -c "\dt"
```

Expected 輸出包含：
```
 public | health_alerts | table | pig
 public | pig_notes     | table | pig
 public | tracking_logs | table | pig
 public | user_settings | table | pig
```

- [ ] **Step 5: 驗證預設設定**

```bash
docker compose exec postgres psql -U pig -d pig_monitoring -c "SELECT key, value FROM user_settings;"
```

Expected:
```
           key            | value
--------------------------+-------
 jpeg_quality             | 70
 analysis_interval_minutes| 30
 anomaly_std_threshold    | 3.0
 hls_retention_days       | 90
```

- [ ] **Step 6: Commit**

```bash
git add docker-compose.yml sql/init.sql
git commit -m "chore: add PostgreSQL Docker Compose stack and schema"
```

---

## Task 3: Settings 模組

**Files:**
- Create: `config.py`
- Create: `.env.example`
- Create: `tests/__init__.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: 建立 tests/__init__.py（空檔案）**

```bash
touch tests/__init__.py
```

- [ ] **Step 2: 建立 tests/test_config.py（先寫測試）**

```python
from config import Settings


def test_default_zmq_port():
    s = Settings(
        database_url="postgresql://pig:pig_password@localhost:15432/pig_monitoring",
        rpi_ip="127.0.0.1",
    )
    assert s.zmq_port == 5555


def test_default_mot_worker_threads():
    s = Settings(
        database_url="postgresql://pig:pig_password@localhost:15432/pig_monitoring",
        rpi_ip="127.0.0.1",
    )
    assert s.mot_worker_threads == 20


def test_default_anomaly_threshold():
    s = Settings(
        database_url="postgresql://pig:pig_password@localhost:15432/pig_monitoring",
        rpi_ip="127.0.0.1",
    )
    assert s.anomaly_std_threshold == 3.0


def test_camera_topics_default_six():
    s = Settings(
        database_url="postgresql://pig:pig_password@localhost:15432/pig_monitoring",
        rpi_ip="127.0.0.1",
        camera_topics="cam_01,cam_02,cam_03,cam_04,cam_05,cam_06",
    )
    assert len(s.camera_topics) == 6


def test_camera_topics_comma_parsing():
    s = Settings(
        database_url="postgresql://pig:pig_password@localhost:15432/pig_monitoring",
        rpi_ip="127.0.0.1",
        camera_topics="cam_01,cam_02",
    )
    assert s.camera_topics == ["cam_01", "cam_02"]


def test_camera_topics_strips_whitespace():
    s = Settings(
        database_url="postgresql://pig:pig_password@localhost:15432/pig_monitoring",
        rpi_ip="127.0.0.1",
        camera_topics="cam_01, cam_02 , cam_03",
    )
    assert s.camera_topics == ["cam_01", "cam_02", "cam_03"]
```

- [ ] **Step 3: 執行測試，確認 FAIL**

```bash
uv run pytest tests/test_config.py -v
```

Expected: `ModuleNotFoundError: No module named 'config'`

- [ ] **Step 4: 建立 config.py**

```python
from typing import List

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = "postgresql://pig:pig_password@localhost:15432/pig_monitoring"
    zmq_port: int = 5555
    rpi_ip: str = "127.0.0.1"
    camera_topics: List[str] = [
        "cam_01", "cam_02", "cam_03", "cam_04", "cam_05", "cam_06"
    ]
    hls_base_dir: str = "/data/pig_monitoring/hls"
    hls_retention_days: int = 90
    jpeg_quality: int = 70
    model_weights: str = "./ref/HybridSORT/pretrained/best_ckpt.pth.tar"
    # Note: pydantic reserves 'model_config'; use model_config_path here.
    # In .env, write MODEL_CONFIG_PATH (not MODEL_CONFIG).
    model_config_path: str = (
        "./ref/HybridSORT/exps/example/mot/yolox_oink_test_hybrid_sort_reid.py"
    )
    device: str = "cuda"
    mot_worker_threads: int = 20
    analysis_interval_minutes: int = 30
    analysis_window_hours: int = 6
    anomaly_std_threshold: float = 3.0
    anomaly_min_samples: int = 50

    @field_validator("camera_topics", mode="before")
    @classmethod
    def parse_comma_separated(cls, v: object) -> object:
        if isinstance(v, str):
            return [t.strip() for t in v.split(",") if t.strip()]
        return v


settings = Settings()
```

- [ ] **Step 5: 建立 .env.example**

```env
# 資料庫
DATABASE_URL=postgresql://pig:pig_password@localhost:15432/pig_monitoring

# ZMQ
ZMQ_PORT=5555
RPI_IP=100.73.233.110
CAMERA_TOPICS=cam_01,cam_02,cam_03,cam_04,cam_05,cam_06

# 儲存路徑
HLS_BASE_DIR=/data/pig_monitoring/hls
HLS_RETENTION_DAYS=90

# 影像
JPEG_QUALITY=70

# 推論（Phase 3）
MODEL_WEIGHTS=./ref/HybridSORT/pretrained/best_ckpt.pth.tar
# pydantic 保留 model_config，故此欄位用 MODEL_CONFIG_PATH
MODEL_CONFIG_PATH=./ref/HybridSORT/exps/example/mot/yolox_oink_test_hybrid_sort_reid.py
DEVICE=cuda

# CPU 執行緒池
MOT_WORKER_THREADS=20

# 分析排程（Phase 5）
ANALYSIS_INTERVAL_MINUTES=30
ANALYSIS_WINDOW_HOURS=6
ANOMALY_STD_THRESHOLD=3.0
ANOMALY_MIN_SAMPLES=50
```

- [ ] **Step 6: 建立 .env（複製範本並填入真實 RPI_IP）**

```bash
cp .env.example .env
```

然後用編輯器把 `.env` 裡的 `RPI_IP=100.73.233.110` 改成實際 RPi 的 Tailscale IP（`tailscale ip -4` 查詢）。

- [ ] **Step 7: 執行測試，確認 PASS**

```bash
uv run pytest tests/test_config.py -v
```

Expected:
```
tests/test_config.py::test_default_zmq_port PASSED
tests/test_config.py::test_default_mot_worker_threads PASSED
tests/test_config.py::test_default_anomaly_threshold PASSED
tests/test_config.py::test_camera_topics_default_six PASSED
tests/test_config.py::test_camera_topics_comma_parsing PASSED
tests/test_config.py::test_camera_topics_strips_whitespace PASSED
6 passed
```

- [ ] **Step 8: Commit**

```bash
git add config.py .env.example tests/__init__.py tests/test_config.py
git commit -m "feat: add Settings module with comma-separated topics validator"
```

---

## Task 4: Database 模組

**Files:**
- Create: `database.py`
- Create: `tests/test_database.py`

前置條件：Task 2 的 Docker Compose PostgreSQL 必須在執行中（`docker compose ps` 顯示 `running`）。

- [ ] **Step 1: 建立 tests/test_database.py（先寫測試）**

```python
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
    assert "jpeg_quality" in keys
    assert "analysis_interval_minutes" in keys
    assert "anomaly_std_threshold" in keys
    assert "hls_retention_days" in keys
```

- [ ] **Step 2: 執行測試，確認 FAIL**

```bash
uv run pytest tests/test_database.py -v
```

Expected: `ModuleNotFoundError: No module named 'database'`

- [ ] **Step 3: 建立 database.py**

```python
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
```

- [ ] **Step 4: 執行測試，確認 PASS**

```bash
uv run pytest tests/test_database.py -v
```

Expected:
```
tests/test_database.py::test_connect_creates_pool PASSED
tests/test_database.py::test_disconnect_clears_pool PASSED
tests/test_database.py::test_required_tables_exist PASSED
tests/test_database.py::test_user_settings_defaults_inserted PASSED
4 passed
```

- [ ] **Step 5: Commit**

```bash
git add database.py tests/test_database.py
git commit -m "feat: add asyncpg database pool module"
```

---

## Task 5: ZMQ Receiver

**Files:**
- Create: `zmq_receiver.py`
- Create: `tests/test_zmq_receiver.py`

- [ ] **Step 1: 建立 tests/test_zmq_receiver.py（先寫測試）**

```python
import time
from zmq_receiver import ZMQReceiver


def test_receiver_starts_thread():
    receiver = ZMQReceiver()
    receiver.start()
    assert receiver._running is True
    assert receiver._thread is not None
    assert receiver._thread.is_alive()
    receiver.stop()


def test_receiver_thread_is_daemon():
    receiver = ZMQReceiver()
    receiver.start()
    assert receiver._thread.daemon is True
    receiver.stop()


def test_receiver_stops_cleanly():
    receiver = ZMQReceiver()
    receiver.start()
    time.sleep(0.15)  # 等一個 poll 週期（100ms）走完
    receiver.stop()
    assert receiver._running is False
    assert not receiver._thread.is_alive()
```

- [ ] **Step 2: 執行測試，確認 FAIL**

```bash
uv run pytest tests/test_zmq_receiver.py -v
```

Expected: `ModuleNotFoundError: No module named 'zmq_receiver'`

- [ ] **Step 3: 建立 zmq_receiver.py**

```python
import struct
import threading
from typing import Optional

import zmq
from loguru import logger

from config import settings


class ZMQReceiver:
    def __init__(self) -> None:
        self._thread: Optional[threading.Thread] = None
        self._running = False

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._run, daemon=True, name="zmq-receiver"
        )
        self._thread.start()
        logger.info("ZMQ receiver started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=3)
        logger.info("ZMQ receiver stopped")

    def _run(self) -> None:
        ctx = zmq.Context()
        sock = ctx.socket(zmq.SUB)
        sock.connect(f"tcp://{settings.rpi_ip}:{settings.zmq_port}")
        sock.setsockopt(zmq.SUBSCRIBE, b"rpi_sensors")
        for topic in settings.camera_topics:
            sock.setsockopt(zmq.SUBSCRIBE, topic.encode())

        while self._running:
            if sock.poll(100) == 0:
                continue
            try:
                parts = sock.recv_multipart()
                if len(parts) < 4:
                    continue
                topic = parts[0].decode()
                ts, frame_id = struct.unpack("dQ", parts[1])
                logger.info(
                    f"[{topic}] frame={frame_id} ts={ts:.3f} "
                    f"rgb={len(parts[2])}B thermal={len(parts[3])}B"
                )
            except Exception as e:
                logger.error(f"ZMQ error: {e}")

        sock.close()
        ctx.term()


zmq_receiver = ZMQReceiver()
```

- [ ] **Step 4: 執行測試，確認 PASS**

```bash
uv run pytest tests/test_zmq_receiver.py -v
```

Expected:
```
tests/test_zmq_receiver.py::test_receiver_starts_thread PASSED
tests/test_zmq_receiver.py::test_receiver_thread_is_daemon PASSED
tests/test_zmq_receiver.py::test_receiver_stops_cleanly PASSED
3 passed
```

Note: ZMQ 的 `connect()` 是非阻塞的，即使 RPi 沒有回應也不會阻塞測試。

- [ ] **Step 5: Commit**

```bash
git add zmq_receiver.py tests/test_zmq_receiver.py
git commit -m "feat: add ZMQ receiver daemon thread (Phase 1: log only)"
```

---

## Task 6: Router Stubs

**Files:**
- Create: `routers/__init__.py`
- Create: `routers/stream.py`
- Create: `routers/tracking.py`
- Create: `routers/alerts.py`
- Create: `routers/settings.py`
- Create: `routers/notes.py`

- [ ] **Step 1: 建立 routers/__init__.py（空檔案）**

```bash
touch routers/__init__.py
```

- [ ] **Step 2: 建立 routers/stream.py**

```python
from fastapi import APIRouter

router = APIRouter(prefix="/stream", tags=["stream"])


@router.get("/{camera_id}/live")
async def get_live_stream(camera_id: str):
    return {"status": "not implemented"}


@router.get("/{camera_id}/vod")
async def get_vod_stream(camera_id: str, start: float = 0, end: float = 0):
    return {"status": "not implemented"}


@router.get("/hls/{camera_id}/{path:path}")
async def serve_hls(camera_id: str, path: str):
    return {"status": "not implemented"}
```

- [ ] **Step 3: 建立 routers/tracking.py**

```python
from typing import Optional

from fastapi import APIRouter

router = APIRouter(prefix="/tracking", tags=["tracking"])


@router.get("/{camera_id}")
async def get_tracking(
    camera_id: str,
    start: Optional[float] = None,
    end: Optional[float] = None,
    object_id: Optional[int] = None,
):
    return {"status": "not implemented"}
```

- [ ] **Step 4: 建立 routers/alerts.py**

```python
from typing import Optional

from fastapi import APIRouter

router = APIRouter(prefix="/alerts", tags=["alerts"])


@router.get("")
async def get_alerts(
    camera_id: Optional[str] = None,
    unread_only: bool = False,
    limit: int = 50,
):
    return {"status": "not implemented"}


@router.put("/{alert_id}/read")
async def mark_alert_read(alert_id: int):
    return {"status": "not implemented"}
```

- [ ] **Step 5: 建立 routers/settings.py**

```python
from fastapi import APIRouter, Request

router = APIRouter(prefix="/settings", tags=["settings"])


@router.get("")
async def get_settings():
    return {"status": "not implemented"}


@router.put("")
async def update_settings(request: Request):
    return {"status": "not implemented"}
```

- [ ] **Step 6: 建立 routers/notes.py**

```python
from typing import Optional

from fastapi import APIRouter, Request

router = APIRouter(prefix="/notes", tags=["notes"])


@router.post("")
async def create_note(request: Request):
    return {"status": "not implemented"}


@router.get("")
async def get_notes(
    camera_id: Optional[str] = None,
    start: Optional[float] = None,
    end: Optional[float] = None,
):
    return {"status": "not implemented"}
```

- [ ] **Step 7: Commit**

```bash
git add routers/
git commit -m "feat: add stub routers for all spec endpoints"
```

---

## Task 7: Main App + Stub Modules

**Files:**
- Create: `main.py`
- Create: `hls_manager.py`
- Create: `inference/__init__.py`
- Create: `analysis/__init__.py`
- Create: `tests/test_main.py`

- [ ] **Step 1: 建立 tests/test_main.py（先寫測試）**

```python
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

import database
import zmq_receiver as zmq_mod


@pytest.fixture
def client():
    with (
        patch.object(database, "connect", new_callable=AsyncMock),
        patch.object(database, "disconnect", new_callable=AsyncMock),
        patch.object(zmq_mod.zmq_receiver, "start"),
        patch.object(zmq_mod.zmq_receiver, "stop"),
    ):
        from main import app
        with TestClient(app) as c:
            yield c


def test_health_returns_ok(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_stream_live_returns_stub(client):
    resp = client.get("/stream/cam_01/live")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}


def test_stream_vod_returns_stub(client):
    resp = client.get("/stream/cam_01/vod")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}


def test_tracking_returns_stub(client):
    resp = client.get("/tracking/cam_01")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}


def test_alerts_returns_stub(client):
    resp = client.get("/alerts")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}


def test_settings_get_returns_stub(client):
    resp = client.get("/settings")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}


def test_notes_get_returns_stub(client):
    resp = client.get("/notes")
    assert resp.status_code == 200
    assert resp.json() == {"status": "not implemented"}
```

- [ ] **Step 2: 執行測試，確認 FAIL**

```bash
uv run pytest tests/test_main.py -v
```

Expected: `ModuleNotFoundError: No module named 'main'`

- [ ] **Step 3: 建立空骨架模組**

```python
# hls_manager.py
# Phase 2 實作
```

```python
# inference/__init__.py
# Phase 3 實作
```

```python
# analysis/__init__.py
# Phase 5 實作
```

- [ ] **Step 4: 建立 main.py**

```python
from contextlib import asynccontextmanager

from fastapi import FastAPI

import database
from routers import alerts, notes, stream, tracking
from routers import settings as settings_router
from zmq_receiver import zmq_receiver


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()
    zmq_receiver.start()
    yield
    zmq_receiver.stop()
    await database.disconnect()


app = FastAPI(title="豬隻疾病監測系統", lifespan=lifespan)


@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok"}


app.include_router(stream.router)
app.include_router(tracking.router)
app.include_router(alerts.router)
app.include_router(settings_router.router)
app.include_router(notes.router)
```

- [ ] **Step 5: 執行 main.py 的測試，確認 PASS**

```bash
uv run pytest tests/test_main.py -v
```

Expected:
```
tests/test_main.py::test_health_returns_ok PASSED
tests/test_main.py::test_stream_live_returns_stub PASSED
tests/test_main.py::test_stream_vod_returns_stub PASSED
tests/test_main.py::test_tracking_returns_stub PASSED
tests/test_main.py::test_alerts_returns_stub PASSED
tests/test_main.py::test_settings_get_returns_stub PASSED
tests/test_main.py::test_notes_get_returns_stub PASSED
7 passed
```

- [ ] **Step 6: 執行完整測試套件**

```bash
uv run pytest -v
```

Expected: config（6）+ database（4）+ zmq_receiver（3）+ main（7）= 20 tests PASSED

（database 測試需 Docker PostgreSQL 執行中）

- [ ] **Step 7: Commit**

```bash
git add main.py hls_manager.py inference/__init__.py analysis/__init__.py tests/test_main.py
git commit -m "feat: add FastAPI app with lifespan and all stub modules"
```

---

## Task 8: Integration Smoke Test

手動驗證所有 Phase 1 完成條件。

- [ ] **Step 1: 確認 PostgreSQL 運行中**

```bash
docker compose ps
```

Expected: postgres 服務狀態為 `running`

- [ ] **Step 2: 啟動 FastAPI**

```bash
uv run uvicorn main:app --reload --log-level info
```

Expected（啟動訊息包含）:
```
INFO:     Application startup complete.
```
以及 loguru 輸出：
```
INFO     | zmq_receiver | ZMQ receiver started
INFO     | database     | Database pool created
```

- [ ] **Step 3: 驗證 /health**

另開終端機：

```bash
curl -s http://localhost:8000/health
```

Expected: `{"status":"ok"}`

- [ ] **Step 4: 驗證 stub endpoints**

```bash
curl -s http://localhost:8000/stream/cam_01/live
curl -s http://localhost:8000/tracking/cam_01
curl -s http://localhost:8000/alerts
curl -s http://localhost:8000/settings
curl -s http://localhost:8000/notes
```

Expected: 每個都回傳 `{"status":"not implemented"}`

- [ ] **Step 5: 驗證 ZMQ log（RPi 需在執行中）**

確認 RPi 的 `rpi_sender.py` 正在執行，觀察 uvicorn log 輸出：

Expected（每收到一個 frame）:
```
INFO     | zmq_receiver:_run | [rpi_sensors] frame=N ts=1234567890.123 rgb=12345B thermal=456B
```

- [ ] **Step 6: 確認優雅關閉**

在 uvicorn 終端按 Ctrl+C，確認 log 包含：

```
INFO     | zmq_receiver:stop | ZMQ receiver stopped
INFO     | database:disconnect | Database pool closed
```

- [ ] **Step 7: Commit（若 Step 5–6 需要修正）**

```bash
git add -p
git commit -m "fix: <說明修正內容>"
```

若無修正，執行：

```bash
git commit --allow-empty -m "chore: Phase 1 smoke test passed"
```

---

## Phase 1 完成條件檢查清單

- [ ] `docker compose up -d` 後四張表存在，預設設定已 INSERT
- [ ] `uv run uvicorn main:app --reload` 啟動成功，無 ImportError
- [ ] `GET /health` → `{"status": "ok"}`
- [ ] ZMQ receiver 收到 RPi 封包時 log 可見
- [ ] 所有 router endpoint 回傳 `{"status": "not implemented"}`
- [ ] `uv run pytest -v` 全部 PASS（20 tests）
- [ ] Ctrl+C 關閉後無 thread leak 或 hang
