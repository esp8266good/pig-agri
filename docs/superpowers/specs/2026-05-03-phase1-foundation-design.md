# Phase 1 — 地基：設計規格

**日期：** 2026-05-03  
**範圍：** PostgreSQL schema 建立 + FastAPI skeleton + ZMQ receiver（只 log）

---

## 背景

農場的 Raspberry Pi 透過 ZeroMQ（topic: `rpi_sensors`）推送 RGB + Thermal 影像串流。
Phase 1 建立整個系統的地基：資料庫 schema、FastAPI 應用骨架、ZMQ 接收並記錄 log。
推論、HLS 串流、分析排程均留到後續 Phase 填入。

---

## 關鍵決策

| 問題 | 決策 | 理由 |
|------|------|------|
| 後端目錄 | 專案根目錄（`./main.py`）| 直接執行，不額外分層 |
| Python 環境 | `uv` 新建環境 | 套件管理一致，方便打包 |
| DB 連線 | asyncpg（async pool）| 符合 FastAPI async 慣例 |
| ZMQ 執行模型 | 獨立 daemon thread | 沿用現有 `run_realtime_track.py` 架構 |
| PostgreSQL | Docker Compose（named volume）| 方便移植與重建 |

---

## 目錄結構

```
pig-agri/
├── docker-compose.yml           # PostgreSQL stack
├── sql/
│   └── init.sql                 # Schema + 預設設定 INSERT
├── .env                         # 執行期設定（不入 git）
├── .env.example                 # 設定範本（入 git）
├── pyproject.toml               # uv 依賴管理
├── main.py                      # FastAPI app + lifespan
├── config.py                    # pydantic-settings
├── database.py                  # asyncpg pool
├── zmq_receiver.py              # ZMQ SUB daemon thread
├── hls_manager.py               # 空骨架（Phase 2）
├── inference/
│   └── __init__.py              # 空骨架（Phase 3）
├── analysis/
│   └── __init__.py              # 空骨架（Phase 5）
└── routers/
    ├── stream.py                # 空骨架
    ├── tracking.py              # 空骨架
    ├── alerts.py                # 空骨架
    └── settings.py              # 空骨架
```

Phase 1 有實質內容的檔案：`main.py`、`config.py`、`database.py`、`zmq_receiver.py`、`sql/init.sql`、`docker-compose.yml`。

---

## Docker Compose

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

- Schema 由 `sql/init.sql` 在容器首次啟動時自動執行
- `pg_data` named volume 保留資料，容器重建後不遺失
- 不預設包含 pgAdmin（需要時以 `--profile admin` 選擇性啟動）

---

## 資料庫 Schema（`sql/init.sql`）

完整 schema 如 `fullstack-spec.md` 所定義，包含以下四張表：

```sql
-- 追蹤紀錄（主表，Phase 3 開始寫入）
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

-- 健康警示（Phase 5 開始寫入）
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

-- 人工備註（未來功能，先建表）
CREATE TABLE IF NOT EXISTS pig_notes (
    id          BIGSERIAL PRIMARY KEY,
    camera_id   VARCHAR(16) NOT NULL,
    object_id   INTEGER,
    note_time   TIMESTAMPTZ NOT NULL,
    content     TEXT,
    created_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 使用者設定
CREATE TABLE IF NOT EXISTS user_settings (
    key        VARCHAR(64) PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ DEFAULT NOW()
);

INSERT INTO user_settings VALUES
    ('jpeg_quality', '70', NOW()),
    ('analysis_interval_minutes', '30', NOW()),
    ('anomaly_std_threshold', '3.0', NOW()),
    ('hls_retention_days', '90', NOW())
ON CONFLICT (key) DO NOTHING;
```

---

## FastAPI 骨架（`main.py`）

```python
@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.connect()     # asyncpg pool 建立
    zmq_receiver.start()         # ZMQ daemon thread 啟動
    yield
    zmq_receiver.stop()          # 等待 ZMQ thread 結束（timeout=3s）
    await database.disconnect()  # asyncpg pool 關閉

app = FastAPI(title="豬隻疾病監測系統", lifespan=lifespan)
app.include_router(stream.router)
app.include_router(tracking.router)
app.include_router(alerts.router)
app.include_router(settings.router)
```

每個 router 的 endpoint 一律回傳 `{"status": "not implemented"}`，為後續 Phase 提供掛載點。

健康檢查：`GET /health` → `{"status": "ok"}`，用於確認服務可達。

---

## ZMQ Receiver（`zmq_receiver.py`）

Phase 1 行為：**收到 frame 就 log，不做任何分發**。

```
ZMQ SUB socket
  ├── 訂閱 settings.camera_topics（cam_01~cam_06）
  └── 訂閱 b"rpi_sensors"（相容現有 RPi sender）

每收到一個 frame：
  logger.info(f"[{topic}] frame={frame_id} ts={ts:.3f} rgb={N}B thermal={M}B")
```

- 使用 `sock.poll(100ms)` 非阻塞，避免 stop() 時卡住
- Phase 2 在此加入 `hls_manager.feed(camera_id, rgb_bytes)`
- Phase 3 在此加入 `inference_queue.put((camera_id, frame_id, ts, rgb, thermal))`

**ZMQ 連線設定：**
- `rpi_ip`：從 `.env` 讀取（新增 `RPI_IP` 設定項）
- `zmq_port`：5555
- connect 模式（server 端在 RPi 用 bind）

---

## 設定（`config.py` + `.env`）

`.env` 新增 `RPI_IP` 設定項（規格未列但 Phase 1 必要）：

```env
DATABASE_URL=postgresql://pig:pig_password@localhost:15432/pig_monitoring
ZMQ_PORT=5555
RPI_IP=100.73.233.110
CAMERA_TOPICS=cam_01,cam_02,cam_03,cam_04,cam_05,cam_06
HLS_BASE_DIR=/data/pig_monitoring/hls
HLS_RETENTION_DAYS=90
JPEG_QUALITY=70
MODEL_WEIGHTS=./ref/HybridSORT/pretrained/best_ckpt.pth.tar
MODEL_CONFIG=./ref/HybridSORT/exps/example/mot/yolox_oink_test_hybrid_sort_reid.py
DEVICE=cuda
MOT_WORKER_THREADS=20
ANALYSIS_INTERVAL_MINUTES=30
ANALYSIS_WINDOW_HOURS=6
ANOMALY_STD_THRESHOLD=3.0
ANOMALY_MIN_SAMPLES=50
```

---

## uv 依賴（`pyproject.toml`）

Phase 1 所需套件：

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
```

PyTorch、yolox、HybridSORT 等推論依賴留到 Phase 3 加入。

---

## Phase 1 完成條件

1. `docker compose up -d` 後 PostgreSQL 可連線，四張表存在
2. `uv run uvicorn main:app --reload` 啟動成功，無 import error
3. `GET /health` 回傳 `{"status": "ok"}`
4. ZMQ receiver 收到 RPi 封包時，在 log 看到 `[rpi_sensors] frame=N ts=...` 訊息
5. 所有 router endpoint 回傳 `{"status": "not implemented"}`

---

## 後續 Phase 銜接點

| Phase | 在哪裡擴充 |
|-------|-----------|
| Phase 2（HLS）| `zmq_receiver.py` 加 `hls_manager.feed()`；填入 `hls_manager.py` |
| Phase 3（MOT）| `zmq_receiver.py` 加 `inference_queue.put()`；填入 `inference/` |
| Phase 4（歷史）| 填入 `routers/tracking.py`；使用 `database.py` pool 查詢 |
| Phase 5（分析）| 填入 `analysis/scheduler.py` |
| Phase 6（設定）| 填入 `routers/settings.py` |
