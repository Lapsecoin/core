import test from "node:test";
import assert from "node:assert/strict";
import { handle, clean, issueTitle, issueBody, validate } from "./worker.js";

const report = (over = {}) => ({
  v: 1, kind: "fatal", where: "start-up", sig: "abc1234567", app: "1.1.6",
  windows: "10.0.26200", arch: "AMD64", python: "3.11.9", packaged: true,
  trace: "Traceback (most recent call last):\n  File \"main.py\", line 12\nPermissionError: cannot open lapsecoin.log",
  steps: "2026-10-20 03:00:00 start", ...over,
});
const post = (body) => new Request("https://relay.test/", { method: "POST", body: JSON.stringify(body) });

// A GitHub that records what it is asked, with a search result chosen by the test.
function fakeGithub(searchItems = []) {
  const calls = [];
  const fetchFn = async (url, init = {}) => {
    calls.push({ url, method: init.method || "GET", body: init.body ? JSON.parse(init.body) : null });
    if (url.includes("/search/issues")) return new Response(JSON.stringify({ items: searchItems }), { status: 200 });
    return new Response("{}", { status: 201 });
  };
  return { calls, fetchFn };
}
const env = { GITHUB_TOKEN: "t", REPO: "Lapsecoin/core" };

test("a first report opens an issue labelled crash-report", async () => {
  const gh = fakeGithub();
  const res = await handle(post(report()), env, gh.fetchFn);
  assert.equal(res.status, 201);
  const made = gh.calls.find((c) => c.method === "POST");
  assert.match(made.url, /\/repos\/Lapsecoin\/core\/issues$/);
  assert.deepEqual(made.body.labels, ["crash-report"]);
  assert.match(made.body.title, /^Crash abc1234567: PermissionError/);
  assert.match(made.body.body, /packaged: yes/);
});

test("a repeat of the same crash adds a comment instead of a second issue", async () => {
  const gh = fakeGithub([{ number: 42 }]);
  const res = await handle(post(report()), env, gh.fetchFn);
  assert.equal(res.status, 201);
  const made = gh.calls.find((c) => c.method === "POST");
  assert.match(made.url, /\/issues\/42\/comments$/);
  assert.match(made.body.body, /^Again: LapseCoin 1.1.6/);
});

test("only POST, small, valid reports are taken", async () => {
  const gh = fakeGithub();
  assert.equal((await handle(new Request("https://relay.test/"), env, gh.fetchFn)).status, 405);
  assert.equal((await handle(new Request("https://relay.test/", { method: "POST", body: "x".repeat(9000) }), env, gh.fetchFn)).status, 413);
  assert.equal((await handle(new Request("https://relay.test/", { method: "POST", body: "nope" }), env, gh.fetchFn)).status, 400);
  assert.equal((await handle(post(report({ v: 2 })), env, gh.fetchFn)).status, 400);
  assert.equal((await handle(post(report({ sig: "../../x" })), env, gh.fetchFn)).status, 400);
  assert.equal(gh.calls.length, 0);
});

test("one sender is limited per day, and the keys hold a hash, not the address", async () => {
  const store = new Map();
  const LIMITS = { get: async (k) => store.get(k), put: async (k, v) => store.set(k, v) };
  const gh = fakeGithub();
  const req = () => new Request("https://relay.test/", {
    method: "POST", body: JSON.stringify(report()), headers: { "CF-Connecting-IP": "203.0.113.9" } });
  const statuses = [];
  for (let i = 0; i < 7; i++) statuses.push((await handle(req(), { ...env, LIMITS }, gh.fetchFn)).status);
  assert.deepEqual(statuses, [201, 201, 201, 201, 201, 429, 429]);
  assert.ok([...store.keys()].every((k) => !k.includes("203.0.113.9")));
});

test("text cannot close the code fence or carry a folder name", () => {
  assert.equal(clean("a```b", 50), "a'''b");
  assert.equal(clean("open C:\\Users\\Vic\\x", 50), "open %USERPROFILE%\\x");
  const body = issueBody(report({ trace: "boom ``` ## injected", steps: "C:\\Users\\Vic\\LapseCoin" }));
  assert.ok(!body.includes("```\n## injected"));
  assert.ok(!body.includes("Vic"));
  assert.ok(issueTitle(report({ trace: "line\n" + "x".repeat(500) })).length <= 120);
  assert.equal(validate(report({ trace: "  " })), "no trace");
});

test("a GitHub failure is reported as a failure, not as success", async () => {
  const fetchFn = async (url) => (url.includes("/search/") ? new Response("{}", { status: 500 }) : new Response("{}", { status: 201 }));
  assert.equal((await handle(post(report()), env, fetchFn)).status, 502);
});
