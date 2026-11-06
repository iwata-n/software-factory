import { renderAttention } from "./a2ui.js?v=spec-interview";

const get = (id) => document.getElementById(id);
const lanes = new Map([
  ["DRAFT", "詳細化前"],
  ["CLARIFYING", "詳細化中"],
  ["READY", "実行待ち"],
  ["RUNNING", "実行中"],
  ["BLOCKED", "要対応"],
  ["DONE", "完了"],
]);
const resultLabels = new Map([
  ["SUCCEEDED", "成功"],
  ["FAILED", "失敗"],
  ["CANCELLED", "取消済み"],
  ["CANCELLING", "取消処理中"],
]);
const phaseLabels = new Map([
  ["PLAN", "仕様の詳細化"],
  ["WAIT_PLAN_APPROVAL", "計画の承認待ち"],
  ["IMPLEMENT", "実装"],
  ["ASSEMBLE_CANDIDATE", "候補の作成"],
  ["VERIFY_REVIEW", "検証・レビュー"],
  ["REPAIR", "修正"],
  ["PUBLISH_DRAFT", "Draft PR の作成"],
  ["PUBLISH_UNKNOWN", "公開結果の確認"],
  ["WAIT_CI", "CI 待ち"],
]);

function boardStatus(run) {
  if (lanes.has(run.task_status)) return run.task_status;
  if (["SUCCEEDED", "CANCELLED"].includes(run.status)) return "DONE";
  if (run.status === "BLOCKED") {
    return ["PLAN_HAS_OPEN_QUESTIONS", "SPEC_CHANGE_REQUIRED"].includes(run.stop_reason)
      ? "CLARIFYING" : "BLOCKED";
  }
  if (run.status === "FAILED") return "BLOCKED";
  if (run.phase === "WAIT_PLAN_APPROVAL" && run.status === "WAITING_EXTERNAL") return "READY";
  if (run.status === "CANCELLING" || run.execution_requested) return "RUNNING";
  if (run.clarification_only) return "CLARIFYING";
  if (run.status === "QUEUED") return run.phase === "PLAN" ? "DRAFT" : "RUNNING";
  if (["RUNNING", "WAITING_EXTERNAL"].includes(run.status)) {
    return ["PLAN", "WAIT_PLAN_APPROVAL"].includes(run.phase) ? "CLARIFYING" : "RUNNING";
  }
  return run.status === "CANCELLING" ? "RUNNING" : "BLOCKED";
}

const detail = get("detail");
const detailResize = get("detail-resize");
const detailWidthKey = "factory.runDetailWidth";
const toolbar = get("toolbar");
const menuToggle = toolbar.querySelector("summary");
const BOARD_REFRESH_MS = 5000;
const RUN_PAGE_SIZE = 50;
const executionButtons = ["execution-action", "execution-submit", "execution-dismiss"];
let token = "";
let account;
const secureAccounts = location.protocol === "https:" ||
  ["127.0.0.1", "localhost", "[::1]"].includes(location.hostname);

function authHeaders() {
  return token ? {Authorization: `Bearer ${token}`} : {};
}
let runs = [];
let showRunHistory = false;
let visibleRunLimit = RUN_PAGE_SIZE;
let boardTimer;
let boardRequest;
let detailRequest;
let detailTab = "conversation";
let detailTabRunId;
let selected;
let executionContext;
let executionRequest;
let pendingExecution;

function element(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function message(id, text, error = false) {
  const node = get(id);
  node.textContent = text;
  node.classList.toggle("error", error);
}

class ApiError extends Error {
  constructor(status) {
    super(`取得できませんでした (HTTP ${status})`);
    this.status = status;
  }
}

async function read(path, signal) {
  const response = await fetch(path, {
    headers: authHeaders(),
    cache: "no-store",
    signal,
    redirect: "error",
  });
  if (!response.ok) throw new ApiError(response.status);
  return response.json();
}

function disconnect(text = "接続を終了しました。", error = false) {
  clearTimeout(boardTimer);
  boardRequest?.abort();
  detailRequest?.abort();
  executionRequest?.abort();
  executionRequest = undefined;
  executionButtons.forEach((id) => { get(id).disabled = false; });
  executionContext = undefined;
  closeExecutionForm();
  closeTaskDialog();
  get("task-open").hidden = true;
  get("integration-settings").close();
  get("integration-open").hidden = true;
  get("execution").hidden = true;
  get("worker-panel").hidden = true;
  get("worker-panel").open = false;
  get("worker-list").replaceChildren();
  get("worker-list").hidden = true;
  executionMessage("");
  token = "";
  account = undefined;
  accountsDialog.close();
  get("account-name").hidden = true;
  get("accounts-open").hidden = true;
  runs = [];
  showRunHistory = false;
  get("run-history-toggle").setAttribute("aria-pressed", "false");
  visibleRunLimit = RUN_PAGE_SIZE;
  selected = null;
  detail.close();
  get("detail-content").replaceChildren();
  get("board").replaceChildren();
  toolbar.open = false;
  for (const id of ["board", "board-navigation", "toolbar", "older"]) get(id).hidden = true;
  get("token-login").hidden = false;
  get("login").hidden = !secureAccounts;
  get(secureAccounts ? "login-username" : "token").focus();
  message("status", text, error);
}

function renderBoard() {
  const board = get("board");
  const scrollLeft = board.scrollLeft;
  const laneOffsets = new Map([...board.querySelectorAll(".lane ul")]
    .map((list) => [list.closest(".lane").dataset.status, list.scrollTop]));
  const focusedRun = document.activeElement?.dataset.runId;
  const grouped = new Map([...lanes.keys()].map((status) => [status, []]));
  for (const run of runs) grouped.get(boardStatus(run)).push(run);
  const sections = [];
  for (const [status, items] of grouped) {
    const section = element("section", undefined, "lane");
    section.dataset.status = status;
    const heading = element("h2", lanes.get(status) ?? status);
    heading.id = `lane-${sections.length}`;
    heading.append(element("span", items.length, "lane-count"));
    section.setAttribute("aria-labelledby", heading.id);
    section.append(heading);
    if (items.length === 0) {
      section.append(element("p", "該当なし"));
    } else {
      const list = element("ul");
      for (const run of items) {
        const item = element("li");
        const card = element("button", undefined, "card");
        card.dataset.runId = run.run_id ?? run.task_id;
        card.setAttribute("aria-haspopup", "dialog");
        card.append(
          element("span", run.task_title),
          element("small", lanes.get(status) + (resultLabels.has(run.status) ? ` · ${resultLabels.get(run.status)}` : "")),
          element("small", `仕様 v${run.task_spec_version ?? run.spec_version ?? 1}${run.run_id ? ` · ${run.run_id.slice(0, 8)}` : ""}`),
        );
        if (run.stop_reason) card.append(element("small", run.stop_reason, "reason"));
        card.addEventListener("click", () => {
          selected = run;
          detail.showModal();
          syncDetailResize();
          get("detail-body").scrollTop = 0;
          loadDetail();
        });
        item.append(card);
        list.append(item);
      }
      section.append(list);
    }
    sections.push(section);
  }
  board.replaceChildren(...sections);
  board.scrollLeft = scrollLeft;
  for (const list of board.querySelectorAll(".lane ul")) {
    list.scrollTop = laneOffsets.get(list.closest(".lane").dataset.status) ?? 0;
  }
  if (focusedRun) {
    board.querySelector(`[data-run-id="${CSS.escape(focusedRun)}"]`)?.focus({preventScroll: true});
  }
}

function executionMessage(text, error = false) {
  message("execution-message", text, error);
  get("execution-message").hidden = !text;
}

function closeExecutionForm() {
  pendingExecution = undefined;
  get("execution-form").hidden = true;
  get("execution-action").setAttribute("aria-expanded", "false");
  get("execution-reason").value = "";
}

function renderExecution(context) {
  const { execution, access } = context;
  if (executionContext?.execution.revision !== execution.revision || access !== "ADMIN") {
    closeExecutionForm();
  }
  executionContext = context;
  const panel = get("execution");
  panel.dataset.stopped = String(execution.stopped);
  panel.hidden = false;
  const state = execution.stopped ? "緊急停止中" : "実行許可";
  if (get("execution-state").textContent !== state) message("execution-state", state);
  get("execution-revision").textContent = `Rev. ${execution.revision}`;
  get("execution-action").hidden = access !== "ADMIN";
  get("task-open").hidden = !["ADMIN", "OPERATOR"].includes(access);
  get("integration-open").hidden = access !== "ADMIN";
  get("accounts-open").hidden = access !== "ADMIN" || !secureAccounts;
  get("disconnect").textContent = account ? "ログアウト" : "切断";
  get("disconnect").hidden = account?.authentication === "lan";
  get("account-name").hidden = !account;
  get("account-name").textContent = account?.username ?? "";
  get("execution-action").textContent = execution.stopped ? "緊急停止を解除" : "緊急停止";
}

function openExecutionForm() {
  if (executionContext?.access !== "ADMIN" || executionRequest) return;
  const { execution } = executionContext;
  pendingExecution = {
    workspaceId: execution.workspace_id,
    stopped: !execution.stopped,
    expected_revision: execution.revision,
  };
  executionMessage("");
  get("execution-submit").textContent = execution.stopped ? "解除を実行" : "停止を実行";
  get("execution-form").hidden = false;
  get("execution-action").setAttribute("aria-expanded", "true");
  get("execution-reason").focus();
}

async function submitExecution(event) {
  event.preventDefault();
  const action = pendingExecution;
  if (!action || executionRequest) return;
  const reason = get("execution-reason").value.trim();
  if (!reason) {
    executionMessage("操作の理由を入力してください。", true);
    return;
  }
  if (action.reason !== reason) {
    action.reason = reason;
    action.key = Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) =>
      b.toString(16).padStart(2, "0")).join("");
  }
  executionRequest = new AbortController();
  const { signal } = executionRequest;
  executionButtons.forEach((id) => { get(id).disabled = true; });
  try {
    const { workspaceId, stopped, expected_revision, key } = action;
    const response = await fetch(`/v1/workspaces/${encodeURIComponent(workspaceId)}/execution-control`, {
      method: "POST",
      headers: {
        ...authHeaders(), "Content-Type": "application/json", "Idempotency-Key": key,
      },
      body: JSON.stringify({ stopped, expected_revision, reason }),
      cache: "no-store", signal, redirect: "error",
    });
    if (signal.aborted) return;
    if (response.status === 401) {
      disconnect(account ? "ログインし直してください。" : "User トークンを確認してください。", true);
      return;
    }
    if ([403, 409].includes(response.status)) {
      closeExecutionForm();
      executionMessage("権限または実行状態が変わりました。最新状態を確認してください。", true);
      await loadBoard();
      return;
    }
    if (!response.ok) throw new ApiError(response.status);
    const execution = await response.json();
    if (signal.aborted) return;
    renderExecution({ ...executionContext, execution });
    executionMessage(stopped ? "緊急停止を適用しました。" : "緊急停止を解除しました。");
    await loadBoard();
  } catch {
    if (!signal.aborted) executionMessage("操作結果を確認できませんでした。同じ理由で再試行できます。", true);
  } finally {
    if (executionRequest?.signal === signal) {
      executionRequest = undefined;
      executionButtons.forEach((id) => { get(id).disabled = false; });
    }
  }
}

const leaseLabels = {
  NO_LEASE: "Lease 履歴なし",
  LEASED: "実行権あり",
  HEARTBEAT_OVERDUE: "Heartbeat 遅延・停止未確認",
  EXECUTION_RIGHT_EXPIRED: "実行権失効・停止未確認",
  WORKER_REPORTED_STOP: "Worker が停止を報告済み",
  STOP_UNCONFIRMED: "停止未確認・Worker 隔離済み",
  LEASE_ENDED: "Lease 終了・停止未確認",
};
const credentialLabels = { ACTIVE: "有効", EXPIRED: "期限切れ", REVOKED: "失効済み" };

async function loadWorkers(workspaceId, signal) {
  if (get("worker-panel").hidden) message("worker-status", "Worker の状態を取得中…");
  get("worker-panel").hidden = false;
  try {
    const { workers } = await read(`/v1/workspaces/${encodeURIComponent(workspaceId)}/workers`, signal);
    if (signal.aborted) return;
    const entries = workers.map((worker) => section(worker.name, fields([
      ["Worker ID", worker.worker_id],
      ["実行状態", leaseLabels[worker.lease_status] ?? worker.lease_status],
      ["最終 Heartbeat", date(worker.last_heartbeat_at)],
      ["Lease 期限", date(worker.lease_expires_at)],
      ["資格情報", credentialLabels[worker.credential_status] ?? worker.credential_status],
    ])));
    get("worker-list").replaceChildren(...entries);
    get("worker-list").hidden = entries.length === 0;
    message("worker-status", workers.length ? "" : "登録済みの Worker はありません。");
  } catch {
    if (!signal.aborted) message("worker-status", "Worker の状態を取得できませんでした。", true);
  }
}

async function readRuns(limit, signal) {
  let before = null;
  const items = [];
  do {
    const query = new URLSearchParams({limit: String(Math.min(100, limit - items.length))});
    if (before) query.set("before", before);
    const page = await read(`${showRunHistory ? "/v1/runs" : "/v1/operations/tasks"}?${query}`, signal);
    items.push(...(showRunHistory ? page.runs : page.tasks));
    before = page.next_before;
  } while (before && items.length < limit);
  return {runs: items, next_before: before};
}

function canRefreshBoard() {
  return !get("board").hidden && !document.hidden && !boardRequest;
}

function scheduleBoardRefresh() {
  clearTimeout(boardTimer);
  if (canRefreshBoard()) {
    boardTimer = setTimeout(() => {
      if (canRefreshBoard()) void loadBoard({quiet: true});
    }, BOARD_REFRESH_MS);
  }
}

async function loadBoard({older = false, quiet = false} = {}) {
  clearTimeout(boardTimer);
  boardRequest?.abort();
  boardRequest = new AbortController();
  const { signal } = boardRequest;
  const initial = get("board").hidden;
  const olderButton = get("older");
  const connectButton = get("connect").querySelector("button");
  olderButton.disabled = !quiet;
  connectButton.disabled = true;
  if (!quiet) message("status", "Run 一覧を取得中…");
  try {
    const limit = visibleRunLimit + (older ? RUN_PAGE_SIZE : 0);
    const [page, context] = await Promise.all([
      readRuns(limit, signal),
      read("/v1/operations/context", signal),
    ]);
    if (signal.aborted) return;
    renderExecution(context);
    if (initial || JSON.stringify(page.runs) !== JSON.stringify(runs)) {
      runs = page.runs;
      renderBoard();
      const current = !showRunHistory && detail.open && selected?.task_id &&
        runs.find((item) => item.task_id === selected.task_id);
      if (current && current.task_status !== selected.task_status &&
          (!selected.run_id || current.run_id === selected.run_id)) {
        selected = {...current};
        void loadDetail();
      }
    }
    visibleRunLimit = limit;
    get("token-login").hidden = get("login").hidden = true;
    get("board").hidden = get("board-navigation").hidden = get("toolbar").hidden = false;
    updateBoardNavigation();
    olderButton.hidden = !page.next_before;
    const kind = showRunHistory ? "Run" : "タスク";
    const status = runs.length ? `${kind} ${runs.length} 件を表示中` : "この Workspace にタスクはありません。";
    if (get("status").textContent !== status || get("status").classList.contains("error")) {
      message("status", status);
    }
    if (!quiet || get("worker-panel").open) {
      await loadWorkers(context.execution.workspace_id, signal);
    }
  } catch (error) {
    if (signal.aborted) return;
    if ([401, 403].includes(error.status)) {
      disconnect(account ? "ログインし直してください。" : "User トークンを確認してください。", true);
    } else {
      const status = initial ? "一覧を取得できませんでした。もう一度接続してください。"
        : "一覧を更新できませんでした。自動で再試行します。";
      if (get("status").textContent !== status) message("status", status, true);
    }
  } finally {
    if (boardRequest?.signal === signal) {
      boardRequest = undefined;
      olderButton.disabled = connectButton.disabled = false;
      scheduleBoardRefresh();
    }
  }
}

function date(seconds) {
  return seconds == null ? null : new Date(seconds * 1000).toLocaleString("ja-JP");
}

function fields(rows) {
  const list = element("dl");
  for (const [label, value] of rows) {
    list.append(element("dt", label), element("dd", value ?? "—"));
  }
  return list;
}

function section(title, ...children) {
  const node = element("section", undefined, "detail-section");
  node.append(element("h3", title), ...children);
  return node;
}

function foldSection(node, hint = "", open = false) {
  const fold = element("details", undefined, "detail-disclosure");
  fold.open = open;
  const title = node.querySelector("h3");
  const summary = element("summary");
  summary.append(element("span", title.textContent));
  if (hint) summary.append(element("small", hint));
  title.remove();
  fold.append(summary, node);
  return fold;
}

function mountDetailTabs(panels) {
  const tabs = get("detail-tabs");
  const body = get("detail-body");
  const offsets = new Map();
  let active;
  const buttons = panels.map(({id, label, content}) => {
    const button = element("button", label);
    button.type = "button";
    button.id = `detail-tab-${id}`;
    button.setAttribute("role", "tab");
    button.setAttribute("aria-controls", `detail-panel-${id}`);
    content.id = `detail-panel-${id}`;
    content.classList.add("detail-panel");
    content.setAttribute("role", "tabpanel");
    content.setAttribute("aria-labelledby", button.id);
    content.tabIndex = 0;
    button.addEventListener("click", () => activate(id));
    button.addEventListener("keydown", (event) => {
      const index = panels.findIndex((panel) => panel.id === id);
      const next = {ArrowRight: (index + 1) % panels.length,
        ArrowLeft: (index + panels.length - 1) % panels.length,
        Home: 0, End: panels.length - 1}[event.key];
      if (next === undefined) return;
      event.preventDefault();
      activate(panels[next].id);
      buttons[next].focus();
    });
    return button;
  });
  function activate(id) {
    if (active) offsets.set(active, body.scrollTop);
    detailTab = active = id;
    panels.forEach((panel, index) => {
      const selected = panel.id === id;
      panel.content.hidden = !selected;
      buttons[index].setAttribute("aria-selected", String(selected));
      buttons[index].tabIndex = selected ? 0 : -1;
    });
    body.scrollTop = offsets.get(id) ?? 0;
  }
  tabs.replaceChildren(...buttons);
  get("detail-content").replaceChildren(...panels.map(({content}) => content));
  tabs.hidden = false;
  activate(detailTab);
}

function taskSpecificationView(task, run) {
  const version = task.spec_versions.find(({version}) => version === run.spec_version);
  if (!version) return section("実行対象の仕様", element("p", "この実行に使った仕様を取得できませんでした。", "error"));
  const spec = version.body;
  const view = element("div");
  const latest = task.spec_versions.at(-1)?.version;
  view.append(element("p", `表示中の仕様 v${version.version}${latest > version.version ? ` · 最新の保存済み仕様は v${latest}` : ""}`, "detail-note"));
  view.append(section("目的", element("p", spec.task.objective, "task-chat-message")));
  const criteria = element("ol", undefined, "spec-criteria");
  for (const criterion of spec.task.acceptance_criteria) {
    const item = element("li");
    item.append(element("small", criterion.id), element("p", criterion.description));
    criteria.append(item);
  }
  view.append(section("受け入れ条件", spec.task.acceptance_criteria.length ? criteria
    : element("p", "未設定です。AIとの詳細化で提案・確定します。", "detail-note")));
  if (spec.task.constraints.length) {
    const constraints = element("ul");
    constraints.append(...spec.task.constraints.map((text) => element("li", text)));
    view.append(section("制約", constraints));
  }
  view.append(foldSection(section("実行対象・検証設定", fields([
    ["リポジトリ ID", spec.task.repository_id], ["対象ブランチ", spec.task.base_ref],
    ["検証プロファイル", spec.verification.profile_id],
    ["必須チェック", spec.verification.required_checks.join("、")],
  ]))));
  return view;
}

function records(title, items, rows) {
  return section(title, ...(items.length ? items.map((item) => {
    const node = element("div", undefined, "record");
    node.append(fields(rows(item)));
    return node;
  }) : [element("p", "記録なし")]));
}

function graphView(graph) {
  const nodes = graph.jobs.map((job) => {
    const node = element("details", undefined, "record");
    node.append(element("summary", `${job.kind} · ${job.state} · 試行 ${job.attempt_no}`));
    node.append(fields([
      ["Job ID", job.job_id], ["Step", job.step_key],
      ...(job.kind === "REPAIR" ? [["修正ラウンド", job.repair_round]] : []),
    ]));
    node.append(records("試行", job.attempts, (attempt) => [
      ["Attempt ID", attempt.attempt_id], ["状態", attempt.state],
      ["Worker", attempt.worker_id], ["Heartbeat", date(attempt.heartbeat_at)],
      ["Lease 期限", date(attempt.expires_at)], ["停止種別", attempt.stop_confirmation],
    ]));
    return node;
  });
  return section("ジョブ・試行・修正", ...(nodes.length ? nodes : [element("p", "記録なし")]),
    records("依存関係", graph.edges, (edge) => [["先行 Job", edge.from], ["後続 Job", edge.to]]));
}

function publicationView(publication) {
  const pr = publication.pull_request;
  return section("PR・公開処理",
    pr ? fields([
      ["PR 番号", pr.number], ["URL", pr.url], ["状態", pr.state],
      ["Draft", pr.draft], ["Head", pr.head_sha], ["観測時刻", date(pr.observed_at)],
    ]) : element("p", "PR の記録なし"),
    records("公開処理", publication.operations, (op) => [
      ["種別", op.kind], ["状態", op.state], ["理由", op.reason],
      ["照合試行", op.reconcile_attempts], ["次回照合", date(op.next_reconcile_at)],
      ...(op.required_action ? [["必要な判断", op.required_action]] : []),
    ]),
  );
}

function verifiedResultView(result, current) {
  return section("検証済み Draft PR",
    fields([
      ["成功確定時刻", date(result.verified_at)], ["検証時 Head", result.draft_pr.head_sha],
      ["Final Gate", result.final_gate_id],
    ]),
    records("成功確定時の CI", result.checks, (check) => [
      ["名前", check.check_name], ["状態", check.status], ["URL", check.url],
    ]),
    current?.status === "OBSERVED" ? fields([
      ["現在の PR", current.state], ["Draft", current.draft],
      ["現在の Head", current.head_sha],
      ["対象の比較", current.target_matches ? "検証時と同じ対象" : "現在コードは未検証"],
    ]) : element("p", "現在の PR を確認できませんでした。"),
  );
}

function ciView(context) {
  if (context?.kind !== "CI") return section("CI", element("p", "CI 待ちの記録なし"));
  return section("CI", fields([
    ["期限", date(context.deadline_at)], ["必須 Check", context.required_ci?.map((check) => check.check_name ?? check).join(", ")],
  ]), records("観測済み Check", context.checks ?? [], (check) => [
    ["名前", check.check_name ?? check.name ?? check.key], ["状態", check.status],
    ["結果", check.conclusion], ["Head", check.head_sha],
  ]));
}

function verificationView(verification) {
  return section("Check・Gate",
    records("Check", verification.checks, (check) => [
      ["種別", check.kind], ["キー", check.key], ["状態", check.status], ["出所", check.origin],
    ]),
    records("Gate", verification.gates, (gate) => [
      ["段階", gate.stage], ["状態", gate.status], ["理由", gate.reason],
    ]),
  );
}

function patchDownloads({artifacts}) {
  const patches = artifacts.filter(({kind, status}) => kind === "PATCH" && status === "AVAILABLE");
  if (!patches.length) return section("変更パッチ", element("p", "ダウンロードできるパッチはありません。"));
  const buttons = patches.map(({id}) => {
    const button = element("button", `パッチをダウンロード · ${id.slice(0, 8)}`);
    button.type = "button";
    button.addEventListener("click", async () => {
      button.disabled = true;
      const signal = detailRequest?.signal;
      try {
        const response = await fetch(`/v1/artifacts/${encodeURIComponent(id)}/content`, {
          headers: authHeaders(), signal, cache: "no-store", redirect: "error",
        });
        if (!response.ok) throw new ApiError(response.status);
        const blob = await response.blob();
        if (signal?.aborted) return;
        const url = URL.createObjectURL(blob);
        const link = element("a");
        link.href = url;
        link.download = `factory-${id}.patch`;
        link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
      } catch (error) {
        if (!signal?.aborted) message("detail-status", error.message, true);
      } finally {
        button.disabled = false;
      }
    });
    return button;
  });
  return section("変更パッチ", ...buttons);
}

const detailSections = [
  ["graph", "ジョブ・試行・修正", graphView],
  ["publication", "PR・公開処理", publicationView],
  ["artifacts", "変更パッチ", patchDownloads],
  ["approvals", "承認", (items) => records("承認", items, (item) => [
    ["判断", item.action], ["状態", item.status], ["期限", date(item.expires_at)],
    ["Approval ID", item.approval_id],
  ])],
  ["event-history", "履歴", ({ events }) => {
    const list = element("ol", undefined, "event-timeline");
    list.id = "run-event-list";
    list.append(...[...events].reverse().map(eventRecord));
    return section("実行イベント", list);
  }],
];

function eventRecord(event) {
  const labels = {
    RUN_CREATED: "実行を登録", RUN_COMPILED: "実行計画を作成", JOB_CLAIMED: "処理を開始",
    WORKER_EVENT: "実行状況を更新", ARTIFACT_AVAILABLE: "成果物を保存",
    USAGE_RECONCILED: "利用量を確定", BLOCKED: "対応待ちに移行",
    RUN_CANCELLED: "実行を取り消し", CANCELLED: "実行を取り消し",
    SUCCEEDED: "実行を完了", FAILED: "実行に失敗",
  };
  const record = element("li");
  const heading = element("div", undefined, "event-heading");
  const title = element("span", labels[event.kind] ?? event.kind);
  title.title = event.kind;
  heading.append(title, element("small", `Rev. ${event.revision}`));
  const timestamp = element("time", date(event.created_at));
  if (event.created_at != null) timestamp.dateTime = new Date(event.created_at * 1000).toISOString();
  record.append(heading, timestamp);
  return record;
}

async function followRunEvents(root, runId, revision, signal) {
  const list = get("run-event-list");
  while (!signal.aborted) {
    try {
      const headers = { ...authHeaders(), Accept: "text/event-stream" };
      if (revision !== undefined) headers["Last-Event-ID"] = `${runId}:${revision}`;
      const response = await fetch(`${root}/events`, {
        headers, cache: "no-store", signal, redirect: "error",
      });
      if (response.status === 410) {
        if (!signal.aborted) loadDetail();
        return;
      }
      if ([401, 403].includes(response.status)) {
        if (!signal.aborted) disconnect(account ? "ログインし直してください。" : "User トークンを確認してください。", true);
        return;
      }
      if (!response.ok) throw new ApiError(response.status);
      let buffer = "";
      for await (const chunk of response.body.pipeThrough(new TextDecoderStream())) {
        if (signal.aborted) return;
        buffer += chunk;
        let boundary;
        while ((boundary = buffer.indexOf("\n\n")) !== -1) {
          const frame = buffer.slice(0, boundary);
          buffer = buffer.slice(boundary + 2);
          if (frame.startsWith("event: reset\n")) {
            loadDetail();
            return;
          }
          const data = frame.split("\n").find((line) => line.startsWith("data: "));
          if (!data) continue;
          const event = JSON.parse(data.slice(6));
          if (revision !== undefined && event.revision <= revision) continue;
          list.prepend(eventRecord(event));
          revision = event.revision;
        }
      }
    } catch {
      if (!signal.aborted) message("detail-status", "履歴ストリームに再接続中…");
    }
    if (!signal.aborted) await new Promise((resolve) => setTimeout(resolve, 1000));
  }
}

const blockedGuidance = {
  PLAN_HAS_OPEN_QUESTIONS: "Codex の質問に答えて仕様を詰めます。確認が済むと仕様案が表示されます。",
  SPEC_CHANGE_REQUIRED: "目的や受け入れ条件を見直し、新しい仕様で実行してください。",
  REPLAN_LIMIT_REACHED: "計画の再作成上限に達しました。仕様を確認し、新しい Run で実行してください。",
  BUDGET_EXHAUSTED: "予算の残額を確認し、必要に応じて予算を変更してから再開してください。",
  BUDGET_EXCEEDED: "使用額と予算を確認し、原因を解消してから再開してください。",
  USAGE_UNKNOWN: "利用量を確定してから再開してください。",
  MANDATORY_EVIDENCE_MISSING: "不足している検証結果を確認してください。",
  EVIDENCE_TARGET_MISMATCH: "検証の対象と現在の候補が一致しているか確認してください。",
};
const recoverableBlocks = new Set([
  "BUDGET_EXHAUSTED", "BUDGET_EXCEEDED", "USAGE_UNKNOWN",
  "MANDATORY_EVIDENCE_MISSING", "EVIDENCE_TARGET_MISMATCH",
]);

function runActionMessage(error) {
  return {
    RUN_REVISION_CONFLICT: "Run の状態が変わりました。「詳細を更新」で最新の状態を確認してください。",
    INTERVENTION_TARGET_STALE: "再開条件が変わりました。「詳細を更新」で確認してください。",
    BUDGET_NOT_RESOLVED: "予算が不足しています。",
    USAGE_NOT_RECONCILED: "利用量がまだ確定していません。",
    EVIDENCE_NOT_RESOLVED: "必要な検証結果がまだ揃っていません。",
    WORKSPACE_EXECUTION_STOPPED: "Workspace の実行が停止中です。管理者に確認してください。",
  }[error.message] ?? `操作できませんでした（${error.message}）。`;
}

function blockedForm(label, status, action) {
  const form = element("form", undefined, "attention-form");
  const submit = element("button", label);
  submit.type = "submit";
  form.append(submit);
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    status.textContent = "処理中…";
    status.classList.remove("error");
    try {
      await action(form);
    } catch (error) {
      if (error.name !== "AbortError") {
        status.textContent = runActionMessage(error);
        status.classList.add("error");
      }
    } finally {
      submit.disabled = false;
    }
  });
  return form;
}

function attentionField(label, name, value, maxLength, rows) {
  const wrapper = element("label", label);
  const input = element(rows ? "textarea" : "input");
  input.name = name;
  input.value = value;
  input.maxLength = maxLength;
  input.required = true;
  if (rows) input.rows = rows;
  wrapper.append(input);
  return wrapper;
}

function specCorrection(run, task, status, signal) {
  const original = task.spec_versions.at(-1);
  if (!original) return null;
  const root = `/v1/tasks/${encodeURIComponent(run.task_id)}`;
  const spec = original.body;
  const criterionText = spec.task.acceptance_criteria.map(({description}) => description).join("\n");
  let pending;
  let saved;
  const runKey = taskKey();
  const form = blockedForm("仕様を保存して再実行", status, async (form) => {
    if (!saved) {
      const formData = new FormData(form);
      const objective = formData.get("objective").trim();
      const criteria = formData.get("criteria");
      const lines = criteria.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
      if (!objective || !lines.length || lines.length > 100 || lines.some((line) => line.length > 4000)) {
        throw new Error("目的と受け入れ条件を確認してください");
      }
      if (objective === spec.task.objective && criteria === criterionText) {
        if (original.version === run.spec_version &&
            ["PLAN_HAS_OPEN_QUESTIONS", "SPEC_CHANGE_REQUIRED"].includes(run.stop_reason)) {
          throw new Error("Codexへの回答を目的または受け入れ条件に追記してください");
        }
        saved = {spec_version: original.version};
      } else {
        const acceptance = criteria === criterionText ? spec.task.acceptance_criteria
          : lines.map((description, index) => ({id: `AC-${index + 1}`, description}));
        const body = JSON.stringify({...spec, task: {...spec.task, objective, acceptance_criteria: acceptance}});
        if (pending?.body !== body) pending = {body, key: taskKey()};
        saved = await postTask(`${root}/spec-versions`, body, pending.key, signal);
      }
      if (signal.aborted) return;
      for (const input of form.querySelectorAll("textarea")) input.readOnly = true;
      form.querySelector("button").textContent = `仕様 v${saved.spec_version} で実行`;
    }
    const result = await postTask(`${root}/runs`, JSON.stringify({spec_version: saved.spec_version, clarification_only: true}), runKey, signal);
    if (!signal.aborted) {
      selected = {...selected, run_id: result.run_id};
      void loadBoard();
      void loadDetail(`更新した仕様 v${saved.spec_version} で再実行を登録しました。`);
    }
  });
  form.prepend(
    element("p", `仕様 v${original.version} を編集中です。変更は新しい仕様として保存されます。`),
    attentionField("目的", "objective", spec.task.objective, 16000, 4),
    attentionField("受け入れ条件（1行に1件）", "criteria", criterionText, 400099, 5),
  );
  const editor = element("details");
  editor.append(element("summary", "仕様を修正して再実行"), form);
  return editor;
}

function mountAttentionSurface(root, run, container, signal) {
  const drafts = {};
  const notice = element("p", "質問を取得中…");
  notice.setAttribute("role", "status");
  const content = element("div");
  container.append(notice, content);
  let refreshTimer;
  let refreshVersion = 0;
  let snapshot;
  let request;
  let sending = false;

  async function handle(envelope) {
    if (sending) return;
    const {timestamp, ...action} = envelope.action;
    const payload = JSON.stringify(action);
    if (request?.payload !== payload) request = {payload, key: taskKey()};
    sending = true;
    ++refreshVersion;
    clearTimeout(refreshTimer);
    notice.textContent = "送信中…";
    notice.classList.remove("error");
    try {
      await postTask(`${root}/attention/actions`, JSON.stringify(envelope), request.key, signal);
      if (action.name === "adopt_specification") {
        void loadBoard();
        void loadDetail("仕様の詳細化が完了しました。実行を開始できます。");
        return;
      }
      snapshot = undefined;
      await refresh();
      request = undefined;
    } catch (error) {
      if (signal.aborted) return;
      notice.textContent = {
        ATTENTION_ANSWER_REQUIRED: "候補を選ぶか、「その他」から回答を入力してください。",
        ATTENTION_SURFACE_STALE: "質問や仕様が変わりました。「詳細を更新」で確認してください。",
        TASK_CHAT_BUSY: "Codex が別の応答を処理中です。少し待って送信してください。",
        TASK_CHAT_DAILY_LIMIT: "本日の Codex 対話の上限に達しました。",
        INTERVIEW_SPECIFICATION_INVALID: "仕様案を保存できませんでした。",
      }[error.message] ?? runActionMessage(error);
      notice.classList.add("error");
      if (error.message === "TASK_CHAT_BUSY") refreshTimer = setTimeout(poll, 2000);
    } finally {
      sending = false;
    }
  }

  function openRun(runId, message) {
    if (signal.aborted) return;
    selected = {...selected, run_id: runId};
    void loadBoard();
    void loadDetail(message);
  }

  function specificationView(state) {
    const {specification, adopted_version: adopted, resumed_run_id: resumed} = state;
    const view = element("div");
    const criteria = element("ul");
    criteria.append(...specification.acceptance_criteria.map(({description}) =>
      element("li", description)));
    view.append(
      element("h4", adopted ? `採用した仕様 v${adopted}` : "Codex がまとめた仕様案"),
      element("h4", "目的"),
      element("p", specification.objective, "task-chat-message"),
      element("h4", "受け入れ条件"),
      criteria,
    );
    if (resumed) {
      const open = element("button", "実行した Run を開く");
      open.type = "button";
      open.addEventListener("click", () => openRun(resumed));
      view.append(open);
    } else if (adopted) {
      view.append(blockedForm("採用した仕様で実行", notice, async () => {
        const taskRoot = `/v1/tasks/${encodeURIComponent(run.task_id)}`;
        const key = `attention-run-${run.run_id}-${adopted}`;
        const result = await postTask(`${taskRoot}/runs`,
          JSON.stringify({spec_version: adopted, reuse_existing: true}), key, signal);
        openRun(result.run_id, `採用した仕様 v${adopted} の Run を表示しています。`);
      }));
    }
    return view;
  }

  async function refresh() {
    const version = ++refreshVersion;
    const state = await read(`${root}/attention`, signal);
    if (signal.aborted || version !== refreshVersion) return;
    const updated = JSON.stringify(state);
    if (snapshot !== updated) {
      const focused = document.activeElement;
      const inputId = content.contains(focused) && focused.dataset.a2uiId;
      const sameQuestion = drafts.surfaceId === state.surface_id;
      const nodes = [];
      if (state.specification) nodes.push(specificationView(state));
      if (state.messages.length) nodes.push(renderAttention(state.messages, drafts, handle));
      if (state.history?.length) {
        const history = element("details", undefined, "interview-history");
        history.open = content.querySelector("details")?.open ?? false;
        history.append(element("summary", `これまでの回答（${state.history.length}件）`));
        for (const {question, answer} of state.history) {
          history.append(element("h4", question), element("p", answer, "task-chat-message"));
        }
        nodes.push(history);
      }
      content.replaceChildren(...nodes);
      if (sameQuestion && inputId) {
        const input = content.querySelector(`[data-a2ui-id="${CSS.escape(inputId)}"]`);
        input?.focus({preventScroll: true});
        input?.setSelectionRange(focused.selectionStart, focused.selectionEnd);
      }
      notice.textContent = !state.available ? "この Run では Codex との仕様確認を利用できません。"
        : state.waiting_reason === "WORKSPACE_EXECUTION_STOPPED" ? "Workspace の緊急停止を解除すると、Codex による詳細化を開始できます。"
        : state.waiting_reason === "TASK_CHAT_DAILY_LIMIT" ? "本日の Codex 対話の上限に達しました。利用枠が回復してから「詳細を更新」で再試行してください。"
        : state.adopted_version ? "仕様を採用しました。"
        : state.status === "FAILED" ? "Codex の応答を取得できませんでした。回答を送り直せます。"
        : state.proposing ? (run.snapshot.task_spec.task.acceptance_criteria.length
          ? "回答候補を準備中です。" : "Codex が目的から質問と仕様案を準備しています…")
        : state.pending ? "Codex が追加で確認する点と仕様案を検討しています…"
        : state.specification ? "仕様案がまとまりました。"
        : state.history?.length ? "Codex から追加の質問があります。"
        : "";
      notice.classList.toggle("error", state.status === "FAILED");
      snapshot = updated;
    }
    if (state.pending) refreshTimer = setTimeout(poll, 2000);
  }

  function poll() {
    if (signal.aborted) return;
    void refresh().catch(() => {
      if (signal.aborted) return;
      notice.textContent = "対話を取得できませんでした。「詳細を更新」で再試行してください。";
      notice.classList.add("error");
    });
  }

  signal.addEventListener("abort", () => clearTimeout(refreshTimer), {once: true});
  poll();
}

async function blockedRunView(root, run, signal, task) {
  const operator = ["ADMIN", "OPERATOR"].includes(executionContext?.access);
  const [plans, targets] = await Promise.allSettled([
    read(`${root}/plan`, signal),
    operator ? read("/v1/operations/task-targets", signal) : null,
  ]);
  if (signal.aborted) return null;
  const status = element("p");
  status.setAttribute("role", "status");
  const panel = element("section", undefined, "detail-section attention-section");
  panel.dataset.state = boardStatus(run);
  const alert = element("div", undefined, "attention-alert");
  alert.append(element("h3", "次にすること"),
    element("p", blockedGuidance[run.stop_reason] ?? "停止理由を確認し、必要な修正を行ってください。"));
  panel.append(alert);
  const questions = plans.value?.plans.at(-1)?.body?.open_questions;
  const questionContent = element("div");
  if (Array.isArray(questions) && questions.length) {
    if (operator) {
      panel.append(element("h4", "Codex と仕様を確認"), questionContent);
      mountAttentionSurface(root, run, questionContent, signal);
    } else {
      const list = element("ul");
      list.append(...questions.map((question) => element("li", question)));
      panel.append(element("h4", "Codex からの質問"), list);
    }
  } else if (plans.status === "rejected") {
    panel.append(element("p", "Codex の計画を取得できませんでした。", "error"));
  }
  const repositoryId = task.value?.spec_versions.at(-1)?.body.task.repository_id;
  const project = targets.value?.projects.find(({repositories}) =>
    repositories.some(({id}) => id === repositoryId));
  if (project?.allowed_write_paths?.length) {
    panel.append(foldSection(section("変更できる範囲",
      element("p", project.allowed_write_paths.join("、")))));
  }
  if (!operator) {
    panel.append(element("p", "対応するには操作者または管理者の権限が必要です。"));
    return panel;
  }
  panel.append(status);
  const canCorrectSpec = ["PLAN", "WAIT_PLAN_APPROVAL"].includes(run.phase) ||
    run.stop_reason === "SPEC_CHANGE_REQUIRED";
  if (canCorrectSpec && !questions?.length) {
    const correction = task.value && specCorrection(run, task.value, status, signal);
    panel.append(correction ?? element("p", "タスクの仕様を取得できませんでした。", "error"));
  }
  if (recoverableBlocks.has(run.stop_reason)) {
    const key = taskKey();
    const resolve = blockedForm("解消を確認して再開", status, async (form) => {
      const reason = new FormData(form).get("reason").trim();
      const target = await read(`${root}/interventions`, signal);
      await postTask(`${root}/interventions`, JSON.stringify({
        action: "resolve_blocked", subject_id: run.run_id, reason,
        target_hash: target.target_hash, expected_run_revision: run.state_revision,
      }), key, signal);
      if (!signal.aborted) {
        void loadBoard();
        void loadDetail("Run を再開しました。");
      }
    });
    resolve.prepend(attentionField("解消した内容", "reason", "", 4000, 2));
    panel.append(resolve);
  }
  const key = taskKey();
  const cancel = blockedForm("この Run を取り消す", status, async (form) => {
    const reason = new FormData(form).get("reason").trim();
    const result = await postTask(`${root}/cancel`, JSON.stringify({
      expected_run_revision: run.state_revision, reason,
    }), key, signal);
    if (!signal.aborted) {
      void loadBoard();
      void loadDetail(result.status === "CANCELLED" ? "Run を取り消しました。" : "Run の取り消しを受け付けました。");
    }
  });
  cancel.prepend(attentionField("取り消し理由", "reason", "", 2000, 2));
  const cancelDetails = element("details", undefined, "detail-disclosure cancel-disclosure");
  cancelDetails.append(element("summary", "この Run を取り消す"), cancel);
  panel.append(cancelDetails);
  return panel;
}

function taskChat(root, signal) {
  const transcript = element("div", undefined, "task-chat-history");
  transcript.setAttribute("aria-label", "会話履歴");
  const status = element("p");
  status.setAttribute("role", "status");
  const operator = ["ADMIN", "OPERATOR"].includes(executionContext?.access);
  const form = element("form", undefined, "attention-form");
  const field = attentionField("メッセージ", "message", "", 4000, 3);
  const input = field.querySelector("textarea");
  input.placeholder = "Codex に相談したい内容を入力してください。";
  const send = element("button", "Codex に送信");
  send.type = "submit";
  form.append(field, send);
  form.hidden = true;
  const panel = section("Codex とチャット", transcript, status, form);
  let sent;
  let displayed;
  let timer;

  async function refresh() {
    clearTimeout(timer);
    const {available, turns} = await read(`${root}/chat`, signal);
    if (signal.aborted) return;
    form.hidden = !available || !operator;
    const snapshot = JSON.stringify(turns);
    if (snapshot !== displayed) {
      displayed = snapshot;
      transcript.replaceChildren(...turns.map((turn) => {
        const question = element("p", turn.question, "task-chat-message");
        const rows = [element("h4", "あなたのチーム"), question];
        if (turn.answer) rows.push(element("h4", "Codex"), element("p", turn.answer, "task-chat-message"));
        if (turn.status === "FAILED") {
          const explanation = {
            CHAT_TIMEOUT: "返答を受信できませんでした。Worker の状態を確認して再送してください。",
            CODEX_AUTH_OR_POLICY: "Codex の連携設定と実行ポリシーを確認してください。",
          }[turn.error] ?? "Codex の返答を取得できませんでした。";
          rows.push(element("p", explanation, "error"));
        }
        const article = element("article", undefined, "task-chat-turn");
        article.append(...rows);
        return article;
      }));
    }
    const waiting = turns.some(({status}) => status === "QUEUED" || status === "RUNNING");
    send.disabled = waiting;
    status.textContent = waiting ? "Codex の返答を待っています…" :
      !available ? "この Run では Codex チャットを利用できません。" :
      !operator ? "チャットの送信には操作者または管理者の権限が必要です。" :
      turns.length ? "" : "タスクの内容や停止理由について質問できます。";
    if (waiting) timer = setTimeout(poll, 2000);
  }

  function poll() {
    if (signal.aborted) return;
    void refresh().catch(() => {
      if (signal.aborted) return;
      status.textContent = "チャットを取得できませんでした。再試行します。";
      timer = setTimeout(poll, 5000);
    });
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = input.value.trim();
    if (!message || send.disabled) return;
    if (sent?.message !== message) sent = {message, key: taskKey()};
    send.disabled = true;
    input.readOnly = true;
    try {
      await postTask(`${root}/chat`, JSON.stringify({message}), sent.key, signal);
      if (signal.aborted) return;
      input.value = "";
      sent = undefined;
      status.textContent = "Codex の返答を待っています…";
      poll();
    } catch (error) {
      if (signal.aborted) return;
      status.textContent = {
        TASK_CHAT_BUSY: "別のチャットが処理中です。少し待ってから再送してください。",
        TASK_CHAT_DAILY_LIMIT: "この Workspace のチャット利用上限に達しました。",
        SENSITIVE_INPUT: "認証情報を含まないメッセージにしてください。",
      }[error.message] ?? runActionMessage(error);
      send.disabled = false;
    } finally {
      input.readOnly = false;
    }
  });

  signal.addEventListener("abort", () => clearTimeout(timer), {once: true});
  poll();
  return panel;
}

function readyToExecute(run, task, signal) {
  const status = element("p");
  status.setAttribute("role", "status");
  const panel = section("実行の準備ができました", element("p", "仕様の詳細化が完了しました。実行開始を押すと実装を開始します。"), status);
  if (!["ADMIN", "OPERATOR"].includes(executionContext?.access)) return panel;
  const key = taskKey();
  panel.append(blockedForm("実行開始", status, async () => {
    let result;
    if (run.phase === "WAIT_PLAN_APPROVAL" && run.status === "WAITING_EXTERNAL" &&
        run.spec_version === task.latest_spec_version) {
      const approval = await read(`/v1/approvals/${encodeURIComponent(run.wait_context.approval_id)}`, signal);
      result = await postTask(`/v1/approvals/${encodeURIComponent(approval.approval_id)}/decisions`,
        JSON.stringify({expected_run_revision: run.state_revision, target_hash: approval.target_hash,
          decision: "approve", reason: "詳細化した仕様と計画の実行を開始"}), key, signal);
      result = result.run;
    } else {
      result = await postTask(`/v1/tasks/${encodeURIComponent(run.task_id)}/runs`,
        JSON.stringify({spec_version: task.latest_spec_version, reuse_existing: true}), key, signal);
    }
    if (signal.aborted) return;
    selected = {...selected, run_id: result.run_id, task_status: undefined};
    void loadBoard();
    void loadDetail("実行を開始しました。");
  }));
  return panel;
}

async function loadTaskDetail(notice = "") {
  detailRequest?.abort();
  detailRequest = new AbortController();
  const {signal} = detailRequest;
  const key = `task:${selected.task_id}`;
  if (detailTabRunId !== key) { detailTab = "conversation"; detailTabRunId = key; }
  get("detail-title").textContent = selected.task_title;
  get("detail-content").replaceChildren();
  get("detail-summary").replaceChildren();
  get("detail-tabs").hidden = true;
  get("reload-detail").disabled = true;
  message("detail-status", "タスク詳細を取得中…");
  try {
    const task = await read(`/v1/tasks/${encodeURIComponent(selected.task_id)}`, signal);
    if (signal.aborted) return;
    if (task.task_status !== "DRAFT" && task.runs.length) {
      selected = {...selected, run_id: task.runs.at(-1).run_id, task_status: task.task_status};
      void loadDetail(notice);
      return;
    }
    const badge = element("span", "詳細化前", "detail-state");
    badge.dataset.state = "DRAFT";
    get("detail-summary").append(badge, element("span", `仕様 v${task.latest_spec_version}`));
    const status = element("p");
    status.setAttribute("role", "status");
    const conversation = section("AIによる仕様の詳細化", element("p", "目的をもとに、AIが確認する点や受け入れ条件を提案します。"), status);
    if (["ADMIN", "OPERATOR"].includes(executionContext?.access)) {
      const startKey = taskKey();
      conversation.append(blockedForm("AIによる詳細化を開始", status, async () => {
        const run = await postTask(`/v1/tasks/${encodeURIComponent(task.task_id)}/runs`,
          JSON.stringify({spec_version: task.latest_spec_version, clarification_only: true, reuse_existing: true}), startKey, signal);
        if (signal.aborted) return;
        selected = {...run, task_id: task.task_id, task_title: task.title};
        void loadBoard();
        void loadDetail("AIによる仕様の詳細化を開始しました。");
      }));
    }
    const history = section("これまでの実行");
    if (!task.runs.length) history.append(element("p", "まだ実行はありません。"));
    for (const run of [...task.runs].reverse()) {
      const open = element("button", `仕様 v${run.spec_version} · ${run.run_id.slice(0, 8)}`);
      open.type = "button";
      open.addEventListener("click", () => {
        selected = {...run, task_title: task.title};
        void loadDetail();
      });
      history.append(open);
    }
    mountDetailTabs([
      {id: "conversation", label: "対応・会話", content: conversation},
      {id: "specification", label: "仕様", content: taskSpecificationView(task, {spec_version: task.latest_spec_version})},
      {id: "history", label: "実行履歴", content: history},
    ]);
    message("detail-status", notice);
  } catch (error) {
    if (!signal.aborted) message("detail-status", error.message, true);
  } finally {
    if (detailRequest.signal === signal) get("reload-detail").disabled = false;
  }
}

async function loadDetail(notice = "") {
  if (!selected.run_id || selected.task_status === "DRAFT") return loadTaskDetail(notice);
  detailRequest?.abort();
  detailRequest = new AbortController();
  const { signal } = detailRequest;
  const root = `/v1/runs/${encodeURIComponent(selected.run_id)}`;
  if (detailTabRunId !== selected.run_id) {
    detailTab = "conversation";
    detailTabRunId = selected.run_id;
  }
  get("detail-title").textContent = selected.task_title;
  get("detail-content").replaceChildren();
  get("detail-summary").replaceChildren();
  get("detail-tabs").hidden = true;
  get("reload-detail").disabled = true;
  message("detail-status", "タスク詳細を取得中…");

  try {
    const [runResult, ...results] = await Promise.allSettled([
      read(root, signal),
      ...detailSections.map(([path]) => read(`${root}/${path}`, signal)),
    ]);
    if (signal.aborted) return;
    if (runResult.status === "rejected") throw runResult.reason;
    const run = runResult.value;
    selected = {...selected, task_id: run.task_id, task_status: run.task_status};
    const candidateId = run.current_candidate_id;
    const candidatePath = `/v1/candidates/${encodeURIComponent(candidateId)}`;
    const [candidateResults, verifiedResults, [taskResult]] = await Promise.all([
      candidateId ? Promise.allSettled([
        read(candidatePath, signal), read(`${candidatePath}/verification`, signal),
      ]) : [],
      run.status === "SUCCEEDED" ? Promise.allSettled([
        read(`${root}/result`, signal), read(`${root}/result/current-pr`, signal),
      ]) : [],
      Promise.allSettled([read(`/v1/tasks/${encodeURIComponent(run.task_id)}`, signal)]),
    ]);
    if (signal.aborted) return;
    const allResults = [taskResult, ...results, ...candidateResults, ...verifiedResults];
    const denied = allResults.find(({ reason }) => [401, 403].includes(reason?.status));
    if (denied) throw denied.reason;
    const context = run.wait_context;
    const ready = boardStatus(run) === "READY";
    const specVersion = ready ? taskResult.value?.latest_spec_version ?? run.spec_version : run.spec_version;
    const badge = element("span", lanes.get(boardStatus(run)), "detail-state");
    badge.dataset.state = boardStatus(run);
    get("detail-summary").append(badge,
      element("span", ready ? "仕様の詳細化完了" : resultLabels.get(run.status) ?? phaseLabels.get(run.phase) ?? run.phase),
      element("span", `仕様 v${specVersion}`, "detail-spec-version"));

    const conversation = element("div");
    if (ready && taskResult.status === "fulfilled") {
      conversation.append(readyToExecute(run, taskResult.value, signal));
    } else if (run.status === "BLOCKED") {
      const attention = await blockedRunView(root, run, signal, taskResult);
      if (signal.aborted) return;
      if (attention) conversation.append(attention);
    } else {
      const guidance = {
        QUEUED: boardStatus(run) === "CLARIFYING" ? "AIによる仕様の詳細化を開始しています。" : "実行を開始しています。",
        RUNNING: `${phaseLabels.get(run.phase) ?? run.phase}を進めています。`,
        WAITING_EXTERNAL: "外部処理の結果を待っています。",
        CANCELLING: "実行の取り消しを処理しています。",
        CANCELLED: "この実行は取り消されました。仕様と実行履歴を確認できます。",
        SUCCEEDED: "実行が完了しました。成果物と検証結果は「実行履歴」で確認できます。",
        FAILED: "実行に失敗しました。「実行履歴」で停止理由と検証結果を確認してください。",
      }[run.status];
      if (guidance) conversation.append(element("p", guidance, "detail-guidance"));
    }
    const chat = taskChat(root, signal);
    conversation.append(run.status === "BLOCKED" ? foldSection(chat, "質問・相談") : chat);

    const history = element("div");
    // Keep every diagnostic available, but show the timeline before the technical details.
    const views = results.map((result, index) => {
      const [, title, view] = detailSections[index];
      return result.status === "fulfilled" ? view(result.value)
        : section(title, element("p", "取得できませんでした。", "error"));
    });
    history.append(views.at(-1));
    history.append(foldSection(views[0], results[0].status === "fulfilled"
      ? `${results[0].value.jobs.length} ジョブ` : "取得エラー"));
    const outputs = section("成果物・検証結果");
    if (verifiedResults[0]?.status === "fulfilled") {
      outputs.append(verifiedResultView(verifiedResults[0].value, verifiedResults[1].value));
    } else if (run.status === "SUCCEEDED") {
      outputs.append(element("p", "検証済みの結果を取得できませんでした。", "error"));
    }
    outputs.append(views[1], views[2]);
    if (run.status !== "SUCCEEDED") outputs.append(ciView(context));
    if (candidateId) outputs.append(candidateResults[1].status === "fulfilled"
      ? verificationView(candidateResults[1].value)
      : section("Check・Gate", element("p", "取得できませんでした。", "error")));
    history.append(foldSection(outputs, "PR・パッチ・チェック", run.status === "SUCCEEDED"));
    history.append(foldSection(views[3]));
    const usd = (value) => value == null ? "—" : new Intl.NumberFormat("ja-JP", {
      style: "currency", currency: "USD", maximumFractionDigits: 6,
    }).format(value / 1000000);
    history.append(foldSection(section("費用", fields([
      ["消費", usd(run.spent_microusd)], ["予約", usd(run.reserved_microusd)],
      ["予算", usd(run.budget_microusd)], ["費用状態", run.cost_state],
    ])), `${usd(run.spent_microusd)} / ${usd(run.budget_microusd)} USD`));
    history.append(foldSection(section("実行の技術情報", fields([
      ["Run ID", run.run_id], ["状態コード", run.status], ["段階コード", run.phase],
      ["Revision", run.state_revision], ["停止理由", run.stop_reason],
      ["終了日時", date(run.finished_at)],
      ...(run.evidence_expired_at == null ? [] : [["証拠の保持期限", "回収済み"]]),
      ["待機種別", context?.kind], ["要対応理由", context?.cause],
    ]), section("現候補", candidateId ? fields([
      ["Candidate ID", candidateId], ["世代", candidateResults[0].value?.generation],
      ["Head", candidateResults[0].value?.head_sha],
    ]) : element("p", "現候補なし")))));
    const specification = taskResult.status === "fulfilled" ? taskSpecificationView(taskResult.value, {...run, spec_version: specVersion})
      : section("仕様", element("p", "仕様を取得できませんでした。「詳細を更新」で再試行してください。", "error"));
    mountDetailTabs([
      {id: "conversation", label: "対応・会話", content: conversation},
      {id: "specification", label: "仕様", content: specification},
      {id: "history", label: "実行履歴", content: history},
    ]);
    const incomplete = allResults.some(({ status }) => status === "rejected");
    message("detail-status", incomplete ? "一部の詳細を取得できませんでした。" : notice, incomplete);
    const events = results.at(-1).value;
    if (events) followRunEvents(root, run.run_id, events.events.at(-1)?.revision, signal);
  } catch (error) {
    if (signal.aborted) return;
    if ([401, 403].includes(error.status)) {
      disconnect(account ? "ログインし直してください。" : "User トークンを確認してください。", true);
    } else {
      message("detail-status", error instanceof ApiError ? error.message : "詳細を取得できませんでした。", true);
    }
  } finally {
    if (detailRequest.signal === signal) get("reload-detail").disabled = false;
  }
}

function updateBoardNavigation() {
  const board = get("board");
  get("board-previous").disabled = board.scrollLeft <= 1;
  get("board-next").disabled = board.scrollLeft + board.clientWidth >= board.scrollWidth - 1;
}

for (const [id, direction] of [["board-previous", -1], ["board-next", 1]]) {
  get(id).addEventListener("click", () => {
    const board = get("board");
    const laneWidth = board.firstElementChild?.getBoundingClientRect().width ?? board.clientWidth;
    const gap = Number.parseFloat(getComputedStyle(board).columnGap);
    board.scrollBy({left: direction * (laneWidth + gap)});
  });
}
get("board").addEventListener("scroll", updateBoardNavigation, {passive: true});
window.addEventListener("resize", updateBoardNavigation);

get("connect").addEventListener("submit", (event) => {
  event.preventDefault();
  token = get("token").value.trim();
  get("token").value = "";
  if (token) loadBoard();
});
toolbar.addEventListener("click", (event) => {
  if (event.target.closest(".masthead-menu-panel > button")) {
    toolbar.open = false;
    menuToggle.focus();
  }
}, {capture: true});
toolbar.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && toolbar.open) {
    event.preventDefault();
    toolbar.open = false;
    menuToggle.focus();
  }
});
document.addEventListener("click", (event) => {
  if (toolbar.open && !toolbar.contains(event.target)) toolbar.open = false;
});
get("run-history-toggle").addEventListener("click", (event) => {
  showRunHistory = !showRunHistory;
  event.currentTarget.setAttribute("aria-pressed", String(showRunHistory));
  visibleRunLimit = RUN_PAGE_SIZE;
  void loadBoard();
});
get("older").addEventListener("click", () => loadBoard({older: true}));
document.addEventListener("visibilitychange", () => {
  clearTimeout(boardTimer);
  if (canRefreshBoard()) void loadBoard({quiet: true});
});
get("disconnect").addEventListener("click", async () => {
  const button = get("disconnect");
  button.disabled = true;
  try {
    if (account) await accountCall("/logout", "POST");
    disconnect(account ? "ログアウトしました。" : "接続を終了しました。");
  } catch {
    message("status", "ログアウトできませんでした。もう一度お試しください。", true);
  } finally {
    button.disabled = false;
  }
});
function detailWidthLimits() {
  const max = document.documentElement.clientWidth;
  const minWidth = Number.parseInt(getComputedStyle(detail).getPropertyValue("--detail-min-width"), 10);
  return {min: Math.min(minWidth, max), max};
}

function syncDetailResize() {
  const {min, max} = detailWidthLimits();
  detailResize.hidden = min === max;
  detailResize.setAttribute("aria-valuemin", min);
  detailResize.setAttribute("aria-valuemax", max);
  detailResize.setAttribute("aria-valuenow", detail.offsetWidth);
  detailResize.setAttribute("aria-valuetext", `${detail.offsetWidth} ピクセル`);
}

function setDetailWidth(requested) {
  const {min, max} = detailWidthLimits();
  detail.style.setProperty("--detail-width", `${Math.max(min, Math.min(max, requested))}px`);
  syncDetailResize();
}

function saveDetailWidth() {
  try {
    localStorage.setItem(detailWidthKey, String(detail.offsetWidth));
  } catch {
    // Resizing remains available when storage is disabled.
  }
}

try {
  const savedWidth = Number(localStorage.getItem(detailWidthKey));
  if (Number.isFinite(savedWidth) && savedWidth > 0) {
    detail.style.setProperty("--detail-width", `${savedWidth}px`);
  }
} catch {
  // Use the responsive default when storage is disabled.
}

let detailDrag;
detailResize.addEventListener("pointerdown", (event) => {
  if (!event.isPrimary || event.button !== 0) return;
  detailDrag = {pointer: event.pointerId, x: event.clientX, width: detail.offsetWidth};
  detailResize.setPointerCapture(event.pointerId);
  detailResize.focus({preventScroll: true});
  event.preventDefault();
});
detailResize.addEventListener("pointermove", (event) => {
  if (detailDrag?.pointer !== event.pointerId) return;
  setDetailWidth(detailDrag.width + detailDrag.x - event.clientX);
});
detailResize.addEventListener("lostpointercapture", (event) => {
  if (detailDrag?.pointer !== event.pointerId) return;
  if (detail.open && detail.offsetWidth !== detailDrag.width) saveDetailWidth();
  detailDrag = undefined;
});
detailResize.addEventListener("keydown", (event) => {
  const {min, max} = detailWidthLimits();
  const widths = {
    ArrowLeft: detail.offsetWidth + 24,
    ArrowRight: detail.offsetWidth - 24,
    Home: min,
    End: max,
  };
  if (!Object.hasOwn(widths, event.key)) return;
  event.preventDefault();
  setDetailWidth(widths[event.key]);
  saveDetailWidth();
});
window.addEventListener("resize", () => {
  if (detail.open) syncDetailResize();
});

// Older browsers ignore closedby. Require the click to begin and end on the backdrop.
if (detail.closedBy !== "any") {
  let startedOnBackdrop = false;

  function hitsDetailBackdrop(event) {
    if (event.target !== detail) return false;
    const {left, right, top, bottom} = detail.getBoundingClientRect();
    return event.clientX < left || event.clientX >= right ||
      event.clientY < top || event.clientY >= bottom;
  }

  detail.addEventListener("pointerdown", (event) => {
    if (event.isPrimary) startedOnBackdrop = event.button === 0 && hitsDetailBackdrop(event);
  });
  detail.addEventListener("pointercancel", () => { startedOnBackdrop = false; });
  detail.addEventListener("click", (event) => {
    const dismiss = startedOnBackdrop && event.detail > 0 && hitsDetailBackdrop(event);
    startedOnBackdrop = false;
    if (dismiss) detail.close();
  });
}

get("reload-detail").addEventListener("click", () => loadDetail());
detail.addEventListener("close", () => {
  if (detail.open) return;
  detailRequest?.abort();
  get("detail-content").replaceChildren();
});

get("execution-action").addEventListener("click", openExecutionForm);
get("execution-dismiss").addEventListener("click", () => {
  closeExecutionForm();
  get("execution-action").focus();
});
get("execution-form").addEventListener("submit", submitExecution);

const accountsDialog = get("accounts-dialog");
const accountForm = get("account-create");
let accountRequest;

async function accountCall(path, method = "GET", body, signal) {
  const response = await fetch("/v1/auth" + path, {
    method, signal, redirect: "error", cache: "no-store",
    headers: {...authHeaders(), ...(body === undefined ? {} : {"Content-Type": "application/json"})},
    ...(body === undefined ? {} : {body: JSON.stringify(body)}),
  });
  if (!response.ok) {
    const error = new ApiError(response.status);
    error.code = (await response.json().catch(() => ({}))).code;
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

get("login").addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!secureAccounts) return;
  const button = event.submitter;
  const body = Object.fromEntries(new FormData(event.currentTarget));
  get("login-password").value = "";
  button.disabled = true;
  try {
    account = await accountCall("/login", "POST", body);
    token = "";
    await loadBoard();
  } catch (error) {
    message("status", error.status === 401 ? "ユーザー名またはパスワードを確認してください。" :
      "ログインできませんでした。少し待ってもう一度お試しください。", true);
  } finally {
    button.disabled = false;
  }
});

async function restoreAccount() {
  get("token-login").open = !secureAccounts;
  if (!secureAccounts) {
    message("status", "トークンで接続すると Run を表示します。");
    return;
  }
  try {
    const saved = await accountCall("/session");
    if (token) return;
    if (saved) {
      account = saved;
      await loadBoard();
      return;
    }
  } catch {
    // The token sign-in path remains available during a session lookup failure.
  }
  if (!token) get("login").hidden = false;
}

const accessLabels = {VIEWER: "閲覧者", OPERATOR: "操作者", ADMIN: "管理者"};

async function renderAccounts(signal) {
  const {accounts, current_id} = await accountCall("/accounts", "GET", undefined, signal);
  if (signal.aborted) return;
  const list = get("accounts-list");
  list.replaceChildren();
  for (const item of accounts) {
    const row = element("section", undefined, "detail-section");
    row.append(element("h3", item.username),
      element("p", `${accessLabels[item.access]} · ${item.active ? "有効" : "無効"}`));
    if (item.id !== current_id) {
      const toggle = element("button", item.active ? "無効化" : "有効化");
      toggle.addEventListener("click", async () => {
        if (item.active && !confirm(`${item.username} を無効化しますか？`)) return;
        await changeAccount(toggle, async () => {
          await accountCall(`/accounts/${item.id}`, "PATCH", {active: !item.active}, signal);
          await renderAccounts(signal);
          message("accounts-status", "アカウントの状態を変更しました。");
        });
      });
      row.append(toggle);
    }
    const reset = element("form", undefined, "account-form");
    const field = element("label", "新しいパスワード");
    const input = element("input");
    input.type = "password";
    input.autocomplete = "new-password";
    input.minLength = 12;
    input.maxLength = 256;
    input.required = true;
    field.append(input);
    reset.append(field, element("button", "パスワードを再設定"));
    reset.addEventListener("submit", (event) => {
      event.preventDefault();
      const password = input.value;
      input.value = "";
      void changeAccount(event.submitter, async () => {
        await accountCall(`/accounts/${item.id}/password`, "PUT", {password}, signal);
        if (item.id === current_id && account) {
          disconnect("パスワードを変更しました。新しいパスワードでログインしてください。");
          return;
        }
        message("accounts-status", "パスワードを再設定しました。");
      });
    });
    row.append(reset);
    list.append(row);
  }
  if (!accounts.length) list.append(element("p", "ログイン用アカウントはまだありません。"));
  accountForm.hidden = false;
}

async function changeAccount(control, action) {
  control.disabled = true;
  try {
    await action();
  } catch (error) {
    if (error.name !== "AbortError") message("accounts-status",
      error.code === "USERNAME_TAKEN" ? "そのユーザー名は使用済みです。" : "変更できませんでした。", true);
  } finally {
    control.disabled = false;
  }
}

get("accounts-open").addEventListener("click", async () => {
  accountRequest?.abort();
  accountRequest = new AbortController();
  accountForm.hidden = true;
  get("accounts-list").replaceChildren();
  message("accounts-status", "");
  accountsDialog.showModal();
  try {
    await renderAccounts(accountRequest.signal);
  } catch (error) {
    if (error.name !== "AbortError") message("accounts-status", "一覧を取得できませんでした。", true);
  }
});

accountForm.addEventListener("submit", (event) => {
  event.preventDefault();
  const body = Object.fromEntries(new FormData(accountForm));
  accountForm.elements.password.value = "";
  const {signal} = accountRequest;
  void changeAccount(event.submitter, async () => {
    await accountCall("/accounts", "POST", body, signal);
    accountForm.reset();
    await renderAccounts(signal);
    if (!signal.aborted) message("accounts-status", "アカウントを追加しました。");
  });
});

accountsDialog.addEventListener("close", () => {
  if (accountsDialog.open) return;
  accountRequest?.abort();
  accountForm.reset();
  get("accounts-list").replaceChildren();
});

const taskDialog = get("task-dialog");
const taskForm = get("task-form");
let taskRequest;
let taskProjects = [];
let taskSubmission;
let createdTask;

function taskKey() {
  return Array.from(crypto.getRandomValues(new Uint8Array(16)),
    (byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function postTask(path, body, key, signal) {
  const response = await fetch(path, {
    method: "POST", body, signal, redirect: "error",
    headers: {...authHeaders(), "Content-Type": "application/json",
      "Idempotency-Key": key},
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(error.code ?? `HTTP ${response.status}`);
  }
  return response.json();
}

function closeTaskDialog() {
  taskRequest?.abort();
  taskDialog.close();
}

function taskOptions(select, items) {
  select.replaceChildren(...items.map(({id, name}) => new Option(name, id)));
}

function selectedProject() {
  return taskProjects.find(({id}) => id === get("task-project").value);
}

function updateTaskRepositories() {
  const project = selectedProject();
  taskOptions(get("task-repository"), project?.repositories ?? []);
  get("task-write-scope").textContent = project?.allowed_write_paths?.length
    ? `変更を許可する範囲 · ${project.allowed_write_paths.join("、")}` : "";
}

get("task-project").addEventListener("change", updateTaskRepositories);
get("task-open").addEventListener("click", async () => {
  taskRequest?.abort();
  taskRequest = new AbortController();
  const {signal} = taskRequest;
  taskForm.reset();
  taskForm.querySelector("details").open = false;
  taskForm.hidden = true;
  taskSubmission = createdTask = undefined;
  get("task-run").hidden = true;
  taskDialog.showModal();
  message("task-status", "作成先を取得中…");
  try {
    const {projects} = await read("/v1/operations/task-targets", signal);
    if (signal.aborted) return;
    taskProjects = projects;
    if (!projects.length) {
      message("task-status", "有効なリポジトリがありません。管理者に登録を依頼してください。", true);
      return;
    }
    taskOptions(get("task-project"), projects);
    updateTaskRepositories();
    taskForm.hidden = false;
    message("task-status", "");
    get("task-objective").focus();
  } catch (error) {
    if (!signal.aborted) message("task-status", error.message, true);
  }
});

taskDialog.addEventListener("close", () => {
  if (!taskDialog.open) taskRequest?.abort();
});

function taskLines(id) {
  return get(id).value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
}

taskForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const submit = get("task-submit");
  if (submit.disabled || taskRequest.signal.aborted) return;
  const project = selectedProject();
  const repository = project?.repositories.find(({id}) => id === get("task-repository").value);
  const objective = get("task-objective").value.trim();
  const title = get("task-title").value.trim() || Array.from(objective.split(/\r?\n/)[0]).slice(0, 80).join("");
  const criteria = taskLines("task-criteria");
  const constraints = taskLines("task-constraints");
  if (!repository || !objective) {
    message("task-status", "作成先と目的を入力してください。", true);
    return;
  }
  if (criteria.length > 100 || criteria.some((line) => Array.from(line).length > 4000) ||
      constraints.length > 100 || constraints.some((line) => Array.from(line).length > 200)) {
    message("task-status", "受け入れ条件と制約はそれぞれ100件まで入力できます。1件の上限は受け入れ条件が4000文字、制約が200文字です。", true);
    return;
  }
  const body = JSON.stringify({
    task: {
      title, objective, repository_id: repository.id, base_ref: repository.base_ref, constraints,
      acceptance_criteria: criteria.map((description, index) => ({id: `AC-${index + 1}`, description})),
    },
    verification: project.verification,
  });
  const path = `/v1/projects/${encodeURIComponent(project.id)}/tasks`;
  if (taskSubmission?.body !== body || taskSubmission?.path !== path) {
    taskSubmission = {body, path, key: taskKey()};
  }
  const {signal} = taskRequest;
  const close = taskDialog.querySelector("header button");
  submit.disabled = close.disabled = true;
  message("task-status", "タスクを作成中…");
  try {
    const result = await postTask(path, body, taskSubmission.key, signal);
    if (signal.aborted) return;
    createdTask = {...result, key: taskKey()};
    taskForm.hidden = true;
    taskForm.reset();
    message("task-status", `「${title}」を作成しました。タスク ID ${result.task_id}。`);
    void loadBoard();
    get("task-run").hidden = false;
    get("task-run").focus();
  } catch (error) {
    if (!signal.aborted) message("task-status", `作成できませんでした（${error.message}）。`, true);
  } finally {
    submit.disabled = close.disabled = false;
  }
});

taskDialog.addEventListener("cancel", (event) => {
  if (get("task-submit").disabled || get("task-run").disabled) event.preventDefault();
});

get("task-run").addEventListener("click", async () => {
  const button = get("task-run");
  if (button.disabled || !createdTask || taskRequest.signal.aborted) return;
  const {task_id, spec_version, key} = createdTask;
  const {signal} = taskRequest;
  const close = taskDialog.querySelector("header button");
  button.disabled = close.disabled = true;
  message("task-status", "AIによる詳細化を開始中…");
  try {
    const run = await postTask(`/v1/tasks/${encodeURIComponent(task_id)}/runs`,
      JSON.stringify({spec_version, clarification_only: true, reuse_existing: true}), key, signal);
    if (signal.aborted) return;
    button.hidden = true;
    message("task-status", "AIによる仕様の詳細化を開始しました。");
    void loadBoard();
  } catch (error) {
    if (!signal.aborted) message("task-status", `実行を登録できませんでした（${error.message}）。`, true);
  } finally {
    button.disabled = close.disabled = false;
    if (button.hidden && !signal.aborted) close.focus();
  }
});

const settingsPath = "/v1/operations/integrations";
const settingsDialog = get("integration-settings");
let settingsRequest;
const providerFields = {
  jira: [
    ["site", "Jira サイト（https://…atlassian.net）"],
    ["project_key", "Jira プロジェクトキー"],
    ["field_id", "TaskSpec フィールド ID"],
    ["email", "Jira アカウントのメール", "email", "token"],
  ],
  slack: [
    ["channel", "送信先チャンネル ID"],
    ["team_id", "Slack Workspace ID", "text", "oauth"],
  ],
  github: [],
};

async function integrationCall(path, method, body, signal) {
  const response = await fetch(settingsPath + path, {
    method, signal, cache: "no-store", redirect: "error",
    headers: {...authHeaders(), "Content-Type": "application/json"},
    ...(body === undefined ? {} : {body: JSON.stringify(body)}),
  });
  if (!response.ok) {
    const {code} = await response.json();
    throw new Error(code ?? "INTEGRATION_REQUEST_FAILED");
  }
  return response.status === 204 ? null : response.json();
}

function integrationMessage(text, error = false) {
  message("integration-status", text, error);
}

function settingsAction(action) {
  return async (event) => {
    event.preventDefault();
    if (!settingsRequest || settingsRequest.signal.aborted) return;
    const control = event.submitter ?? event.currentTarget;
    const signal = settingsRequest.signal;
    control.disabled = true;
    try {
      await action(signal);
    } catch (error) {
      if (!signal.aborted) integrationMessage(`設定を処理できませんでした（${error.message}）`, true);
    } finally {
      control.disabled = false;
    }
  };
}

function settingsField(key, label, type = "text", initial = "") {
  const wrapper = element("label", label, "integration-field");
  const input = element("input");
  input.name = key;
  input.type = type;
  input.required = true;
  input.autocomplete = "off";
  input.spellcheck = false;
  input.value = initial;
  wrapper.append(input);
  return wrapper;
}

function settingsCard(provider, label, value, oauthAvailable, projects) {
  const card = element("section", undefined, "detail-section");
  card.append(element("h3", label), element("p", value.connected ? "設定済み" : "未接続"));
  const form = element("form");
  const mode = element("select");
  mode.name = "mode";
  for (const [key, title] of [["token", "トークン"], ["oauth", "OAuth"]]) {
    if (key === "oauth" && !oauthAvailable) continue;
    const option = element("option", title);
    option.value = key;
    mode.append(option);
  }
  mode.value = value.mode ?? "token";
  if (!mode.value) mode.value = "token";
  if (provider !== "github") {
    const label = element("label", "認証方式", "integration-field");
    label.append(mode);
    form.append(label);
  }
  if (provider === "jira") {
    const projectLabel = element("label", "Factory Project", "integration-field");
    const project = element("select");
    project.name = "project_id";
    project.required = true;
    const placeholder = element("option", "プロジェクトを選択");
    placeholder.value = "";
    project.append(placeholder);
    for (const {id, name} of projects) {
      const option = element("option", name);
      option.value = id;
      project.append(option);
    }
    project.value = value.project_id ?? "";
    projectLabel.append(project);
    form.append(projectLabel);
  }
  const fields = [...providerFields[provider], ["token", "トークン", "password", "token"]];
  if (provider !== "github") fields.push(
    ["client_id", "OAuth Client ID", "text", "oauth"],
    ["client_secret", "OAuth Client Secret", "password", "oauth"],
  );
  const entries = fields.map(([key, label, type, only]) => {
    const field = settingsField(key, label, type, value[key] ?? "");
    form.append(field);
    return [field, only];
  });
  function showFields() {
    for (const [field, only] of entries) {
      field.hidden = Boolean(only && only !== mode.value);
      field.querySelector("input").disabled = field.hidden;
    }
  }
  mode.addEventListener("change", showFields);
  showFields();
  const save = element("button", "保存");
  form.append(save);
  form.addEventListener("submit", settingsAction(async (signal) => {
    const body = Object.fromEntries(new FormData(form));
    for (const field of form.querySelectorAll('input[type="password"]')) field.value = "";
    await integrationCall(`/${provider}`, "PUT", body, signal);
    await refreshSettings(signal);
    if (!signal.aborted) integrationMessage("設定を保存しました。");
  }));
  card.append(form);
  if (provider !== "github" && oauthAvailable && value.mode === "oauth") {
    const start = element("button", "OAuth 認可リンクを発行");
    const link = element("a", "認可ページを開く（別タブ）");
    link.hidden = true;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    start.addEventListener("click", settingsAction(async (signal) => {
      const result = await integrationCall(`/${provider}/oauth/start`, "POST", undefined, signal);
      const url = new URL(result.url);
      const expected = provider === "jira" ? "https://auth.atlassian.com" : "https://slack.com";
      if (url.origin !== expected) throw new Error("OAUTH_ORIGIN_INVALID");
      link.href = url.href;
      link.hidden = false;
      integrationMessage("認可完了後、この画面で「状態を更新」を押してください。");
    }));
    card.append(start, link);
  }
  if (value.mode) {
    const remove = element("button", "設定を削除");
    remove.addEventListener("click", settingsAction(async (signal) => {
      if (!window.confirm(`${label} の保存済み設定を削除しますか？`)) return;
      await integrationCall(`/${provider}`, "DELETE", undefined, signal);
      await refreshSettings(signal);
      if (!signal.aborted) integrationMessage("設定を削除しました。");
    }));
    card.append(remove);
  }
  if (provider === "github") {
    for (const hook of value.webhooks) {
      const path = `/github/webhooks/${encodeURIComponent(hook.id)}`;
      const section = element("section", undefined, "detail-section");
      section.append(element("h4", `Webhook · ${hook.repository}`), element("p",
        hook.configured ? "Web設定済み" : hook.environment_configured ? "環境設定を使用" : "未設定"));
      section.append(element("code", `/v1/webhooks/github/${hook.id}`));
      const form = element("form");
      const field = settingsField("secret", "Webhook 署名シークレット", "password");
      field.querySelector("input").minLength = 32;
      form.append(field, element("button", "署名シークレットを保存"));
      form.addEventListener("submit", settingsAction(async (signal) => {
        const body = Object.fromEntries(new FormData(form));
        field.querySelector("input").value = "";
        await integrationCall(path, "PUT", body, signal);
        await refreshSettings(signal);
        if (!signal.aborted) integrationMessage("署名シークレットを保存しました。");
      }));
      section.append(form);
      if (hook.configured) {
        const remove = element("button", "Web設定を解除");
        remove.addEventListener("click", settingsAction(async (signal) => {
          if (!window.confirm("Webで保存した署名シークレットを解除しますか？")) return;
          await integrationCall(path, "DELETE", undefined, signal);
          await refreshSettings(signal);
          if (!signal.aborted) integrationMessage("署名シークレットを解除しました。");
        }));
        section.append(remove);
      }
      card.append(section);
    }
  }
  if (provider === "jira" && value.connected) {
    const importer = element("form");
    importer.append(settingsField("issue_key", "取り込む Jira 課題キー"), element("button", "取り込む"));
    importer.addEventListener("submit", settingsAction(async (signal) => {
      const result = await integrationCall("/jira/import", "POST", Object.fromEntries(new FormData(importer)), signal);
      if (!signal.aborted) integrationMessage(`Jira 取り込み結果 · ${result.state}${result.task_id ? " · " + result.task_id : ""}`);
    }));
    card.append(importer);
  }
  if (provider === "slack" && value.connected) {
    const deliver = element("button", "保留中の通知を1件配送");
    deliver.addEventListener("click", settingsAction(async (signal) => {
      const result = await integrationCall("/slack/deliver", "POST", undefined, signal);
      if (!signal.aborted) integrationMessage(result.state ? `配送結果 · ${result.state}` : "配送待ちの通知はありません。");
    }));
    card.append(deliver);
  }
  return card;
}

async function renderCodexSettings(section, signal) {
  const result = await integrationCall("/codex", "GET", undefined, signal);
  if (signal.aborted || !section.isConnected) return;
  if (result.login === "pending") {
    setTimeout(() => {
      if (!signal.aborted && section.isConnected) {
        void renderCodexSettings(section, signal).catch(() => {
          if (!signal.aborted) integrationMessage("Codex の認可状態を取得できませんでした。", true);
        });
      }
    }, 2000);
  }
  const snapshot = JSON.stringify(result);
  if (snapshot === section.dataset.snapshot) return;
  section.dataset.snapshot = snapshot;
  section.replaceChildren(element("h3", "Codex · ChatGPT サブスクリプション"));
  if (!result.available) {
    section.append(element("p", "この Workspace では実行ホストの Codex 接続設定が必要です。"));
    return;
  }
  section.append(element("p", result.connected
    ? (result.mode === "device" ? "ChatGPT の認可を使用中" : "実行ホストの既存の ChatGPT ログインを使用中")
    : "ChatGPT に未接続"));
  if (result.login === "pending") {
    if (result.user_code) {
      const auth = element("a", "ChatGPT で認可する（別タブ）");
      auth.href = "https://auth.openai.com/codex/device";
      auth.target = "_blank";
      auth.rel = "noopener noreferrer";
      const linkRow = element("p");
      linkRow.append(auth);
      section.append(element("p", "ChatGPT の画面に次のコードを入力してください。"),
        element("code", result.user_code), linkRow);
    } else {
      section.append(element("p", "認可コードを取得中…"));
    }
    return;
  }
  if (result.login === "failed") {
    section.append(element("p", "認可を完了できませんでした。ChatGPT のセキュリティ設定でデバイスコード認証を確認してください。", "error"));
  }
  for (const [action, label] of [["existing", "既存のログインを使う"], ["login", "ChatGPT で接続する"]]) {
    const button = element("button", label);
    button.addEventListener("click", settingsAction(async (activeSignal) => {
      await integrationCall(`/codex/${action}`, "POST", undefined, activeSignal);
      await renderCodexSettings(section, activeSignal);
    }));
    section.append(button);
  }
}

async function refreshSettings(signal) {
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
  if (window.location.protocol !== "https:" && !loopback) {
    integrationMessage("連携の認証情報を設定するには HTTPS で開いてください。", true);
    return;
  }
  const status = await integrationCall("", "GET", undefined, signal);
  if (signal.aborted) return;
  const container = get("integration-providers");
  const codex = element("section", undefined, "detail-section");
  container.replaceChildren(codex);
  await renderCodexSettings(codex, signal);
  if (signal.aborted) return;
  if (!status.available) {
    container.append(element("p", "Jira・Slack・GitHubの設定にはサーバーの暗号化キーが必要です。"));
    return;
  }
  const reload = element("button", "状態を更新");
  reload.addEventListener("click", settingsAction(async (signal) => {
    await refreshSettings(signal);
    if (!signal.aborted) integrationMessage("状態を更新しました。");
  }));
  container.append(reload);
  for (const [provider, label] of [["jira", "Jira Cloud"], ["slack", "Slack Bot"], ["github", "GitHub CI 読み取り"]]) {
    container.append(settingsCard(provider, label, status.providers[provider], status.oauth_available, status.projects));
  }
}

get("integration-open").addEventListener("click", async () => {
  settingsRequest = new AbortController();
  const {signal} = settingsRequest;
  settingsDialog.showModal();
  integrationMessage("設定を取得中…");
  try {
    integrationMessage("");
    await refreshSettings(signal);
  } catch (error) {
    if (!signal.aborted) integrationMessage(`設定を取得できませんでした（${error.message}）`, true);
  }
});
settingsDialog.addEventListener("close", () => {
  if (settingsDialog.open) return;
  settingsRequest?.abort();
  get("integration-providers").replaceChildren();
});
const oauthResult = new URLSearchParams(location.search).get("oauth");
if (oauthResult === "connected" || oauthResult === "failed") {
  message("status", oauthResult === "connected"
    ? "OAuth 認可が完了しました。このタブを閉じ、元の設定画面で状態を更新してください。"
    : "OAuth 認可を完了できませんでした。元の設定画面からやり直してください。", oauthResult === "failed");
  history.replaceState(null, "", location.pathname);
}

void restoreAccount();
