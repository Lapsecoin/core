// Receives a crash report from LapseCoin and files it as a GitHub issue, once per
// kind of crash (the "sig" in the report): the first report opens an issue, later
// ones add a short comment to it. It runs as a Cloudflare Worker so that the GitHub
// token lives here and never in the program, where anyone could read it.
//
// It stores nothing about the sender. To slow down abuse it counts requests per
// day in a KV namespace, keyed by a hash of the sender's address, and the keys
// expire after two days.

const MAX_BODY = 8000;
const PER_IP_PER_DAY = 5;
const PER_DAY = 200;
const TEXT_FIELDS = { kind: 20, where: 40, sig: 10, app: 20, windows: 40, arch: 12, python: 20 };

export function validate(p) {
  if (!p || typeof p !== "object" || p.v !== 1) return "not a v1 report";
  if (typeof p.sig !== "string" || !/^[0-9a-z]{4,12}$/.test(p.sig)) return "bad signature";
  if (typeof p.trace !== "string" || !p.trace.trim()) return "no trace";
  return null;
}

// Text from the program is shown inside code fences, so it is kept to plain
// characters, any folder under C:\Users is taken out again, and nothing can
// close the fence early.
export function clean(text, max) {
  return String(text ?? "")
    .replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g, "")
    .replace(/[A-Za-z]:\\Users\\[^\\\s]+/g, "%USERPROFILE%")
    .replace(/```/g, "'''")
    .slice(0, max);
}

export function issueTitle(p) {
  const last = clean(p.trace, 3500).trim().split("\n").filter(Boolean).pop() || "crash";
  return `Crash ${clean(p.sig, 10)}: ${last}`.replace(/\s+/g, " ").slice(0, 120);
}

function facts(p) {
  const f = (k) => clean(p[k], TEXT_FIELDS[k]);
  return `LapseCoin ${f("app")}, ${f("kind")}, ${f("where")}. Windows ${f("windows")} ${f("arch")}, ` +
    `Python ${f("python")}, packaged: ${p.packaged === true ? "yes" : "no"}.`;
}

export function issueBody(p) {
  return [
    "Reported automatically by LapseCoin.",
    "",
    facts(p),
    "",
    "Traceback:",
    "```",
    clean(p.trace, 3500),
    "```",
    "",
    "Last start-up steps:",
    "```",
    clean(p.steps, 1200),
    "```",
  ].join("\n");
}

export function commentBody(p) {
  return `Again: ${facts(p)}`;
}

const reply = (status, text = "") => new Response(text, { status });

async function sha(text) {
  const bytes = new TextEncoder().encode(text);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return [...new Uint8Array(digest)].slice(0, 8).map((b) => b.toString(16).padStart(2, "0")).join("");
}

async function withinLimits(request, env) {
  if (!env.LIMITS) return true;
  const day = new Date().toISOString().slice(0, 10);
  const ip = await sha(request.headers.get("CF-Connecting-IP") || "unknown");
  const keys = [`ip:${day}:${ip}`, `all:${day}`];
  const counts = await Promise.all(keys.map(async (k) => Number((await env.LIMITS.get(k)) || 0)));
  if (counts[0] >= PER_IP_PER_DAY || counts[1] >= PER_DAY) return false;
  await Promise.all(keys.map((k, i) => env.LIMITS.put(k, String(counts[i] + 1), { expirationTtl: 172800 })));
  return true;
}

export async function handle(request, env, fetchFn = fetch) {
  if (request.method !== "POST") return reply(405);
  const raw = await request.text();
  if (raw.length > MAX_BODY) return reply(413);
  let report;
  try {
    report = JSON.parse(raw);
  } catch {
    return reply(400, "not json");
  }
  const bad = validate(report);
  if (bad) return reply(400, bad);
  if (!(await withinLimits(request, env))) return reply(429);

  const gh = (path, init = {}) =>
    fetchFn(`https://api.github.com${path}`, {
      ...init,
      headers: {
        authorization: `Bearer ${env.GITHUB_TOKEN}`,
        accept: "application/vnd.github+json",
        "content-type": "application/json",
        "user-agent": "lapsecoin-crash-relay",
      },
    });

  const query = `repo:${env.REPO} is:issue label:crash-report in:title "Crash ${clean(report.sig, 10)}"`;
  const found = await gh(`/search/issues?q=${encodeURIComponent(query)}&per_page=1`);
  if (!found.ok) return reply(502, "search failed");
  const existing = ((await found.json()).items || [])[0];

  const done = existing
    ? await gh(`/repos/${env.REPO}/issues/${existing.number}/comments`, {
        method: "POST", body: JSON.stringify({ body: commentBody(report) }) })
    : await gh(`/repos/${env.REPO}/issues`, {
        method: "POST",
        body: JSON.stringify({ title: issueTitle(report), body: issueBody(report), labels: ["crash-report"] }) });
  return done.ok ? reply(201) : reply(502, "github refused");
}

export default { fetch: (request, env) => handle(request, env) };
