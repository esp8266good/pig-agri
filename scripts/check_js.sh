#!/usr/bin/env bash
# static/js 的語法檢查。
#
# 為什麼不直接 `node --check static/js/foo.js`：那個指令對 .js 副檔名走 CommonJS
# 解析，而 CommonJS 解析器對這些 ES module 檔案會直接放行、回傳 exit 0，連
# 「物件字面量少一個逗號」這種一定會讓整包 module 載不起來的錯都抓不到。
# 2026-08-24 就是這樣把一個壞掉的 help.js 部署到正式機，整個前端全黑。
# 複製成 .mjs 再檢查，node 才會用 ES module 解析器。
set -euo pipefail
cd "$(dirname "$0")/.."
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
fail=0
for f in static/js/*.js; do
  cp "$f" "$tmp/$(basename "${f%.js}").mjs"
done
for m in "$tmp"/*.mjs; do
  orig="static/js/$(basename "${m%.mjs}").js"
  if ! node --check "$m" 2>&1 | sed "s#$m#$orig#"; then
    fail=1
  fi
done
if [ "$fail" -ne 0 ]; then echo "JS 語法檢查失敗" >&2; exit 1; fi
echo "JS 語法檢查通過（$(ls static/js/*.js | wc -l) 個檔案）"
