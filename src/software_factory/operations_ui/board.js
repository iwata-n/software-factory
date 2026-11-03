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
const executionButtons = ["execution-action", "execution-submit", "execution-dismiss"];
let token = "";
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
    headers: { Authorization: `Bearer ${token}` },
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
  get("execution").hidden = true;
  executionMessage("");
  token = "";
  runs = [];
  nextBefore = null;
  selected = null;
  detail.close();
  get("detail-content").replaceChildren();
  get("board").replaceChildren();
  for (const id of ["board", "toolbar", "older"]) get(id).hidden = true;
  get("connect").hidden = false;
  get("token").focus();
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
        Authorization: `Bearer ${token}`, "Content-Type": "application/json", "Idempotency-Key": key,
      },
      body: JSON.stringify({ stopped, expected_revision, reason }),
      cache: "no-store", signal, redirect: "error",
    });
    if (signal.aborted) return;
    if (response.status === 401) {
      disconnect("User トークンを確認してください。", true);
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
    runs = older ? [...runs, ...page.runs] : page.runs;
    nextBefore = page.next_before;
    renderBoard();
    get("connect").hidden = true;
    get("board").hidden = get("toolbar").hidden = false;
    get("older").hidden = !nextBefore;
    message("status", runs.length ? `${runs.length} 件を表示中` : "この Workspace に Run はありません。");
  } catch (error) {
    if (signal.aborted) return;
    if ([401, 403].includes(error.status)) {
      disconnect("User トークンを確認してください。", true);
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
    node.append(fields([["Job ID", job.job_id], ["Step", job.step_key]]));
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

const detailSections = [
  ["graph", "ジョブ・試行・修正", graphView],
  ["publication", "PR・公開処理", publicationView],
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
      const headers = { Authorization: `Bearer ${token}`, Accept: "text/event-stream" };
      if (revision !== undefined) headers["Last-Event-ID"] = `${runId}:${revision}`;
      const response = await fetch(`${root}/events`, {
        headers, cache: "no-store", signal, redirect: "error",
      });
      if (response.status === 410) {
        if (!signal.aborted) loadDetail();
        return;
      }
      if ([401, 403].includes(response.status)) {
        if (!signal.aborted) disconnect("User トークンを確認してください。", true);
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
      disconnect("User トークンを確認してください。", true);
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
get("refresh").addEventListener("click", () => loadBoard());
get("older").addEventListener("click", () => loadBoard(true));
get("disconnect").addEventListener("click", () => disconnect());
get("reload-detail").addEventListener("click", loadDetail);
detail.addEventListener("close", () => {
  if (detail.open) return;
  detailRequest?.abort();
  get("detail-content").replaceChildren();
});

get("execution-action").addEventListener("click", openExecutionForm);
get("execution-dismiss").addEventListener("click", closeExecutionForm);
get("execution-form").addEventListener("submit", submitExecution);
