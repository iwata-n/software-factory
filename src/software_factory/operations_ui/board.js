const get = (id) => document.getElementById(id);
const lanes = new Map([
  ["QUEUED", "実行待ち"],
  ["RUNNING", "実行中"],
  ["WAITING_EXTERNAL", "外部待ち"],
  ["BLOCKED", "要対応"],
  ["CANCELLING", "取消処理中"],
  ["SUCCEEDED", "成功"],
  ["FAILED", "失敗"],
  ["CANCELLED", "取消済み"],
]);
const detail = get("detail");
const toolbar = get("toolbar");
const menuToggle = toolbar.querySelector("summary");
const executionButtons = ["execution-action", "execution-submit", "execution-dismiss"];
let token = "";
let account;
const secureAccounts = location.protocol === "https:" ||
  ["127.0.0.1", "localhost", "[::1]"].includes(location.hostname);

function authHeaders() {
  return token ? {Authorization: `Bearer ${token}`} : {};
}
let runs = [];
let nextBefore = null;
let boardRequest;
let detailRequest;
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
  executionMessage("");
  token = "";
  account = undefined;
  accountsDialog.close();
  get("account-name").hidden = true;
  get("accounts-open").hidden = true;
  runs = [];
  nextBefore = null;
  selected = null;
  detail.close();
  get("detail-content").replaceChildren();
  get("board").replaceChildren();
  toolbar.open = false;
  for (const id of ["board", "toolbar", "older"]) get(id).hidden = true;
  get("token-login").hidden = false;
  get("login").hidden = !secureAccounts;
  get(secureAccounts ? "login-username" : "token").focus();
  message("status", text, error);
}

function renderBoard() {
  const grouped = new Map([...lanes.keys()].map((status) => [status, []]));
  for (const run of runs) {
    if (!grouped.has(run.status)) grouped.set(run.status, []);
    grouped.get(run.status).push(run);
  }
  const sections = [];
  for (const [status, items] of grouped) {
    const section = element("section", undefined, "lane");
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
        card.append(
          element("span", run.task_title),
          element("small", `${run.status} / ${run.phase}`),
          element("small", `Rev. ${run.state_revision} · ${run.run_id.slice(0, 8)}`),
        );
        if (run.stop_reason) card.append(element("small", run.stop_reason, "reason"));
        card.addEventListener("click", () => {
          selected = run;
          detail.showModal();
          loadDetail();
        });
        item.append(card);
        list.append(item);
      }
      section.append(list);
    }
    sections.push(section);
  }
  get("board").replaceChildren(...sections);
}

function executionMessage(text, error = false) {
  message("execution-message", text, error);
  get("execution-message").hidden = !text;
}

function closeExecutionForm() {
  pendingExecution = undefined;
  get("execution-form").hidden = true;
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
  get("execution-state").textContent = execution.stopped
    ? `緊急停止中 · Rev. ${execution.revision}`
    : `実行を許可 · Rev. ${execution.revision}`;
  get("execution-action").hidden = access !== "ADMIN";
  get("task-open").hidden = !["ADMIN", "OPERATOR"].includes(access);
  get("integration-open").hidden = access !== "ADMIN";
  get("accounts-open").hidden = access !== "ADMIN" || !secureAccounts;
  get("disconnect").textContent = account ? "ログアウト" : "切断";
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
  get("worker-panel").hidden = false;
  get("worker-list").replaceChildren();
  message("worker-status", "Worker の状態を取得中…");
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
    message("worker-status", workers.length ? "" : "登録済みの Worker はありません。");
  } catch {
    if (!signal.aborted) message("worker-status", "Worker の状態を取得できませんでした。", true);
  }
}

async function loadBoard(older = false) {
  boardRequest?.abort();
  boardRequest = new AbortController();
  const { signal } = boardRequest;
  get("refresh").disabled = get("older").disabled = true;
  get("connect").querySelector("button").disabled = true;
  message("status", "Run 一覧を取得中…");
  try {
    const suffix = older ? `?before=${encodeURIComponent(nextBefore)}` : "";
    const [page, context] = await Promise.all([
      read(`/v1/runs${suffix}`, signal), read("/v1/operations/context", signal),
    ]);
    if (signal.aborted) return;
    renderExecution(context);
    void loadWorkers(context.execution.workspace_id, signal);
    runs = older ? [...runs, ...page.runs] : page.runs;
    nextBefore = page.next_before;
    renderBoard();
    get("token-login").hidden = get("login").hidden = true;
    get("board").hidden = get("toolbar").hidden = false;
    get("older").hidden = !nextBefore;
    message("status", runs.length ? `${runs.length} 件を表示中` : "この Workspace に Run はありません。");
  } catch (error) {
    if (signal.aborted) return;
    if ([401, 403].includes(error.status)) {
      disconnect(account ? "ログインし直してください。" : "User トークンを確認してください。", true);
    } else {
      message("status", "一覧を更新できませんでした。再試行してください。", true);
    }
  } finally {
    if (boardRequest.signal === signal) {
      get("refresh").disabled = get("older").disabled = false;
      get("connect").querySelector("button").disabled = false;
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
    const list = element("div");
    list.id = "run-event-list";
    list.append(...[...events].reverse().map(eventRecord));
    return section("履歴", list);
  }],
];

function eventRecord(event) {
  const record = element("div", undefined, "record");
  record.append(fields([
    ["Revision", event.revision], ["イベント", event.kind], ["時刻", date(event.created_at)],
  ]));
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

async function loadDetail() {
  detailRequest?.abort();
  detailRequest = new AbortController();
  const { signal } = detailRequest;
  const root = `/v1/runs/${encodeURIComponent(selected.run_id)}`;
  get("detail-title").textContent = selected.task_title;
  get("detail-content").replaceChildren();
  get("reload-detail").disabled = true;
  message("detail-status", "Run 詳細を取得中…");

  try {
    const [runResult, ...results] = await Promise.allSettled([
      read(root, signal),
      ...detailSections.map(([path]) => read(`${root}/${path}`, signal)),
    ]);
    if (signal.aborted) return;
    if (runResult.status === "rejected") throw runResult.reason;
    const run = runResult.value;
    const candidateId = run.current_candidate_id;
    const candidatePath = `/v1/candidates/${encodeURIComponent(candidateId)}`;
    const [candidateResults, verifiedResults] = await Promise.all([
      candidateId ? Promise.allSettled([
        read(candidatePath, signal), read(`${candidatePath}/verification`, signal),
      ]) : [],
      run.status === "SUCCEEDED" ? Promise.allSettled([
        read(`${root}/result`, signal), read(`${root}/result/current-pr`, signal),
      ]) : [],
    ]);
    if (signal.aborted) return;
    const allResults = [...results, ...candidateResults, ...verifiedResults];
    const denied = allResults.find(({ reason }) => [401, 403].includes(reason?.status));
    if (denied) throw denied.reason;
    const context = run.wait_context;
    const content = [
      section("Run", fields([
        ["Run ID", run.run_id], ["状態", run.status], ["段階", run.phase],
        ["Revision", run.state_revision], ["停止理由", run.stop_reason],
        ...(run.evidence_expired_at == null ? [] : [["証拠の保持期限", "回収済み"]]),
        ["待機種別", context?.kind], ["要対応理由", context?.cause],
      ])),
      section("費用 (µUSD)", fields([
        ["消費", run.spent_microusd], ["予約", run.reserved_microusd],
        ["予算", run.budget_microusd], ["費用状態", run.cost_state],
      ])),
      section("現候補", candidateId ? fields([
        ["Candidate ID", candidateId], ["世代", candidateResults[0].value?.generation],
        ["Head", candidateResults[0].value?.head_sha],
      ]) : element("p", "現候補なし")),
      ...(run.status === "SUCCEEDED" ? [] : [ciView(context)]),
    ];
    if (verifiedResults[0]?.status === "fulfilled") {
      content.push(verifiedResultView(verifiedResults[0].value, verifiedResults[1].value));
    }
    if (candidateId) content.push(candidateResults[1].status === "fulfilled"
      ? verificationView(candidateResults[1].value)
      : section("Check・Gate", element("p", "取得できませんでした。", "error")));
    results.forEach((result, index) => {
      const [, title, view] = detailSections[index];
      content.push(result.status === "fulfilled" ? view(result.value)
        : section(title, element("p", "取得できませんでした。", "error")));
    });
    get("detail-content").replaceChildren(...content);
    const incomplete = allResults.some(({ status }) => status === "rejected");
    message("detail-status", incomplete ? "一部の詳細を取得できませんでした。" : "", incomplete);
    const history = results[detailSections.length - 1].value;
    if (history) followRunEvents(root, run.run_id, history.events.at(-1)?.revision, signal);
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

get("connect").addEventListener("submit", (event) => {
  event.preventDefault();
  token = get("token").value.trim();
  get("token").value = "";
  if (token) loadBoard();
});
toolbar.addEventListener("click", (event) => {
  if (event.target.closest("button")) {
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
get("refresh").addEventListener("click", () => loadBoard());
get("older").addEventListener("click", () => loadBoard(true));
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
get("reload-detail").addEventListener("click", loadDetail);
detail.addEventListener("close", () => {
  if (detail.open) return;
  detailRequest?.abort();
  get("detail-content").replaceChildren();
});

get("execution-action").addEventListener("click", openExecutionForm);
get("execution-dismiss").addEventListener("click", closeExecutionForm);
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
    get("task-title").focus();
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
  const title = get("task-title").value.trim();
  const objective = get("task-objective").value.trim();
  const criteria = taskLines("task-criteria");
  const constraints = taskLines("task-constraints");
  if (!repository || !title || !objective || !criteria.length) {
    message("task-status", "作成先、タイトル、目的、受け入れ条件を入力してください。", true);
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
  message("task-status", "実行を登録中…");
  try {
    const run = await postTask(`/v1/tasks/${encodeURIComponent(task_id)}/runs`,
      JSON.stringify({spec_version}), key, signal);
    if (signal.aborted) return;
    button.hidden = true;
    message("task-status", `実行を登録しました。Run ID ${run.run_id}。`);
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

async function refreshSettings(signal) {
  const loopback = ["localhost", "127.0.0.1", "[::1]"].includes(window.location.hostname);
  if (window.location.protocol !== "https:" && !loopback) {
    integrationMessage("連携の認証情報を設定するには HTTPS で開いてください。", true);
    return;
  }
  const status = await integrationCall("", "GET", undefined, signal);
  if (signal.aborted) return;
  const container = get("integration-providers");
  container.replaceChildren();
  if (!status.available) {
    integrationMessage("連携設定にはサーバーの暗号化キー設定が必要です。", true);
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
