#!/usr/bin/env python3
"""用 headless 瀏覽器驗前端：載得起來、沒有 console 錯誤、互動真的會動。

為什麼需要這一層：`uv run pytest` 與 `./scripts/check_js.sh` **都不載入瀏覽器**。
前者測後端，後者只確認每個 ES module 的語法過得了 parser。所以「選單被別的
listener 搶走」「details 展開了但高度是 0」「說明模式下按鈕點不動」這類症狀
兩層都抓不到，只能靠人點——於是就會發生推上去才發現壞了。

用法：
    uv run python scripts/check_frontend_ux.py --url http://127.0.0.1:18321 --mode polished
    uv run python scripts/check_frontend_ux.py --url http://192.168.50.48:5005 --mode baseline
    uv run python scripts/check_frontend_ux.py --url ... --viewport 390x844

退出碼 0 = 沒有 ❌。⚠ SKIP（前置條件不成立，例如沒有相機在送幀）不影響退出碼，
但會印出原因。

⚠ 唯讀約束：這支腳本會對正在跑的正式服務執行，所以**只准讀**。
   不 PUT /settings、不存遮罩、不刪錄影、不發通知。要驗「設定值讀得回來」
   只斷言 input 的 value 非空，不要改值再存；點 #mask-edit-btn 只驗編輯器
   開得起來，不要按儲存。加新斷言時守住這條。
"""
import argparse
import re
import sys

from playwright.sync_api import sync_playwright

# 這兩個是 18405aa「拉開 vod/thermal 色相」改的值。改回舊值或打錯字這裡會紅。
EXPECTED_VARS = {"--thermal": "#ff7043", "--vod": "#e6b23c"}

# 設定抽屜裡 7 組手風琴的順序，跟 index.html 的 <details> 順序一致。
SETTINGS_GROUPS = [
    "異常分析", "關注清單", "遮罩", "錄影排程", "儲存空間", "推播通知", "進階",
]


class Report:
    """收斷言結果。ok=True → ✅，ok=False → ❌，ok=None → ⚠ 跳過（不影響退出碼）。"""

    def __init__(self) -> None:
        self.rows: list[tuple[bool | None, str, str, str]] = []

    def add(self, ok, num, name, detail) -> None:
        self.rows.append((ok, str(num), name, str(detail)))

    def check(self, num, name, cond, detail) -> bool:
        self.add(bool(cond), num, name, detail)
        return bool(cond)

    def skip(self, num, name, why) -> None:
        self.add(None, num, name, f"跳過：{why}")

    def failed(self) -> int:
        return sum(1 for ok, *_ in self.rows if ok is False)

    def dump(self) -> None:
        for ok, num, name, detail in self.rows:
            mark = "✅" if ok else ("❌" if ok is False else "⚠ ")
            print(f"{mark} #{num} {name}：{detail}")


def _rect(page, selector):
    return page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return null;
            const r = el.getBoundingClientRect();
            return {top: r.top, right: r.right, bottom: r.bottom, left: r.left,
                    width: r.width, height: r.height};
        }""",
        selector,
    )


def _hidden(page, selector):
    return page.evaluate(
        "(sel) => { const el = document.querySelector(sel); return el ? el.hidden : null; }",
        selector,
    )


def _more_menu_open(page) -> bool:
    return _hidden(page, "#more-menu") is False


def _open_more_menu(page) -> None:
    """把選單開起來。已經開著就不動（再點一次會關掉）。"""
    if not _more_menu_open(page):
        page.click("#more-btn")
        page.wait_for_timeout(120)


def _close_more_menu(page) -> None:
    if _more_menu_open(page):
        page.click("#more-btn")
        page.wait_for_timeout(120)


def _help_mode(page) -> bool:
    return page.evaluate("() => document.body.classList.contains('help-mode')")


def _set_help_mode(page, on: bool) -> None:
    """靠真的點按鈕切換，不直接改 state：要驗的正是「點得到嗎」。"""
    if _help_mode(page) == on:
        return
    _open_more_menu(page)
    page.click("#help-btn")
    page.wait_for_timeout(150)


def _btn_style(page, selector):
    return page.evaluate(
        """(sel) => {
            const el = document.querySelector(sel);
            if (!el) return null;
            const cs = getComputedStyle(el);
            return {color: cs.color, background: cs.backgroundColor,
                    border: cs.borderColor, outline: cs.outlineColor};
        }""",
        selector,
    )


def check_menu_position(page, rep, num, name):
    """選單開得起來、落在 viewport 內、掛在按鈕正下方且靠右對齊。"""
    _close_more_menu(page)
    _open_more_menu(page)
    if not _more_menu_open(page):
        rep.check(num, name, False, "點了 #more-btn 之後 #more-menu 仍是 hidden")
        return
    menu = _rect(page, "#more-menu")
    btn = _rect(page, "#more-btn")
    vw, vh = page.evaluate("() => [window.innerWidth, window.innerHeight]")
    in_view = (menu["left"] >= 0 and menu["top"] >= 0
               and menu["right"] <= vw and menu["bottom"] <= vh)
    below = menu["top"] >= btn["bottom"] - 1        # 1px 容忍：子像素捨入
    aligned = abs(menu["right"] - btn["right"]) < 40
    rep.check(
        num, name, in_view and below and aligned,
        f"menu={ {k: round(v) for k, v in menu.items()} } btn.bottom={round(btn['bottom'])} "
        f"btn.right={round(btn['right'])} viewport={vw}x{vh} "
        f"（落在畫面內={in_view} 在按鈕下方={below} 靠右對齊={aligned}）",
    )
    _close_more_menu(page)


def run_polished(page, rep) -> None:
    # ── 1 選單位置 ────────────────────────────────────────
    check_menu_position(page, rep, 1, "更多選單開得起來且位置正確")

    # ── 11 再按一次要關得掉 ──────────────────────────────
    # outside-click listener 掛在捕獲階段，比按鈕自己的 handler 早跑；它若把
    # 按鈕自己的點擊也當成「點外部」而先關掉選單，按鈕的 handler 就會看到
    # hidden=true 又開回來，於是按第二次沒反應。
    _open_more_menu(page)
    page.click("#more-btn")
    page.wait_for_timeout(200)
    rep.check(11, "再按一次 #more-btn 關得掉選單",
              _hidden(page, "#more-menu") is True,
              f"more-menu.hidden={_hidden(page, '#more-menu')}")

    # ── 2 說明模式開啟時 #help-btn 要看得出來是「開著的」 ──
    # 舊樣式 #help-btn[aria-pressed="true"] 只改 border-color，而選單裡的
    # .slot-action-menu button 是 border: none，沒有 border 可以上色。
    _open_more_menu(page)
    off_style = _btn_style(page, "#help-btn")
    _close_more_menu(page)
    _set_help_mode(page, True)
    _open_more_menu(page)
    on_style = _btn_style(page, "#help-btn")
    changed = off_style != on_style
    rep.check(2, "說明模式開啟時 #help-btn 有視覺回饋",
              changed, f"關={off_style} 開={on_style}")

    # ── 4 說明模式下 #more-btn 仍打得開（PASSTHROUGH 白名單）──
    # 順序刻意放在 3 前面：3 會把選單點掉。
    _close_more_menu(page)
    page.click("#more-btn")
    page.wait_for_timeout(150)
    rep.check(4, "說明模式下 #more-btn 仍打得開選單",
              _more_menu_open(page),
              f"help-mode={_help_mode(page)} more-menu.hidden={_hidden(page, '#more-menu')}")

    # ── 3 說明模式下點畫面別處要關掉選單 ──────────────────
    # help.js 在捕獲階段 stopPropagation，掛在冒泡階段的 onOutsideClick 收不到。
    _open_more_menu(page)
    page.mouse.click(60, 400)
    page.wait_for_timeout(150)
    rep.check(3, "說明模式下點外部會關掉選單",
              _hidden(page, "#more-menu") is True,
              f"more-menu.hidden={_hidden(page, '#more-menu')}")

    _set_help_mode(page, False)
    _close_more_menu(page)

    # ── 5 放大影片時按 Escape 只該關選單，不該一併退出放大 ──
    # 窄螢幕整條 .stats-row 是 display:none（既有的行動版版面，不是這次改的），
    # 放大鈕連同它一起消失，這一項在 390px 下沒有東西可驗。
    if not page.locator("#video-max-btn").is_visible():
        rep.skip(5, "Escape 只關選單不退出放大", "#video-max-btn 在這個 viewport 不可見")
        is_max = False
    else:
        page.click("#video-max-btn")
        page.wait_for_timeout(200)
        is_max = page.evaluate("() => document.body.classList.contains('video-max')")
    if page.locator("#video-max-btn").is_visible() and not is_max:
        rep.skip(5, "Escape 只關選單不退出放大", "#video-max-btn 沒能進入放大模式")
    elif is_max:
        _open_more_menu(page)
        page.keyboard.press("Escape")
        page.wait_for_timeout(200)
        menu_closed = _hidden(page, "#more-menu") is True
        still_max = page.evaluate("() => document.body.classList.contains('video-max')")
        rep.check(5, "Escape 只關選單不退出放大", menu_closed and still_max,
                  f"選單關了={menu_closed} 仍在放大={still_max}")
        if page.evaluate("() => document.body.classList.contains('video-max')"):
            page.click("#video-max-btn")
            page.wait_for_timeout(200)

    # ── 設定抽屜 ─────────────────────────────────────────
    page.click("#settings-btn")
    page.wait_for_timeout(900)          # loadSettings 是 async fetch
    groups = page.eval_on_selector_all(
        "#settings-drawer details.settings-group",
        "els => els.map(e => ({open: e.open, label: e.querySelector('summary b')?.textContent || ''}))",
    )
    if len(groups) != len(SETTINGS_GROUPS):
        rep.check(6, "7 組手風琴逐一開合", False,
                  f"找到 {len(groups)} 組 details.settings-group，預期 {len(SETTINGS_GROUPS)}："
                  f"{[g['label'] for g in groups]}")
        rep.skip(7, "收合狀態下設定值仍讀得回來", "手風琴組數不符，先修 6")
        rep.skip(8, "遮罩編輯器開得起來", "手風琴組數不符，先修 6")
    else:
        # ── 7 先讀收合著的組（「儲存空間」）裡的欄位值 ─────
        # 這證明 loadSettings 在 display:none 底下照樣寫得進值。
        # 必須在 6 之前跑：6 會把每一組都展開一次。
        storage_idx = SETTINGS_GROUPS.index("儲存空間")
        is_collapsed = not page.evaluate(
            "(i) => document.querySelectorAll('#settings-drawer details.settings-group')[i].open",
            storage_idx,
        )
        vals = page.evaluate(
            """() => ['set-storage_min_free_gb', 'set-storage_check_interval_seconds',
                      'set-hls-retention', 'set-ntfy_revive_priority']
                     .map(id => [id, document.getElementById(id)?.value ?? null])""",
        )
        all_filled = all(v not in (None, "") for _, v in vals)
        rep.check(7, "收合狀態下設定值仍讀得回來", is_collapsed and all_filled,
                  f"「儲存空間」收合著={is_collapsed} 值={dict(vals)}")

        # ── 6 逐一點 summary，open 要跟著切換、展開要有高度 ──
        bad = []
        for i, want in enumerate(SETTINGS_GROUPS):
            before = page.evaluate(
                "(i) => document.querySelectorAll('#settings-drawer details.settings-group')[i].open", i)
            page.eval_on_selector_all(
                "#settings-drawer details.settings-group",
                "(els, i) => els[i].querySelector('summary').scrollIntoView({block:'center'})", i)
            page.wait_for_timeout(60)
            # 點 chev（predicted 根因：svg 吃掉點擊）與點文字各驗一次。
            # 用真的滑鼠座標點，不用 el.click()：後者繞過 pointer-events，
            # 而「svg 吃掉點擊」正是要驗的東西。
            target = ".settings-chev" if i % 2 == 0 else ".settings-group-head"
            box = page.evaluate(
                """(args) => {
                    const el = document.querySelectorAll('#settings-drawer details.settings-group')[args.i]
                                 .querySelector('summary ' + args.t);
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return {x: r.left + r.width / 2, y: r.top + Math.min(r.height / 2, 8)};
                }""",
                {"i": i, "t": target},
            )
            if not box:
                bad.append(f"{want}(summary 裡找不到 {target})")
                continue
            page.mouse.click(box["x"], box["y"])
            page.wait_for_timeout(150)
            after = page.evaluate(
                "(i) => document.querySelectorAll('#settings-drawer details.settings-group')[i].open", i)
            if after == before:
                bad.append(f"{want}(點 {target} 後 open 沒切換，仍是 {before})")
                continue
            if not after:      # 剛剛是關掉的，再開起來量高度
                page.evaluate(
                    "(i) => { document.querySelectorAll('#settings-drawer details.settings-group')[i].open = true; }", i)
                page.wait_for_timeout(150)
            h = page.evaluate(
                """(i) => {
                    const el = document.querySelectorAll('#settings-drawer details.settings-group')[i];
                    return el.querySelector('.settings-form')?.offsetHeight ?? -1;
                }""", i)
            if h <= 0:
                bad.append(f"{want}(展開後 .settings-form offsetHeight={h})")
        rep.check(6, "7 組手風琴逐一開合", not bad,
                  "全部正常" if not bad else "；".join(bad))

        # ── 8 遮罩編輯器開得起來（不要按儲存）────────────────
        cam = page.evaluate("() => document.getElementById('cam-select')?.value || ''")
        if not cam:
            rep.skip(8, "遮罩編輯器開得起來", "沒有可用的相機（cam-select 是空的）")
        else:
            mask_idx = SETTINGS_GROUPS.index("遮罩")
            page.evaluate(
                "(i) => { document.querySelectorAll('#settings-drawer details.settings-group')[i].open = true; }",
                mask_idx)
            page.wait_for_timeout(100)
            page.click("#mask-edit-btn")
            page.wait_for_timeout(700)
            opened = _hidden(page, "#mask-editor") is False
            toast = page.evaluate(
                "() => document.getElementById('toast')?.textContent?.trim() || ''")
            rep.check(8, "遮罩編輯器開得起來", opened,
                      f"#mask-editor.hidden={_hidden(page, '#mask-editor')} camera={cam} toast={toast!r}")
            if opened:      # ⚠ 只按取消，絕不按儲存
                page.click("#mask-cancel-btn")
                page.wait_for_timeout(200)

    # 抽屜可能已被 openMaskEditor 收起來；還開著就關掉，免得擋住後面的操作
    page.evaluate("() => document.getElementById('settings-close-btn')?.click()")
    page.wait_for_timeout(300)

    # ── 9 色票 ──────────────────────────────────────────
    got = page.evaluate(
        """(names) => Object.fromEntries(names.map(n =>
              [n, getComputedStyle(document.documentElement).getPropertyValue(n).trim()]))""",
        list(EXPECTED_VARS),
    )
    rep.check(9, "vod／thermal 色票已拉開", got == EXPECTED_VARS,
              f"實際={got} 預期={EXPECTED_VARS}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:18321")
    ap.add_argument("--mode", choices=["baseline", "polished"], default="polished")
    ap.add_argument("--viewport", default="1920x1080", help="例如 1920x1080 或 390x844")
    ap.add_argument("--headed", action="store_true", help="開實體視窗看它在做什麼")
    ap.add_argument("--ignore-console", default="",
                    help="正規式；符合的 console.error 與失敗請求 URL 不算數。"
                         "只給已知的環境噪音用（例如驗證機沒有相機在送幀，"
                         "index.m3u8 必然 404），不要拿來蓋掉真的錯誤")
    args = ap.parse_args()

    m = re.fullmatch(r"(\d+)x(\d+)", args.viewport)
    if not m:
        print(f"❌ --viewport 格式錯誤：{args.viewport}（要像 1920x1080）")
        return 2
    vw, vh = int(m.group(1)), int(m.group(2))
    ignore = re.compile(args.ignore_console) if args.ignore_console else None

    rep = Report()
    errors: list[str] = []
    print(f"── {args.url} mode={args.mode} viewport={vw}x{vh} ──")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        ctx = browser.new_context(viewport={"width": vw, "height": vh})
        page = ctx.new_page()
        # 每個失敗的請求都會再吐一則「Failed to load resource: ... status 404」的
        # console.error，訊息裡沒有 URL，查不出是誰。那份資訊由下面的 response
        # handler 完整記下來，所以這裡把這種泛用訊息濾掉，只留真正的 JS 錯誤。
        page.on("console", lambda msg: (
            errors.append(f"[console.error] {msg.text}")
            if msg.type == "error"
            and "Failed to load resource" not in msg.text
            and not (ignore and ignore.search(msg.text)) else None))
        page.on("pageerror", lambda e: errors.append(f"[pageerror] {e}"))
        # console.error 對 HTTP 失敗只印「status 500」不印是誰，查不出根因
        page.on("response", lambda r: (
            errors.append(f"[http {r.status}] {r.url}")
            if r.status >= 400 and not (ignore and ignore.search(r.url)) else None))
        # 對話框一律取消：這支腳本不做任何寫入，confirm 出現就代表要放棄改動
        page.on("dialog", lambda d: d.dismiss())

        try:
            resp = page.goto(args.url, wait_until="domcontentloaded", timeout=30000)
            rep.check(0, "頁面載得起來", resp is not None and resp.ok,
                      f"HTTP {resp.status if resp else 'no response'}")
            page.wait_for_selector("header", timeout=15000)
            page.wait_for_timeout(2500)     # 讓 init()／WS／loadSettings 跑完

            base = args.url.rstrip("/")
            for path in ("/static/favicon.ico", "/favicon.ico"):
                r = ctx.request.get(base + path)
                rep.check("fav", f"{path} 回 200", r.status == 200, f"HTTP {r.status}")

            if args.mode == "polished":
                run_polished(page, rep)
                # ── 10 窄螢幕重跑第 1 項 ────────────────────
                page.set_viewport_size({"width": 390, "height": 844})
                page.wait_for_timeout(400)
                check_menu_position(page, rep, 10, "窄螢幕（390x844）選單仍在畫面內")
        finally:
            ctx.close()
            browser.close()

    rep.check("err", "console 沒有錯誤", not errors,
              "0 筆" if not errors else f"{len(errors)} 筆")
    rep.dump()
    for e in errors:
        print(f"   {e}")

    n = rep.failed()
    print(f"── {'全過' if not n else f'{n} 項失敗'} ──")
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
