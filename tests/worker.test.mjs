// Тести воркера: node --test tests/  (Node ≥ 20, без залежностей)
import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import worker from "../worker/worker.js";

class KV {
  constructor() { this.m = new Map(); }
  async get(k, type) { const v = this.m.get(k); if (!v) return null; return type === "json" ? JSON.parse(v.value) : v.value; }
  async put(k, value, opts = {}) { this.m.set(k, { value, metadata: opts.metadata, ttl: opts.expirationTtl }); }
  async delete(k) { this.m.delete(k); }
  async getWithMetadata(k) { const v = this.m.get(k); return { value: v?.value ?? null, metadata: v?.metadata ?? null }; }
  async list({ prefix }) { return { keys: [...this.m].filter(([k]) => k.startsWith(prefix)).map(([name, v]) => ({ name, metadata: v.metadata })), list_complete: true }; }
}

const TOKEN = "123:TEST";
const SECRET = createHash("sha256").update("pfk-rozklad:" + TOKEN).digest("hex").slice(0, 48);
const SITE = "https://site.test/";
let calls = [];
const INDEX = { hash: "abc123", courses: { 1: [{ name: "СОБ-11", slug: "sob-11", room: "" }] } };
const GROUP = { group: "СОБ-11", course: 1, slug: "sob-11", days: {}, changes: [] };
globalThis.fetch = async (url, opts = {}) => {
  url = String(url);
  calls.push({ url, body: opts.body ? JSON.parse(opts.body) : null });
  if (url.startsWith(SITE + "data/index.json")) return Response.json(INDEX);
  if (url.startsWith(SITE + "data/g/")) return Response.json(GROUP);
  if (url.includes("api.github.com")) return new Response(null, { status: 204 });
  return Response.json({ ok: true });
};
const mkEnv = (extra = {}) => ({ BOT_TOKEN: TOKEN, SITE_URL: SITE, USERS: new KV(), ...extra });
async function api(env, path, body) {
  const r = await worker.fetch(new Request("https://w.test" + path, { method: "POST", headers: { Authorization: "Bearer " + SECRET }, body: JSON.stringify(body ?? {}) }), env, { waitUntil() {} });
  return [r.status, r.status === 200 ? await r.json() : null];
}
const tgSends = () => calls.filter((c) => c.url.includes("api.telegram.org") && c.url.endsWith("/sendMessage"));

test("API вимагає секрет", async () => {
  const r = await worker.fetch(new Request("https://w.test/api/users", { method: "POST", body: "{}" }), mkEnv(), { waitUntil() {} });
  assert.equal(r.status, 403);
});

test("стан розсилок: set → get, ttl обмежено", async () => {
  const env = mkEnv();
  assert.deepEqual((await api(env, "/api/state/get", { keys: ["morning:2026-11-03:6"] }))[1], { values: { "morning:2026-11-03:6": null } });
  await api(env, "/api/state/set", { key: "morning:2026-11-03:6", value: { failed: ["5"], tries: 1 }, ttl: 10 });
  assert.deepEqual((await api(env, "/api/state/get", { keys: ["morning:2026-11-03:6"] }))[1].values["morning:2026-11-03:6"], { failed: ["5"], tries: 1 });
  assert.equal(env.USERS.m.get("s:morning:2026-11-03:6").ttl, 60);
});

test("перенос підписки (супергрупа) зберігає всі налаштування", async () => {
  const env = mkEnv();
  const meta = { g: "СОБ-11", m: 0, e: 1, c: 1, w: 0, s: 1, sg: 2, mh: 8, eh: 20 };
  await env.USERS.put("u:-1", "", { metadata: meta });
  await api(env, "/api/move", { from: "-1", to: "-1001" });
  assert.equal(env.USERS.m.has("u:-1"), false);
  assert.deepEqual(env.USERS.m.get("u:-1001").metadata, meta);
  const users = (await api(env, "/api/users"))[1].users;
  assert.deepEqual(users, [{ id: "-1001", ...meta }]);
});

test("watchdog: один алерт, без спаму, потім відновлення; уночі мовчить", async () => {
  const env = mkEnv();
  const noon = Date.parse("2026-11-06T10:00:00Z"); // 12:00 за Києвом
  await api(env, "/api/heartbeat", { admins: ["900"], repo: "o/r" });
  env.USERS.m.get("hb").value = JSON.stringify({ ...JSON.parse(env.USERS.m.get("hb").value), ts: noon - 5 * 3600e3 });
  calls = [];
  const run = (t) => new Promise((res) => worker.scheduled({ scheduledTime: t }, env, { waitUntil: (p) => p.then(res) }));
  await run(noon);
  assert.equal(tgSends().length, 1);
  assert.match(tgSends()[0].body.text, /не запускався вже ~5 год/);
  await run(noon + 3600e3);
  await run(noon + 2 * 3600e3);
  assert.equal(tgSends().length, 1); // нагадування не частіше ніж раз на 12 год
  await api(env, "/api/heartbeat", { admins: ["900"], repo: "o/r" });
  env.USERS.m.get("hb").value = JSON.stringify({ ...JSON.parse(env.USERS.m.get("hb").value), ts: noon + 2.5 * 3600e3 });
  await run(noon + 3 * 3600e3);
  assert.equal(tgSends().length, 2);
  assert.match(tgSends()[1].body.text, /відновилися/);
  assert.equal(env.USERS.m.has("wd"), false);
  // ніч: старий heartbeat, але о 03:00 за Києвом не будимо адміна
  calls = [];
  env.USERS.m.get("hb").value = JSON.stringify({ ts: Date.parse("2026-11-06T19:20:00Z"), admins: ["900"], repo: "o/r" });
  await run(Date.parse("2026-11-07T01:00:00Z"));
  assert.equal(tgSends().length, 0);
});

test("dispatch: лише з GH_DISPATCH_TOKEN і лише якщо запуску давно не було", async () => {
  const env = mkEnv({ GH_DISPATCH_TOKEN: "ghp_x" });
  const t = Date.parse("2026-11-06T08:30:00Z"); // 10:30 Київ
  const run = (tt) => new Promise((res) => worker.scheduled({ scheduledTime: tt }, env, { waitUntil: (p) => p.then(res) }));
  await env.USERS.put("hb", JSON.stringify({ ts: t - 20 * 60e3, admins: [], repo: "o/r" }));
  calls = [];
  await run(t);
  assert.equal(calls.filter((c) => c.url.includes("dispatches")).length, 0);
  await env.USERS.put("hb", JSON.stringify({ ts: t - 90 * 60e3, admins: [], repo: "o/r" }));
  await run(t);
  const d = calls.filter((c) => c.url.includes("dispatches"));
  assert.equal(d.length, 1);
  assert.equal(d[0].url, "https://api.github.com/repos/o/r/actions/workflows/rozklad.yml/dispatches");
  assert.deepEqual(d[0].body, { ref: "main", inputs: { mode: "auto" } });
});

test("дані групи беруться з версією index.hash (без застарілого кешу)", async () => {
  const env = mkEnv();
  await env.USERS.put("u:42", "", { metadata: { g: "СОБ-11", m: 1, e: 1, c: 1, w: 1, s: 0, sg: 0, mh: 6, eh: 18 } });
  calls = [];
  let done;
  await worker.fetch(new Request("https://w.test/", { method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ message: { chat: { id: 42, type: "private" }, text: "/today" } }) }), env, { waitUntil: (p) => (done = p) });
  await done;
  assert.ok(calls.some((c) => c.url === SITE + "data/g/sob-11.json?v=abc123"), calls.map((c) => c.url).join("\n"));
});
