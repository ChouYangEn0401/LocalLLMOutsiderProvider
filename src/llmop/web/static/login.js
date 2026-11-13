/* Login page: POST /ui/api/login (JSON), then go to ?next= (same-site /ui/* only). */
(function () {
  'use strict';

  LL.boot(() => {
    const form = LL.$('#login-form');
    const err = LL.$('#login-error');
    const btn = LL.$('#login-btn');
    const params = new URLSearchParams(location.search);
    let next = params.get('next') || '';
    if (!/^\/ui\/[a-z]+$/.test(next)) next = '/ui/dashboard';

    form.addEventListener('submit', async (ev) => {
      ev.preventDefault();
      err.textContent = '';
      const username = form.username.value.trim();
      const password = form.password.value;
      if (!username || !password) {
        err.textContent = '請輸入帳號與密碼。';
        (username ? form.password : form.username).focus();
        return;
      }
      btn.disabled = true;
      btn.textContent = '登入中…';
      try {
        await LL.api('POST', '/ui/api/login', { username, password });
        location.replace(next);
        return;
      } catch (e) {
        err.textContent = e.message;
        form.password.value = '';
        form.password.focus();
      }
      btn.disabled = false;
      btn.textContent = '登入';
    });
  });
})();
