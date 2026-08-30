"""操作手冊：把 docs/manual.md 渲染成一頁 HTML。

手冊寫成 Markdown 而不是手寫 HTML，是因為它之後每加一個功能就要補一段，
Markdown 讓改手冊不必碰標籤。渲染放後端而不是往「零 build」的前端塞一個
第三方 JS 檔，也是同一個理由。

刻意不放進登入保護（見 auth_middleware._PUBLIC_EXACT）：有人卡在登入頁時，
手冊正好是他該看的東西，而且裡面不含任何場域資料。
"""
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from markdown_it import MarkdownIt

router = APIRouter(tags=["manual"])

MANUAL_PATH = Path(__file__).resolve().parent.parent / "docs" / "manual.md"

# mtime → 渲染結果。手冊很少改，但改了要能直接重整看到，不必重啟。
_CACHE: dict[float, str] = {}

_MISSING = (
    "<h1>操作手冊</h1><p>找不到手冊檔案（<code>docs/manual.md</code>）。"
    "這通常代表部署時漏了 <code>docs/</code> 目錄。</p>"
)

_PAGE = """<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>操作手冊 — 豬隻監測系統</title>
<link rel="icon" href="/static/favicon.ico" sizes="any">
<link rel="stylesheet" href="/static/css/app.css">
<style>
  .manual-page {{
    max-width: 46rem; margin: 0 auto; padding: 2rem 1.25rem 6rem;
    line-height: 1.75;
  }}
  .manual-page h1 {{ margin-bottom: 0.25em; }}
  .manual-page h2 {{ margin-top: 2.5em; padding-top: 0.75em;
                     border-top: 1px solid var(--border, #333); }}
  .manual-page h3 {{ margin-top: 1.75em; }}
  .manual-page table {{ width: 100%; border-collapse: collapse; margin: 1em 0;
                        display: block; overflow-x: auto; }}
  .manual-page th, .manual-page td {{
    border: 1px solid var(--border, #333); padding: 0.4em 0.6em; text-align: left;
  }}
  .manual-page code {{ padding: 0.1em 0.35em; border-radius: 3px;
                       background: var(--surface-2, #222); }}
  .manual-back {{ display: inline-block; margin-bottom: 1.5rem; }}
</style>
</head>
<body>
<article class="manual-page">
<a class="manual-back" href="/">← 回到監測畫面</a>
{body}
</article>
</body>
</html>
"""


def render_manual() -> str:
    try:
        mtime = MANUAL_PATH.stat().st_mtime
    except OSError:
        return _PAGE.format(body=_MISSING)
    cached = _CACHE.get(mtime)
    if cached is None:
        md = MarkdownIt("commonmark", {"html": False}).enable("table")
        cached = _PAGE.format(body=md.render(MANUAL_PATH.read_text(encoding="utf-8")))
        _CACHE.clear()          # 只留最新一版，手冊改了舊的沒有用
        _CACHE[mtime] = cached
    return cached


@router.get("/manual", response_class=HTMLResponse)
async def get_manual():
    return HTMLResponse(render_manual())
