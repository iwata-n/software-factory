// This controller also runs in Node unit tests; no browser or network is required.
export function installTaskDeletion({get, currentTask, canDelete, request, onDeleted}) {
  const dialog = get("delete-task-dialog");
  const action = get("delete-task");
  const confirm = get("delete-task-confirm");
  const cancel = get("delete-task-cancel");
  const status = get("delete-task-status");
  let pending;
  let controller;

  function update() {
    action.hidden = !currentTask() || !canDelete();
    action.disabled = confirm.disabled = cancel.disabled = Boolean(controller);
  }

  function reset() {
    controller?.abort();
    controller = undefined;
    pending = undefined;
    dialog.close();
    update();
  }

  action.addEventListener("click", () => {
    const task = currentTask();
    if (!task || !canDelete() || controller) return;
    // Freeze the target even if background refresh changes the current selection.
    pending = {id: task.task_id, title: task.task_title};
    get("delete-task-description").textContent = `「${pending.title}」を削除しますか？`;
    status.textContent = "";
    status.classList.toggle("error", false);
    dialog.showModal();
  });
  cancel.addEventListener("click", () => { if (!controller) reset(); });
  dialog.addEventListener("cancel", (event) => {
    if (controller) event.preventDefault();
  });
  dialog.addEventListener("close", () => { if (!controller) pending = undefined; });
  confirm.addEventListener("click", async () => {
    const target = pending;
    if (!target || controller || !canDelete()) return;
    const active = new AbortController();
    controller = active;
    update();
    try {
      const response = await request(`/v1/tasks/${encodeURIComponent(target.id)}`, {
        method: "DELETE", redirect: "error", cache: "no-store", signal: active.signal,
      });
      if (active.signal.aborted) return;
      if (!response.ok) {
        const body = await response.json().catch(() => ({}));
        if (active.signal.aborted) return;
        throw new Error(body.code ?? `HTTP ${response.status}`);
      }
      onDeleted(target.id);
      pending = undefined;
      dialog.close();
    } catch (error) {
      if (active.signal.aborted) return;
      status.textContent = {
        TASK_DELETE_ACTIVE_RUN: "実行や詳細化が進行中のため削除できません。先にタスク詳細から実行を取り消してください。",
        TASK_DELETE_ACTIVE_CHAT: "AIとの会話が処理中のため削除できません。処理の終了を待ってから再試行してください。",
        NOT_FOUND: "対象のタスクが見つかりません。詳細と一覧を更新してください。",
        USER_ACCESS_DENIED: "削除には操作者または管理者の権限が必要です。",
      }[error.message] ?? "削除結果を確認できませんでした。状態を確認して再試行してください。";
      status.classList.toggle("error", true);
    } finally {
      if (controller === active) {
        controller = undefined;
        update();
      }
    }
  });
  update();
  return {update, reset};
}
