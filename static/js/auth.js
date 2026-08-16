// static/js/auth.js — 登入畫面與 session 過期處理。
//
// 後端 AUTH_ENABLED=false（預設）時，/auth/status 回 enabled:false，這支就整個
// 不做事：不顯示登入框、不裝 fetch 攔截、登出鈕保持隱藏。也就是說在啟用之前，
// 前端行為與加這支之前完全一樣。

function el(id) { return document.getElementById(id); }

function showOverlay(msg = '') {
  const ov = el('login-overlay');
  if (!ov) return;
  ov.hidden = false;
  el('login-error').textContent = msg;
  el('login-username')?.focus();
}

function hideOverlay() {
  const ov = el('login-overlay');
  if (ov) ov.hidden = true;
}

function setLogoutVisible(on, username) {
  const btn = el('logout-btn');
  if (!btn) return;
  btn.hidden = !on;
  if (on && username) btn.title = `登出 ${username}`;
}

// 使用者按下「登入」→ 成功才 resolve。失敗留在畫面上顯示原因讓他重試，
// 所以這個 Promise 在登入成功前不會 settle。
function waitForLogin() {
  return new Promise(resolve => {
    const form = el('login-form');
    if (!form) return resolve();
    const onSubmit = async (e) => {
      e.preventDefault();
      const btn = el('login-submit');
      btn.disabled = true;
      try {
        const resp = await fetch('/auth/login', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username: el('login-username').value,
            password: el('login-password').value,
          }),
        });
        if (resp.ok) {
          el('login-password').value = '';
          hideOverlay();
          form.removeEventListener('submit', onSubmit);
          resolve();
          return;
        }
        const data = await resp.json().catch(() => ({}));
        el('login-error').textContent = data.detail || `登入失敗（HTTP ${resp.status}）`;
      } catch (_) {
        el('login-error').textContent = '無法連線到伺服器';
      } finally {
        btn.disabled = false;
      }
    };
    form.addEventListener('submit', onSubmit);
  });
}

// session 過期（12 小時後）或被登出時，任何 API 請求都會回 401。逐一改寫
// 四十幾個 fetch 呼叫點太侵入，改成在這裡包一層 window.fetch 集中處理：
// 收到 401 就把登入框叫出來，登入成功後整頁 reload 讓狀態乾淨重來。
// 只在驗證確實開啟時才裝這層。
function installUnauthorizedHandler() {
  const orig = window.fetch;
  let reloading = false;
  window.fetch = async (...args) => {
    const resp = await orig(...args);
    const url = String(args[0] ?? '');
    // /auth/* 自己回的 401 是登入失敗，由表單顯示錯誤，不能在這裡再彈一次。
    if (resp.status === 401 && !url.includes('/auth/') && !reloading) {
      reloading = true;
      showOverlay('連線階段已過期，請重新登入');
      await waitForLogin();
      location.reload();
    }
    return resp;
  };
}

/** 開機時呼叫。驗證關閉 → 直接回；已登入 → 直接回；未登入 → 擋在登入畫面。 */
export async function ensureAuth() {
  let st;
  try {
    st = await fetch('/auth/status').then(r => r.json());
  } catch (_) {
    // 問不到狀態（後端還沒起來之類）就照舊往下跑，讓後續請求的 401 接手，
    // 不要因為一個輔助端點失敗就把整個 app 卡在登入畫面。
    return;
  }
  if (!st.enabled) { setLogoutVisible(false); return; }

  installUnauthorizedHandler();
  if (!st.authenticated) {
    showOverlay();
    await waitForLogin();
    // 這裡是開機路徑，init() 還沒跑，直接往下走即可，不需要 reload。
  }
  setLogoutVisible(true, st.username);

  el('logout-btn')?.addEventListener('click', async () => {
    try { await fetch('/auth/logout', { method: 'POST' }); } catch (_) {}
    location.reload();
  });
}
