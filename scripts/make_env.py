#!/usr/bin/env python3
"""互動式產生 `.env`，以及檢查既有 `.env` 有沒有踩到已知的坑。

    uv run python scripts/make_env.py            # 互動式產生
    uv run python scripts/make_env.py --check    # 只檢查現有 .env，不修改任何東西

為什麼需要這支：把 `.env` 從別台機器複製過來是最常見的部署方式，也是最常出事的
一步——`HLS_BASE_DIR` 指著別台的路徑、`MOT_WORKER_THREADS` 沿用別台的核心數、
相機位址在這個網段連不到。這些都不會讓服務起不來，只會讓它「跑起來但什麼都沒有」，
而那種故障最難查。所以這支的重點不是省下打字，是**每一項都當場驗證**。

`--check` 適合放進部署流程或開機前的健檢；有 error 會以非 0 結束。

驗證邏輯全部是純函式（`check_*`），互動的部分只負責問與印，方便測試。
"""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from config import ZmqSource  # noqa: E402

OK, WARN, ERROR = "ok", "warn", "error"
_MARK = {OK: "✅", WARN: "⚠ ", ERROR: "❌"}


@dataclass(frozen=True)
class Finding:
    level: str
    field: str
    message: str

    def __str__(self) -> str:
        return f"{_MARK[self.level]} {self.field}：{self.message}"


# ════════════════════════════════════════════════════════════════
# 驗證器（純函式，不做 I/O 以外的事，也不印東西）
# ════════════════════════════════════════════════════════════════

def probe_tcp(host: str, port: int, timeout: float = 3.0) -> bool:
    """能不能跟 host:port 建立 TCP 連線。失敗一律回 False，不拋例外。"""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def check_zmq_sources(raw: str, *, probe=probe_tcp) -> list[Finding]:
    """格式先用 config.ZmqSource.from_str 驗（跟正式程式同一套解析），再逐一探連通性。

    連不到只算 warn 不算 error：相機可能只是暫時斷線，不該因此擋住產生 .env。
    但一定要講出來——服務會起來、網頁會通、就是一幀資料都沒有，這種故障最難查。
    """
    raw = raw.strip()
    if not raw:
        return [Finding(ERROR, "ZMQ_SOURCES", "不能空白，沒有來源就沒有任何影像")]

    findings: list[Finding] = []
    sources: list[ZmqSource] = []
    for chunk in [c for c in raw.split(";") if c.strip()]:
        try:
            sources.append(ZmqSource.from_str(chunk))
        except ValueError as e:
            findings.append(Finding(ERROR, "ZMQ_SOURCES", str(e).replace("\n", " ")))
    if not sources:
        return findings

    labels = [s.label for s in sources]
    dupes = {x for x in labels if labels.count(x) > 1}
    if dupes:
        findings.append(Finding(
            ERROR, "ZMQ_SOURCES",
            f"label 重複：{', '.join(sorted(dupes))}。label 是相機在整個系統裡的身分，"
            "重複會讓兩路影像互相蓋掉"))

    for s in sources:
        if probe(s.src_host, s.src_port):
            findings.append(Finding(OK, f"ZMQ {s.label}", f"{s.src_host}:{s.src_port} 連得到"))
        else:
            findings.append(Finding(
                WARN, f"ZMQ {s.label}",
                f"{s.src_host}:{s.src_port} 連不到。相機斷線、網段不通、或還沒加入 VPN"))
    return findings


def check_writable_dir(path: str, field: str) -> list[Finding]:
    """目錄存在嗎、寫得進去嗎。不存在時檢查上層能不能建。"""
    if not path.strip():
        return [Finding(ERROR, field, "不能空白")]
    p = Path(path).expanduser()
    if p.exists():
        if not p.is_dir():
            return [Finding(ERROR, field, f"{p} 存在但不是目錄")]
        if not os.access(p, os.W_OK):
            return [Finding(ERROR, field, f"{p} 不可寫")]
        return [Finding(OK, field, f"{p} 存在且可寫")]

    parent = next((a for a in p.parents if a.exists()), None)
    if parent is None or not os.access(parent, os.W_OK):
        return [Finding(ERROR, field, f"{p} 不存在，而且上層 {parent} 也建不了")]
    return [Finding(WARN, field, f"{p} 還不存在，會在第一次錄影時建立（上層 {parent} 可寫）")]


def check_recording_disk(path: str, *, min_free_gb: float = 100.0,
                         root_path: str = "/") -> list[Finding]:
    """錄影碟的剩餘空間，以及有沒有跟系統碟共用。

    共用只給 warn：能跑，但 storage_monitor 的低空間保護就保護不到東西了——
    系統碟被別的程式吃掉一樣會讓錄影停擺。

    `root_path` 可注入是為了測試：不同機器的 /tmp 可能是 tmpfs、也可能就在系統碟上，
    拿環境當前提的測試會時好時壞。
    """
    p = Path(path).expanduser()
    probe_at = p if p.exists() else next((a for a in p.parents if a.exists()), Path("/"))
    try:
        usage = shutil.disk_usage(probe_at)
    except OSError as e:
        return [Finding(WARN, "HLS_BASE_DIR", f"讀不到 {probe_at} 的空間資訊：{e}")]

    free_gb = usage.free / 1024 ** 3
    findings = []
    if free_gb < min_free_gb:
        findings.append(Finding(
            WARN, "HLS_BASE_DIR",
            f"剩 {free_gb:.0f}G，低於 storage_min_free_gb 的預設門檻 {min_free_gb:.0f}G，"
            "一開始錄影就會判定空間不足並切到 ephemeral"))
    else:
        findings.append(Finding(OK, "HLS_BASE_DIR", f"剩 {free_gb:.0f}G"))

    try:
        if os.stat(probe_at).st_dev == os.stat(root_path).st_dev:
            findings.append(Finding(
                WARN, "HLS_BASE_DIR",
                "跟系統碟是同一顆。能跑，但系統碟被別的東西吃掉時錄影會一起停擺，"
                "低空間保護等於保護不到東西"))
    except OSError:
        pass
    return findings


def check_model_files(*paths_and_fields: tuple[str, str]) -> list[Finding]:
    """權重與設定檔在不在。相對路徑以 repo 根目錄為基準（正式程式也是這樣解）。"""
    findings = []
    for value, field in paths_and_fields:
        if not value.strip():
            findings.append(Finding(ERROR, field, "不能空白"))
            continue
        p = Path(value).expanduser()
        if not p.is_absolute():
            p = _REPO_ROOT / p
        if p.is_file():
            size = p.stat().st_size / 1024 ** 2
            findings.append(Finding(OK, field, f"{p.name} 存在（{size:.0f} MB）"))
        else:
            findings.append(Finding(
                ERROR, field,
                f"{p} 不存在。ref/ 與模型權重都被 gitignore，clone 拿不到，"
                "要從既有機器 rsync 過來"))
    return findings


def check_worker_threads(value: str, *, cpu_count: int | None = None) -> list[Finding]:
    cpu = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    try:
        n = int(value)
    except ValueError:
        return [Finding(ERROR, "MOT_WORKER_THREADS", f"必須是整數，收到 '{value}'")]
    if n < 1:
        return [Finding(ERROR, "MOT_WORKER_THREADS", "至少要 1")]
    if n > cpu:
        return [Finding(ERROR, "MOT_WORKER_THREADS",
                        f"{n} 超過這台機器的核心數 {cpu}")]
    if n > cpu * 0.75:
        return [Finding(WARN, "MOT_WORKER_THREADS",
                        f"{n} 已經吃掉 {cpu} 核的大半，ffmpeg 會搶不到核心。"
                        f"建議 {suggest_worker_threads(cpu)}")]
    return [Finding(OK, "MOT_WORKER_THREADS", f"{n}（共 {cpu} 核）")]


def suggest_worker_threads(cpu_count: int | None = None) -> int:
    """核心數的三分之二，留 headroom 給 ffmpeg 與作業系統。至少 1。"""
    cpu = cpu_count if cpu_count is not None else (os.cpu_count() or 1)
    return max(1, int(cpu * 2 / 3))


def check_device(value: str) -> list[Finding]:
    """`cuda` 但實際沒有 GPU 是 error——推論會在啟動時就炸，不是慢一點而已。"""
    v = value.strip().lower()
    if v not in ("cuda", "cpu"):
        return [Finding(ERROR, "DEVICE", f"只接受 cuda 或 cpu，收到 '{value}'")]
    if v == "cpu":
        return [Finding(WARN, "DEVICE", "用 CPU 推論，實務上跟不上多路即時影像")]
    try:
        import torch
    except ImportError:
        return [Finding(WARN, "DEVICE", "torch 還沒裝，無法確認 CUDA 可用（先跑 uv sync）")]
    if not torch.cuda.is_available():
        return [Finding(ERROR, "DEVICE",
                        "設成 cuda 但 torch.cuda.is_available() 是 False。"
                        "檢查 NVIDIA driver 與 torch 的 CUDA 版本")]
    return [Finding(OK, "DEVICE", f"cuda（{torch.cuda.get_device_name(0)}）")]


def check_database_url(url: str, *, probe=probe_tcp) -> list[Finding]:
    """只驗格式與 TCP 連通性，不真的登入——這支不該需要資料庫密碼以外的權限。"""
    if not url.startswith("postgresql://"):
        return [Finding(ERROR, "DATABASE_URL", "必須以 postgresql:// 開頭")]
    try:
        hostport = url.split("@", 1)[1].split("/", 1)[0]
        host, _, port_s = hostport.partition(":")
        port = int(port_s) if port_s else 5432
    except (IndexError, ValueError):
        return [Finding(ERROR, "DATABASE_URL",
                        "解析不出 host:port，格式應為 postgresql://user:pw@host:port/db")]
    if probe(host, port):
        return [Finding(OK, "DATABASE_URL", f"{host}:{port} 連得到")]
    return [Finding(WARN, "DATABASE_URL",
                    f"{host}:{port} 連不到。還沒 docker compose up -d 的話這是正常的")]


def parse_env_file(text: str) -> dict[str, str]:
    """讀 .env 成 dict。只認 `KEY=value`，忽略註解與空行；不處理引號跳脫
    （`.env` 的值本來就不該有引號，config 那邊也不解）。"""
    out: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip()
    return out


def check_env(values: dict[str, str], *, probe=probe_tcp) -> list[Finding]:
    """把一份 .env 的內容整個檢查一遍。`--check` 與互動流程的最後複查都走這裡。"""
    findings: list[Finding] = []
    findings += check_zmq_sources(values.get("ZMQ_SOURCES", ""), probe=probe)
    hls = values.get("HLS_BASE_DIR", "")
    findings += check_writable_dir(hls, "HLS_BASE_DIR")
    if hls.strip():
        findings += check_recording_disk(hls)
    findings += check_model_files(
        (values.get("MODEL_WEIGHTS", ""), "MODEL_WEIGHTS"),
        (values.get("MODEL_CONFIG_PATH", ""), "MODEL_CONFIG_PATH"),
    )
    findings += check_worker_threads(values.get("MOT_WORKER_THREADS", "0"))
    findings += check_device(values.get("DEVICE", "cuda"))
    findings += check_database_url(values.get("DATABASE_URL", ""), probe=probe)

    if values.get("AUTH_ENABLED", "").strip().lower() == "true":
        if not values.get("AUTH_PASSWORD_HASH", "").startswith("scrypt$"):
            findings.append(Finding(ERROR, "AUTH_PASSWORD_HASH",
                                    "AUTH_ENABLED=true 但沒有有效的密碼雜湊，沒有人登得進去"))
        if len(values.get("AUTH_SESSION_SECRET", "")) < 32:
            findings.append(Finding(ERROR, "AUTH_SESSION_SECRET",
                                    "太短或沒設。空的話每次重啟都會換一把、所有人被登出"))
        if values.get("AUTH_COOKIE_SECURE", "").strip().lower() == "true":
            findings.append(Finding(WARN, "AUTH_COOKIE_SECURE",
                                    "設成 true 表示 cookie 只在 HTTPS 下送出。"
                                    "前面還沒有 TLS 的話沒有人登得進去"))
    return findings


def worst_level(findings: list[Finding]) -> str:
    if any(f.level == ERROR for f in findings):
        return ERROR
    if any(f.level == WARN for f in findings):
        return WARN
    return OK


# ════════════════════════════════════════════════════════════════
# 互動
# ════════════════════════════════════════════════════════════════

def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    got = input(f"{prompt}{suffix}: ").strip()
    return got or default


def _ask_validated(prompt: str, default: str, validator) -> str:
    """問到過為止。只有 error 會擋，warn 印出來就放行——很多 warn（相機暫時斷線、
    資料庫還沒起來）在產生 .env 的當下本來就無法排除。"""
    while True:
        value = _ask(prompt, default)
        findings = validator(value)
        for f in findings:
            print(f"    {f}")
        if not any(f.level == ERROR for f in findings):
            return value
        print("    ↑ 有 error，請重新輸入（Ctrl-C 放棄）\n")


def _section(title: str) -> None:
    print(f"\n── {title} " + "─" * max(0, 56 - len(title)))


def interactive(target: Path, example: Path) -> int:
    print("=== pig-agri .env 產生器 ===\n")
    print("每一項都會當場驗證。空白就用中括號裡的預設值。\n")

    if target.exists():
        print(f"⚠ {target} 已經存在。")
        if _ask("要備份後覆蓋嗎？(yes/no)", "no").lower() not in ("y", "yes"):
            print("沒有動任何東西。要檢查現有設定可以跑：--check")
            return 1
        backup = target.with_suffix(f".bak-{datetime.now():%Y%m%d-%H%M%S}")
        shutil.copy2(target, backup)
        print(f"  已備份到 {backup}")

    defaults = parse_env_file(example.read_text()) if example.exists() else {}
    v: dict[str, str] = {}

    _section("相機來源")
    print("  格式：name:host:port:src_topic:label，多個用分號分隔")
    v["ZMQ_SOURCES"] = _ask_validated(
        "  ZMQ_SOURCES", defaults.get("ZMQ_SOURCES", ""),
        lambda s: check_zmq_sources(s))
    v["ZMQ_WARMUP_SECS"] = defaults.get("ZMQ_WARMUP_SECS", "0.5")
    v["ZMQ_STALE_MS"] = defaults.get("ZMQ_STALE_MS", "30000")

    _section("錄影儲存")
    print("  建議指到獨立的一顆碟，不要跟系統碟共用")
    v["HLS_BASE_DIR"] = _ask_validated(
        "  HLS_BASE_DIR", defaults.get("HLS_BASE_DIR", "data/pig_monitoring/hls"),
        lambda s: check_writable_dir(s, "HLS_BASE_DIR") + (check_recording_disk(s) if s.strip() else []))
    v["HLS_RETENTION_DAYS"] = _ask("  HLS_RETENTION_DAYS（保留幾天）",
                                   defaults.get("HLS_RETENTION_DAYS", "90"))
    v["HLS_TARGET_FPS"] = defaults.get("HLS_TARGET_FPS", "25")
    v["HLS_FRAME_BUFFER_SIZE"] = defaults.get("HLS_FRAME_BUFFER_SIZE", "10")

    _section("資料庫")
    v["DATABASE_URL"] = _ask_validated(
        "  DATABASE_URL",
        defaults.get("DATABASE_URL", "postgresql://pig:pig_password@localhost:15432/pig_monitoring"),
        lambda s: check_database_url(s))

    _section("推論")
    v["MODEL_WEIGHTS"] = _ask_validated(
        "  MODEL_WEIGHTS", defaults.get("MODEL_WEIGHTS", "./ref/HybridSORT/pretrained/best_ckpt.pth.tar"),
        lambda s: check_model_files((s, "MODEL_WEIGHTS")))
    v["MODEL_CONFIG_PATH"] = _ask_validated(
        "  MODEL_CONFIG_PATH",
        defaults.get("MODEL_CONFIG_PATH",
                     "./ref/HybridSORT/exps/example/mot/yolox_oink_test_hybrid_sort_reid.py"),
        lambda s: check_model_files((s, "MODEL_CONFIG_PATH")))
    v["DEVICE"] = _ask_validated("  DEVICE (cuda/cpu)", defaults.get("DEVICE", "cuda"),
                                 lambda s: check_device(s))
    v["MOT_WORKER_THREADS"] = _ask_validated(
        "  MOT_WORKER_THREADS", str(suggest_worker_threads()),
        lambda s: check_worker_threads(s))

    _section("其他")
    v["JPEG_QUALITY"] = defaults.get("JPEG_QUALITY", "70")
    v["LOG_LEVEL"] = defaults.get("LOG_LEVEL", "INFO")
    v["FFMPEG_LOG_LEVEL"] = defaults.get("FFMPEG_LOG_LEVEL", "error")
    for key, dflt in (("ANALYSIS_INTERVAL_MINUTES", "30"), ("ANALYSIS_WINDOW_HOURS", "6"),
                      ("ANOMALY_STD_THRESHOLD", "3.0"), ("ANOMALY_MIN_SAMPLES", "50")):
        v[key] = defaults.get(key, dflt)
    print("  影像品質、log 等級、分析排程沿用 .env.example 的值，之後可以在前端設定頁改")

    _section("登入")
    print("  反向代理在別台機器、服務要綁 0.0.0.0 時，這個一定要開")
    if _ask("  要開啟登入嗎？(yes/no)", "no").lower() in ("y", "yes"):
        import getpass
        import secrets
        from auth import hash_password

        username = _ask("  帳號", "pig")
        while True:
            pw = getpass.getpass("  密碼（至少 12 字元，不會顯示）：")
            if len(pw) < 12:
                print("    ❌ 太短了")
                continue
            if pw != getpass.getpass("  再輸入一次："):
                print("    ❌ 兩次不一致")
                continue
            break
        v["AUTH_ENABLED"] = "true"
        v["AUTH_USERNAME"] = username
        v["AUTH_PASSWORD_HASH"] = hash_password(pw)
        v["AUTH_SESSION_SECRET"] = secrets.token_urlsafe(32)
        has_tls = _ask("  前面已經有 TLS 反向代理了嗎？(yes/no)", "no").lower() in ("y", "yes")
        v["AUTH_COOKIE_SECURE"] = "true" if has_tls else "false"
        v["AUTH_TRUST_FORWARDED_FOR"] = "true" if _ask(
            "  前面有反向代理嗎？(yes/no)", "no").lower() in ("y", "yes") else "false"
        if not has_tls:
            print("    ⚠  AUTH_COOKIE_SECURE=false —— 有 TLS 之後記得改回 true")
    else:
        v["AUTH_ENABLED"] = "false"
        v["AUTH_TRUST_FORWARDED_FOR"] = "false"

    target.write_text(_render(v), encoding="utf-8")
    print(f"\n已寫入 {target}\n")

    _section("整份複查")
    findings = check_env(v)
    for f in findings:
        print(f"  {f}")
    level = worst_level(findings)
    print()
    if level == ERROR:
        print("❌ 還有 error，服務起不來。修好再跑一次 --check。")
        return 1
    if level == WARN:
        print("⚠  有 warning。多半是相機還沒接上或資料庫還沒起來，確認一下是不是預期內。")
    else:
        print("✅ 全部通過。")
    print("\n下一步：docker compose up -d && uv run uvicorn main:app --host 127.0.0.1 --port 5005")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="產生或檢查 pig-agri 的 .env")
    ap.add_argument("--check", action="store_true", help="只檢查現有 .env，不修改任何東西")
    ap.add_argument("--file", default=str(_REPO_ROOT / ".env"), help="要產生/檢查的檔案路徑")
    args = ap.parse_args()

    target = Path(args.file)
    if args.check:
        if not target.exists():
            print(f"❌ 找不到 {target}")
            return 1
        findings = check_env(parse_env_file(target.read_text()))
        print(f"=== 檢查 {target} ===\n")
        for f in findings:
            print(f"  {f}")
        level = worst_level(findings)
        print()
        print({OK: "✅ 全部通過。", WARN: "⚠  有 warning，確認是不是預期內。",
               ERROR: "❌ 有 error，服務會起不來或收不到資料。"}[level])
        return 1 if level == ERROR else 0

    try:
        return interactive(target, _REPO_ROOT / ".env.example")
    except KeyboardInterrupt:
        print("\n已放棄，沒有寫入任何東西。")
        return 130


def _render(v: dict[str, str]) -> str:
    """照 .env.example 的分節順序輸出，人看得懂、之後也好手動改。"""
    def block(title: str, keys: list[str]) -> str:
        lines = [f"# ── {title} " + "─" * max(0, 50 - len(title))]
        lines += [f"{k}={v[k]}" for k in keys if k in v]
        return "\n".join(lines)

    parts = [
        f"# 由 scripts/make_env.py 產生於 {datetime.now():%Y-%m-%d %H:%M:%S}",
        block("相機來源", ["ZMQ_SOURCES", "ZMQ_WARMUP_SECS", "ZMQ_STALE_MS"]),
        block("錄影儲存", ["HLS_BASE_DIR", "HLS_RETENTION_DAYS",
                           "HLS_TARGET_FPS", "HLS_FRAME_BUFFER_SIZE"]),
        block("資料庫", ["DATABASE_URL"]),
        block("推論", ["MODEL_WEIGHTS", "MODEL_CONFIG_PATH", "DEVICE", "MOT_WORKER_THREADS"]),
        block("影像與 log", ["JPEG_QUALITY", "LOG_LEVEL", "FFMPEG_LOG_LEVEL"]),
        block("分析排程", ["ANALYSIS_INTERVAL_MINUTES", "ANALYSIS_WINDOW_HOURS",
                           "ANOMALY_STD_THRESHOLD", "ANOMALY_MIN_SAMPLES"]),
        block("登入", ["AUTH_ENABLED", "AUTH_USERNAME", "AUTH_PASSWORD_HASH",
                       "AUTH_SESSION_SECRET", "AUTH_COOKIE_SECURE", "AUTH_TRUST_FORWARDED_FOR"]),
    ]
    return "\n\n".join(parts) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
