import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("../../src/software_factory/operations_ui/task_deletion.js", import.meta.url), "utf8");
const {installTaskDeletion} = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

// A small DOM stand-in exercises the real controller without launching a browser.
class Node extends EventTarget {
  hidden = false;
  disabled = false;
  open = false;
  textContent = "";
  classes = new Set();
  classList = {toggle: (name, on) => on ? this.classes.add(name) : this.classes.delete(name)};
  showModal() { this.open = true; }
  close() {
    if (!this.open) return;
    this.open = false;
    this.dispatchEvent(new Event("close"));
  }
  click() { if (!this.disabled) this.dispatchEvent(new Event("click")); }
  escape() {
    if (this.dispatchEvent(new Event("cancel", {cancelable: true}))) this.close();
  }
}

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return {promise, resolve};
}
const settle = () => new Promise((done) => setImmediate(done));
const ok = {ok: true};

function setup(access = "OPERATOR") {
  const nodes = new Map();
  const get = (id) => {
    if (!nodes.has(id)) nodes.set(id, new Node());
    return nodes.get(id);
  };
  const state = {access, selected: {task_id: "task/1", task_title: "削除確認 <script>"}};
  const requests = [], removed = [];
  let respond = async () => ok;
  const controller = installTaskDeletion({
    get, currentTask: () => state.selected,
    canDelete: () => ["OPERATOR", "ADMIN"].includes(state.access),
    request: (path, options) => {
      requests.push({path, options});
      return respond();
    },
    onDeleted: (id) => removed.push(id),
  });
  return {get, state, controller, requests, removed, respond: (fn) => { respond = fn; }};
}

test("operator/admin have the action; viewer and closed detail cannot open confirmation", () => {
  for (const role of ["OPERATOR", "ADMIN", "VIEWER", undefined]) {
    const ui = setup(role);
    if (role === undefined) { ui.state.access = undefined; ui.controller.update(); }
    const permitted = ["OPERATOR", "ADMIN"].includes(role);
    assert.equal(ui.get("delete-task").hidden, !permitted);
    ui.get("delete-task").click();
    assert.equal(ui.get("delete-task-dialog").open, permitted);
    assert.equal(ui.requests.length, 0);
    ui.controller.reset();
    ui.state.selected = null;
    ui.controller.update();
    assert.equal(ui.get("delete-task").hidden, true);
    ui.get("delete-task").click();
    assert.equal(ui.get("delete-task-dialog").open, false);
  }
});

test("target uses text, and cancel/Escape do not mutate or send a deletion", async () => {
  const ui = setup();
  for (const cancel of [() => ui.get("delete-task-cancel").click(), () => ui.get("delete-task-dialog").escape()]) {
    ui.get("delete-task").click();
    assert.equal(ui.get("delete-task-description").textContent, "「削除確認 <script>」を削除しますか？");
    cancel();
    ui.get("delete-task-confirm").click();
    await settle();
    assert.equal(ui.get("delete-task-dialog").open, false);
    assert.equal(ui.requests.length, 0);
    assert.deepEqual(ui.removed, []);
  }
});

test("confirmation sends once, freezes target and removes only on confirmed success", async () => {
  const ui = setup(), response = deferred();
  ui.respond(() => response.promise);
  ui.get("delete-task").click();
  ui.state.selected = {task_id: "different", task_title: "別のタスク"};
  ui.get("delete-task-confirm").click();
  ui.get("delete-task-confirm").click();
  ui.get("delete-task-dialog").escape();
  assert.equal(ui.get("delete-task-dialog").open, true);
  assert.equal(ui.requests.length, 1);
  assert.deepEqual(ui.removed, []);
  const {path, options} = ui.requests[0];
  assert.equal(path, "/v1/tasks/task%2F1");
  assert.equal(options.method, "DELETE");
  assert.equal(options.cache, "no-store");
  assert.equal(options.redirect, "error");
  response.resolve(ok);
  await settle();
  assert.deepEqual(ui.removed, ["task/1"]);
  assert.equal(ui.get("delete-task-dialog").open, false);
  assert.equal(ui.get("delete-task-confirm").disabled, false);
});

test("active Run/chat and permission errors retain task with an actionable reason", async () => {
  for (const [code, reason] of [
    ["TASK_DELETE_ACTIVE_RUN", "実行を取り消して"],
    ["TASK_DELETE_ACTIVE_CHAT", "処理の終了を待って"],
    ["USER_ACCESS_DENIED", "権限が必要"],
    ["NOT_FOUND", "一覧を更新"],
  ]) {
    const ui = setup();
    ui.respond(async () => ({ok: false, json: async () => ({code})}));
    ui.get("delete-task").click();
    ui.get("delete-task-confirm").click();
    await settle();
    assert.match(ui.get("delete-task-status").textContent, new RegExp(reason));
    assert.deepEqual(ui.removed, []);
    assert.equal(ui.get("delete-task-dialog").open, true);
    assert.equal(ui.get("delete-task-confirm").disabled, false);
    ui.respond(async () => ok);
    ui.get("delete-task-confirm").click();
    await settle();
    assert.deepEqual(ui.removed, ["task/1"]);
  }
});

test("unknown delivery result stays retryable and never pretends success", async () => {
  const ui = setup();
  ui.respond(async () => { throw new Error("network unavailable"); });
  ui.get("delete-task").click();
  ui.get("delete-task-confirm").click();
  await settle();
  assert.match(ui.get("delete-task-status").textContent, /削除結果を確認できません/);
  assert.deepEqual(ui.removed, []);
  assert.equal(ui.get("delete-task-confirm").disabled, false);
});

test("disconnect aborts pending request and ignores its late success in the next session", async () => {
  const ui = setup(), response = deferred();
  ui.respond(() => response.promise);
  ui.get("delete-task").click();
  ui.get("delete-task-confirm").click();
  ui.state.access = undefined;
  ui.controller.reset();
  assert.equal(ui.requests[0].options.signal.aborted, true);
  response.resolve(ok);
  await settle();
  assert.deepEqual(ui.removed, []);
  assert.equal(ui.get("delete-task-dialog").open, false);
  assert.equal(ui.get("delete-task").hidden, true);
});
