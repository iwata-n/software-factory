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
let token = "";
let runs = [];
let nextBefore = null;
let boardRequest;
let detailRequest;
let selected;

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

async function loadBoard(older = false) {
  boardRequest?.abort();
  boardRequest = new AbortController();
  const { signal } = boardRequest;
  get("refresh").disabled = get("older").disabled = true;
  get("connect").querySelector("button").disabled = true;
  message("status", "Run 一覧を取得中…");
  try {
    const suffix = older ? `?before=${encodeURIComponent(nextBefore)}` : "";
    const page = await read(`/v1/runs${suffix}`, signal);
    if (signal.aborted) return;
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
    const history = records("履歴", [...events].reverse(), (event) => [
      ["Revision", event.revision], ["イベント", event.kind], ["時刻", date(event.created_at)],
    ]);
    if (events.length === 1000) history.append(element("p", "履歴は先頭の 1,000 件を表示しています。"));
    return history;
  }],
];

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
    const candidateResults = candidateId ? await Promise.allSettled([
      read(candidatePath, signal), read(`${candidatePath}/verification`, signal),
    ]) : [];
    if (signal.aborted) return;
    const allResults = [...results, ...candidateResults];
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
      ciView(context),
    ];
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
