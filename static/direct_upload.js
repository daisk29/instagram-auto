/* Per-file XHR upload: keeps the selected Instagram account and shows actual progress. */
(() => {
  'use strict';
  const root = document.querySelector('[data-direct-upload]');
  if (!root) return;
  const form = root.querySelector('form');
  const input = root.querySelector('input[type="file"]');
  const choose = root.querySelector('[data-upload-choose]');
  const bar = root.querySelector('[data-upload-progress-bar]');
  const progress = root.querySelector('[data-upload-progress]');
  const status = root.querySelector('[data-upload-status]');
  const MAX_FILES = 20;
  const MAX_BYTES = 25 * 1024 * 1024;
  let busy = false;

  choose.addEventListener('click', () => { if (!busy) input.click(); });
  input.addEventListener('change', () => {
    const files = Array.from(input.files || []);
    if (!files.length) return;
    if (files.length > MAX_FILES) {
      status.textContent = `一度に選択できる写真は${MAX_FILES}枚までです。`;
      input.value = '';
      return;
    }
    const invalid = files.find(f => !/\.(jpe?g|png)$/i.test(f.name) || f.size > MAX_BYTES);
    if (invalid) {
      status.textContent = 'JPEG・PNG形式、1枚25MB以下の写真を選択してください。';
      input.value = '';
      return;
    }
    uploadFiles(files);
  });

  const send = (file, onProgress) => new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest();
    const body = new FormData();
    body.append('csrf_token', form.elements.csrf_token.value);
    body.append('account_key', form.elements.account_key.value);
    body.append('photo', file, file.name);
    xhr.open('POST', form.action);
    xhr.timeout = 120000;
    xhr.upload.addEventListener('progress', e => {
      if (e.lengthComputable) onProgress(e.loaded / e.total);
    });
    xhr.addEventListener('load', () => {
      let payload = {};
      try { payload = JSON.parse(xhr.responseText || '{}'); } catch (_e) { /* server may return HTML */ }
      if (xhr.status >= 200 && xhr.status < 300 && payload.ok) resolve(payload);
      else reject(new Error(payload.error || `サーバーエラー（HTTP ${xhr.status}）`));
    });
    xhr.addEventListener('error', () => reject(new Error('通信エラーが発生しました')));
    xhr.addEventListener('timeout', () => reject(new Error('通信がタイムアウトしました。Driveを確認してください')));
    xhr.send(body);
  });

  async function uploadFiles(files) {
    busy = true;
    choose.disabled = true;
    choose.textContent = 'アップロード中…';
    progress.hidden = false;
    bar.style.width = '0%';
    let success = 0;
    const errors = [];
    for (let i = 0; i < files.length; i++) {
      status.textContent = `${i + 1} / ${files.length}枚をアップロード中：${files[i].name}`;
      try {
        await send(files[i], fraction => {
          bar.style.width = `${Math.round((i + fraction) / files.length * 100)}%`;
        });
        success++;
      } catch (err) {
        errors.push(`${files[i].name}：${err.message}`);
      }
      bar.style.width = `${Math.round((i + 1) / files.length * 100)}%`;
    }
    busy = false;
    choose.disabled = false;
    choose.textContent = '写真をアップロード';
    input.value = '';
    status.textContent = `${success}枚の写真を追加しました。` +
      (errors.length ? ` ${errors.length}枚失敗：${errors.slice(0, 3).join('／')}` : ' 未処理一覧を更新します。');
    if (success && !errors.length) {
      window.setTimeout(() => { window.location.href = root.dataset.returnUrl; }, 900);
    } else if (success) {
      const a = document.createElement('a');
      a.href = root.dataset.returnUrl;
      a.textContent = '未処理の写真を確認';
      a.className = 'button button-primary';
      status.appendChild(document.createTextNode(' '));
      status.appendChild(a);
    }
  }
})();
