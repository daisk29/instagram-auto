document.addEventListener("DOMContentLoaded", () => {
  const globalLoading = document.querySelector("[data-global-loading]");
  const globalMessage = globalLoading?.querySelector("[data-global-loading-message]");
  const globalDetail = globalLoading?.querySelector("[data-global-loading-detail]");
  const globalTrack = globalLoading?.querySelector("[data-global-loading-track]");
  const globalBar = globalLoading?.querySelector("[data-global-loading-bar]");
  const globalPercent = globalLoading?.querySelector("[data-global-loading-percent]");
  const globalCancel = globalLoading?.querySelector("[data-global-cancel]");

  function updateGlobalProgress(value, text) {
    if (!globalLoading || globalLoading.hidden) return;
    const safeValue = Math.max(0, Math.min(100, Number(value) || 0));
    if (globalBar) globalBar.style.width = `${safeValue}%`;
    if (globalTrack) globalTrack.setAttribute("aria-valuenow", String(safeValue));
    if (globalPercent) {
      globalPercent.hidden = false;
      globalPercent.textContent = `${safeValue}%`;
    }
    if (text && globalMessage) globalMessage.textContent = text;
  }

  const showGlobalLoading = (text, options = {}) => {
    if (!globalLoading) return;
    const determinate = options.determinate === true;
    globalLoading.hidden = false;
    globalLoading.classList.toggle("is-determinate", determinate);
    document.body.classList.add("is-loading");
    document.body.setAttribute("aria-busy", "true");
    if (globalMessage) globalMessage.textContent = text || "処理しています";
    if (globalDetail) {
      globalDetail.textContent = options.detail ||
        "完了するまで、この画面を閉じずにお待ちください。";
    }
    if (globalCancel) globalCancel.hidden = options.cancellable !== true;
    if (determinate) {
      updateGlobalProgress(options.progress || 0, text);
    } else {
      if (globalPercent) globalPercent.hidden = true;
      if (globalTrack) globalTrack.removeAttribute("aria-valuenow");
      if (globalBar) globalBar.style.width = "";
    }
  };

  const hideGlobalLoading = () => {
    if (!globalLoading) return;
    globalLoading.hidden = true;
    globalLoading.classList.remove("is-determinate", "is-error");
    document.body.classList.remove("is-loading");
    document.body.removeAttribute("aria-busy");
    if (globalCancel) {
      globalCancel.hidden = true;
      globalCancel.disabled = false;
      globalCancel.textContent = "中止する";
    }
  };

  document.querySelectorAll(".button, .account-tab").forEach((control) => {
    const release = () => control.classList.remove("is-pressed");
    control.addEventListener("pointerdown", () => {
      if (!control.disabled && control.getAttribute("aria-disabled") !== "true") {
        control.classList.add("is-pressed");
      }
    });
    control.addEventListener("pointerup", release);
    control.addEventListener("pointercancel", release);
    control.addEventListener("pointerleave", release);
  });

  document.querySelectorAll("[data-schedule-input]").forEach((input) => {
    const now = new Date();
    now.setMinutes(now.getMinutes() + 1);
    now.setSeconds(0, 0);
    const local = new Date(now.getTime() - now.getTimezoneOffset() * 60000);
    input.min = local.toISOString().slice(0, 16);
  });

  const loadingForms = document.querySelectorAll("[data-loading-form]");

  loadingForms.forEach((loadingForm) => {
    const loadingButton = loadingForm.querySelector("[data-loading-button]");
    const loadingProgress = loadingForm.querySelector("[data-loading-progress]");
    const loadingText = loadingForm.querySelector("[data-loading-text]");
    if (!loadingButton || !loadingProgress) return;

    const originalLabel = loadingButton.textContent;

    loadingForm.addEventListener("submit", (event) => {
      if (loadingForm.dataset.submitting === "true") {
        event.preventDefault();
        return;
      }

      if (loadingForm.hasAttribute("data-require-photo-selection")) {
        const selected = loadingForm.querySelectorAll(
          "input[name='photo_ids']:checked"
        );
        if (selected.length === 0) {
          event.preventDefault();
          window.alert("キャプションを生成する写真を選択してください。");
          return;
        }
      }

      loadingForm.dataset.submitting = "true";
      loadingButton.disabled = true;
      loadingButton.setAttribute("aria-busy", "true");
      loadingButton.textContent =
        loadingForm.dataset.loadingButtonLabel || "処理中…";
      loadingProgress.hidden = false;
      if (loadingText) {
        loadingText.textContent =
          loadingForm.dataset.loadingMessage || "処理しています";
      }
      loadingProgress.scrollIntoView({ behavior: "smooth", block: "nearest" });
      showGlobalLoading(
        loadingForm.dataset.loadingMessage || "処理しています"
      );
    });

    window.addEventListener("pageshow", (event) => {
      if (!event.persisted) return;
      loadingForm.dataset.submitting = "false";
      loadingButton.disabled = false;
      loadingButton.removeAttribute("aria-busy");
      loadingButton.textContent = originalLabel;
      loadingProgress.hidden = true;
      hideGlobalLoading();
    });
  });

  document.querySelectorAll("form:not([data-loading-form])").forEach((plainForm) => {
    plainForm.addEventListener("submit", (event) => {
      const submitter = event.submitter;
      if (submitter?.matches("[data-publish-button]")) return;
      if (event.defaultPrevented) return;
      const label = submitter?.dataset.loadingLabel || submitter?.textContent?.trim();
      showGlobalLoading(label ? `${label}…` : "処理しています");
    });
  });

  document.querySelectorAll("a.button[href], a.account-tab[href]").forEach((link) => {
    link.addEventListener("click", (event) => {
      if (
        event.defaultPrevented || event.button !== 0 || event.metaKey ||
        event.ctrlKey || event.shiftKey || event.altKey || link.target === "_blank"
      ) return;
      showGlobalLoading(
        link.matches(".account-tab")
          ? "アカウントを切り替えています"
          : "画面を読み込んでいます"
      );
    });
  });

  window.addEventListener("pageshow", (event) => {
    if (event.persisted) hideGlobalLoading();
  });

  const form = document.querySelector("[data-draft-form]");
  const button = document.querySelector("[data-publish-button]");
  const progressBox = document.querySelector("[data-publish-progress]");

  if (!form || !progressBox) return;

  const statusUrl = progressBox.dataset.statusUrl;
  const bar = progressBox.querySelector("[data-progress-bar]");
  const track = progressBox.querySelector("[role='progressbar']");
  const message = progressBox.querySelector("[data-progress-message]");
  const percent = progressBox.querySelector("[data-progress-percent]");
  const cancelButton = progressBox.querySelector("[data-cancel-publish]");
  const progressNote = progressBox.querySelector("[data-progress-note]");
  let submitting = false;
  let pollTimer = null;

  const updateProgress = (value, text) => {
    const safeValue = Math.max(0, Math.min(100, Number(value) || 0));
    bar.style.width = `${safeValue}%`;
    track.setAttribute("aria-valuenow", String(safeValue));
    percent.textContent = `${safeValue}%`;
    if (text) message.textContent = text;
    updateGlobalProgress(safeValue, text);
  };

  const stopPolling = () => {
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = null;
  };

  const pollStatus = async () => {
    try {
      const response = await fetch(statusUrl, {
        credentials: "same-origin",
        cache: "no-store",
        headers: { Accept: "application/json" },
      });
      if (!response.ok) throw new Error("進捗を取得できませんでした");

      const state = await response.json();
      updateProgress(state.progress, state.message);
      if (cancelButton) cancelButton.hidden = !state.cancellable;
      if (globalCancel) globalCancel.hidden = !state.cancellable;
      if (progressNote && !state.cancellable && state.status === "committing") {
        progressNote.textContent = "公開処理が始まったため中止できません。";
        if (globalDetail) {
          globalDetail.textContent = "Instagramで公開処理中です。この段階では中止できません。";
        }
      }

      if (state.complete) {
        stopPolling();
        window.location.reload();
        return;
      }
    } catch (error) {
      message.textContent = "投稿処理を確認しています…";
      if (globalMessage) globalMessage.textContent = "投稿処理を確認しています…";
    }
    pollTimer = window.setTimeout(pollStatus, 1000);
  };

  if (!progressBox.hidden) {
    submitting = true;
    if (button) button.disabled = true;
    showGlobalLoading(message.textContent, {
      determinate: true,
      progress: Number(track.getAttribute("aria-valuenow")) || 5,
      cancellable: !cancelButton?.hidden,
      detail: "画像送信中は、この画面から投稿処理を中止できます。",
    });
    pollStatus();
  }

  const cancelPublish = async () => {
    if (!window.confirm("Instagramへの投稿処理を中止しますか？")) return;
    if (cancelButton) {
      cancelButton.disabled = true;
      cancelButton.textContent = "中止しています…";
    }
    if (globalCancel) {
      globalCancel.disabled = true;
      globalCancel.textContent = "中止しています…";
    }
    const csrf = form.querySelector("input[name='csrf_token']");
    const body = new URLSearchParams({ csrf_token: csrf ? csrf.value : "" });
    try {
      const response = await fetch(progressBox.dataset.cancelUrl, {
        method: "POST",
        body,
        credentials: "same-origin",
        headers: { Accept: "application/json" },
      });
      const state = await response.json();
      if (!response.ok) throw new Error(state.message || "中止できませんでした");
      stopPolling();
      updateProgress(100, state.message);
      if (globalCancel) globalCancel.hidden = true;
      if (globalDetail) globalDetail.textContent = "写真は確認待ちへ戻しました。";
      if (progressNote) progressNote.textContent = "写真は確認待ちへ戻しました。";
      window.setTimeout(() => window.location.reload(), 800);
    } catch (error) {
      if (cancelButton) {
        cancelButton.disabled = false;
        cancelButton.textContent = "中止する";
      }
      if (globalCancel) {
        globalCancel.disabled = false;
        globalCancel.textContent = "中止する";
      }
      if (progressNote) progressNote.textContent = error.message;
      if (globalDetail) globalDetail.textContent = error.message;
    }
  };

  if (cancelButton) cancelButton.addEventListener("click", cancelPublish);
  if (globalCancel) globalCancel.addEventListener("click", cancelPublish);

  form.addEventListener("submit", async (event) => {
    const submitter = event.submitter;
    if (!submitter || !submitter.matches("[data-publish-button]")) return;

    event.preventDefault();
    if (submitting) return;

    const confirmed = form.querySelector("input[name='confirm']");
    if (!confirmed || !confirmed.checked) {
      window.alert("写真とキャプションを確認し、確認欄にチェックしてください。");
      return;
    }

    submitting = true;
    const action = submitter.formAction;
    button.disabled = true;
    button.textContent = "投稿処理中…";
    progressBox.hidden = false;
    showGlobalLoading("投稿処理を開始しています", {
      determinate: true,
      progress: 5,
      cancellable: true,
      detail: "画像送信中は、この画面から投稿処理を中止できます。",
    });
    updateProgress(5, "投稿処理を開始しています");
    progressBox.scrollIntoView({ behavior: "smooth", block: "nearest" });
    pollStatus();

    try {
      const response = await fetch(action, {
        method: "POST",
        body: new FormData(form),
        credentials: "same-origin",
      });
      if (!response.ok) throw new Error("投稿リクエストに失敗しました");
      stopPolling();
      window.location.assign(response.url);
    } catch (error) {
      stopPolling();
      message.textContent = "通信結果を確認できません。画面を再読み込みしてください。";
      progressBox.classList.add("is-error");
      globalLoading?.classList.add("is-error");
      if (globalMessage) globalMessage.textContent = "通信結果を確認できません";
      if (globalDetail) {
        globalDetail.textContent = "画面を再読み込みして投稿状態を確認します。";
      }
      window.setTimeout(() => window.location.reload(), 2000);
    }
  });
});
