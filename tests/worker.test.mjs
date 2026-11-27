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

test("свіжіший знімок у KV перекриває застарілий GitHub Pages і віддається на /data/*", async () => {
  const env = mkEnv();
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Kyiv", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  const kvBundle = {
    hash: "fresh99",
    updated: "2099-01-01T12:00:00+02:00",
    index: { hash: "fresh99", updated: "2099-01-01T12:00:00+02:00", courses: { 1: [{ name: "СОБ-11", slug: "sob-11", room: "101" }] } },
    groups: { "sob-11": { group: "СОБ-11", course: 1, slug: "sob-11", days: { [today]: { items: [{ pair: 1, start: "08:00", end: "09:20", subject: "Свіжа пара з KV" }] } }, changes: [] } },
  };
  await api(env, "/api/state/set", { key: "site:bundle", value: kvBundle, ttl: 86400 });
  await env.USERS.put("u:42", "", { metadata: { g: "СОБ-11", m: 1, e: 1, c: 1, w: 1, s: 0, sg: 0, mh: 6, eh: 18 } });
  calls = [];
  let done;
  await worker.fetch(new Request("https://w.test/", { method: "POST", headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ message: { chat: { id: 42, type: "private" }, text: "/today" } }) }), env, { waitUntil: (p) => (done = p) });
  await done;
  assert.match(tgSends()[0].body.text, /Свіжа пара з KV/);

  const idxRes = await worker.fetch(new Request("https://w.test/data/index.json"), env, { waitUntil() {} });
  assert.equal(idxRes.status, 200);
  assert.equal(idxRes.headers.get("Access-Control-Allow-Origin"), "*");
  assert.equal((await idxRes.json()).hash, "fresh99");

  const grpRes = await worker.fetch(new Request("https://w.test/data/g/sob-11.json"), env, { waitUntil() {} });
  assert.equal(grpRes.status, 200);
  assert.equal((await grpRes.json()).days[today].items[0].subject, "Свіжа пара з KV");
});

test("cron воркера: точний ранок о 06:00 за Києвом, підгрупа, беззвучний режим і захист від дублів", async () => {
  const env = mkEnv({ NO_DELAY: "1" });
  const day = "2026-11-03";
  await api(env, "/api/state/set", {
    key: "site:bundle",
    value: {
      hash: "h1",
      updated: "2026-11-03T05:00:00+02:00",
      index: { hash: "h1", courses: { 1: [{ name: "СОБ-11", slug: "sob-11", room: "101" }] } },
      groups: {
        "sob-11": {
          group: "СОБ-11",
          course: 1,
          slug: "sob-11",
          days: {
            [day]: {
              items: [
                { pair: 1, start: "08:00", end: "09:20", subject: "Спільна" },
                { pair: 2, start: "09:30", end: "10:50", subject: "Лише 1", subgroup: 1 },
                { pair: 2, start: "09:30", end: "10:50", subject: "Лише 2", subgroup: 2 },
              ],
            },
          },
          changes: [],
        },
      },
    },
    ttl: 86400,
  });
  await env.USERS.put("u:1", "", { metadata: { g: "СОБ-11", m: 1, e: 1, c: 1, w: 1, s: 1, sg: 2, mh: 6, eh: 18 } });
  await env.USERS.put("u:2", "", { metadata: { g: "СОБ-11", m: 0, e: 1, c: 1, w: 1, s: 0, sg: 0, mh: 6, eh: 18 } });
  await env.USERS.put("u:3", "", { metadata: { g: "СОБ-11", m: 1, e: 1, c: 1, w: 1, s: 0, sg: 0, mh: 7, eh: 18 } });

  const run = (iso) => new Promise((res) => worker.scheduled({ scheduledTime: Date.parse(iso) }, env, { waitUntil: (p) => p.then(res) }));
  calls = [];
  // 05:55 за Києвом (03:55 UTC взимку) — зарано
  await run("2026-11-03T03:55:00Z");
  assert.equal(tgSends().length, 0);

  // 06:00 за Києвом (04:00 UTC) — приходить лише u:1, з KV (0 зовнішніх fetch до SITE_URL)
  await run("2026-11-03T04:00:00Z");
  assert.equal(tgSends().length, 1);
  assert.equal(tgSends()[0].body.chat_id, "1");
  assert.equal(tgSends()[0].body.disable_notification, true);
  assert.match(tgSends()[0].body.text, /Спільна/);
  assert.match(tgSends()[0].body.text, /Лише 2/);
  assert.doesNotMatch(tgSends()[0].body.text, /Лише 1/);
  assert.equal(calls.filter((c) => c.url.startsWith(SITE)).length, 0);

  // 06:01 і 06:15 — дублів немає
  await run("2026-11-03T04:01:00Z");
  await run("2026-11-03T04:15:00Z");
  assert.equal(tgSends().length, 1);

  // 07:00 за Києвом — приходить u:3, u:1 дубля не отримує
  await run("2026-11-03T05:00:00Z");
  assert.equal(tgSends().length, 2);
  assert.equal(tgSends()[1].body.chat_id, "3");
});

test("cron воркера: вечір о 18:00, тиша в суботу та тиждень у неділю", async () => {
  const env = mkEnv({ NO_DELAY: "1" });
  await api(env, "/api/state/set", {
    key: "site:bundle",
    value: {
      hash: "h2",
      updated: "2026-11-06T12:00:00+02:00",
      index: { hash: "h2", courses: { 1: [{ name: "СОБ-11", slug: "sob-11", room: "" }] } },
      groups: {
        "sob-11": {
          group: "СОБ-11",
          course: 1,
          slug: "sob-11",
          days: {
            "2026-11-04": { items: [{ pair: 1, start: "08:00", end: "09:20", subject: "Пари середовища" }] },
            "2026-11-09": { items: [{ pair: 1, start: "08:00", end: "09:20", subject: "Новий тиждень" }] },
          },
          changes: [],
        },
      },
    },
    ttl: 86400,
  });
  await env.USERS.put("u:10", "", { metadata: { g: "СОБ-11", m: 1, e: 1, c: 1, w: 1, s: 0, sg: 0, mh: 6, eh: 18 } });

  const run = (iso) => new Promise((res) => worker.scheduled({ scheduledTime: Date.parse(iso) }, env, { waitUntil: (p) => p.then(res) }));
  calls = [];
  // Вівторок 03.11 18:00 Київ (16:00 UTC) → пари на 04.11 («Завтра»)
  await run("2026-11-03T16:00:00Z");
  assert.equal(tgSends().length, 1);
  assert.match(tgSends()[0].body.text, /Завтра.*04\.11/);

  // Субота 07.11 18:00 Київ (16:00 UTC) → нічого не шле
  await run("2026-11-07T16:00:00Z");
  assert.equal(tgSends().length, 1);

  // Неділя 08.11 18:00 Київ (16:00 UTC) → огляд тижня 09.11–13.11
  await run("2026-11-08T16:00:00Z");
  assert.equal(tgSends().length, 2);
  assert.match(tgSends()[1].body.text, /Тиждень 09\.11–13\.11/);
});

test("cron воркера: батчинг 80 підписників по 35 за хвилину без перевищення лімітів", async () => {
  const env = mkEnv({ NO_DELAY: "1" });
  const day = "2026-11-03";
  await api(env, "/api/state/set", {
    key: "site:bundle",
    value: {
      hash: "h3",
      updated: "2026-11-03T05:00:00+02:00",
      index: { hash: "h3", courses: { 1: [{ name: "СОБ-11", slug: "sob-11", room: "" }] } },
      groups: { "sob-11": { group: "СОБ-11", course: 1, slug: "sob-11", days: { [day]: { items: [{ pair: 1, start: "08:00", end: "09:20", subject: "Тест" }] } }, changes: [] } },
    },
    ttl: 86400,
  });
  for (let i = 1; i <= 80; i++) {
    await env.USERS.put("u:" + i, "", { metadata: { g: "СОБ-11", m: 1, e: 1, c: 1, w: 1, s: 0, sg: 0, mh: 6, eh: 18 } });
  }
  const run = (iso) => new Promise((res) => worker.scheduled({ scheduledTime: Date.parse(iso) }, env, { waitUntil: (p) => p.then(res) }));
  calls = [];

  await run("2026-11-03T04:00:00Z"); // 06:00 → 35
  assert.equal(tgSends().length, 35);
  assert.equal((await env.USERS.get("s:morning:2026-11-03:6", "json")).pending.length, 45);

  await run("2026-11-03T04:01:00Z"); // 06:01 → +35 = 70
  assert.equal(tgSends().length, 70);
  assert.equal((await env.USERS.get("s:morning:2026-11-03:6", "json")).pending.length, 10);

  await run("2026-11-03T04:02:00Z"); // 06:02 → +10 = 80
  assert.equal(tgSends().length, 80);
  const doneState = await env.USERS.get("s:morning:2026-11-03:6", "json");
  assert.equal(doneState.pending, undefined);
  assert.equal(doneState.tries, 1);

  await run("2026-11-03T04:03:00Z"); // 06:03 → 0 нових
  assert.equal(tgSends().length, 80);
  const uniqueIds = new Set(tgSends().map((s) => String(s.body.chat_id)));
  assert.equal(uniqueIds.size, 80);
});

test("cron воркера: обробка 403, migrate_to_chat_id, 502 і 429 без втрати підписників", async () => {
  const env = mkEnv({ NO_DELAY: "1" });
  const day = "2026-11-03";
  await api(env, "/api/state/set", {
    key: "site:bundle",
    value: {
      hash: "h4",
      updated: "2026-11-03T05:00:00+02:00",
      index: { hash: "h4", courses: { 1: [{ name: "СОБ-11", slug: "sob-11", room: "" }] } },
      groups: { "sob-11": { group: "СОБ-11", course: 1, slug: "sob-11", days: { [day]: { items: [{ pair: 1, start: "08:00", end: "09:20", subject: "Тест" }] } }, changes: [] } },
    },
    ttl: 86400,
  });
  await env.USERS.put("u:drop403", "", { metadata: { g: "СОБ-11", m: 1, mh: 6 } });
  await env.USERS.put("u:-1", "", { metadata: { g: "СОБ-11", m: 1, mh: 6, sg: 2, s: 1 } });
  await env.USERS.put("u:flaky", "", { metadata: { g: "СОБ-11", m: 1, mh: 6 } });
  // 6 користувачів, щоб перевірити 429 на першому мікро-батчі й перенос 6-го у pending
  await env.USERS.put("u:u4", "", { metadata: { g: "СОБ-11", m: 1, mh: 6 } });
  await env.USERS.put("u:u5", "", { metadata: { g: "СОБ-11", m: 1, mh: 6 } });
  await env.USERS.put("u:u6", "", { metadata: { g: "СОБ-11", m: 1, mh: 6 } });

  let failFlaky = true;
  let trigger429 = true;
  const origFetch = globalThis.fetch;
  globalThis.fetch = async (url, opts = {}) => {
    url = String(url);
    if (url.endsWith("/sendMessage")) {
      const body = JSON.parse(opts.body);
      calls.push({ url, body });
      if (body.chat_id === "drop403") return Response.json({ ok: false, error_code: 403, description: "Forbidden: bot was blocked by the user" });
      if (body.chat_id === "-1") return Response.json({ ok: false, error_code: 400, description: "Bad Request: group chat was upgraded to a supergroup chat", parameters: { migrate_to_chat_id: -1001 } });
      if (body.chat_id === "flaky" && failFlaky) { failFlaky = false; return Response.json({ ok: false, error_code: 502, description: "HTTP 502" }); }
      if (body.chat_id === "u4" && trigger429) { trigger429 = false; return Response.json({ ok: false, error_code: 429, description: "Too Many Requests" }); }
      return Response.json({ ok: true });
    }
    return origFetch(url, opts);
  };

  try {
    const run = (iso) => new Promise((res) => worker.scheduled({ scheduledTime: Date.parse(iso) }, env, { waitUntil: (p) => p.then(res) }));
    calls = [];
    await run("2026-11-03T04:00:00Z");

    // 403 видалено, -1 перенесено в -1001 і надіслано, flaky в failed, u4 і u6 (з 2-го мікро-батча) у pending
    assert.equal(env.USERS.m.has("u:drop403"), false);
    assert.equal(env.USERS.m.has("u:-1"), false);
    assert.equal(env.USERS.m.get("u:-1001").metadata.sg, 2);
    assert.ok(tgSends().some((c) => c.body.chat_id === -1001));
    const st1 = await env.USERS.get("s:morning:2026-11-03:6", "json");
    assert.deepEqual(st1.failed, ["flaky"]);
    assert.ok(st1.pending.includes("u4") && st1.pending.includes("u6"));

    // Наступна хвилина: досилає pending (u4, u6)
    await run("2026-11-03T04:01:00Z");
    assert.ok(tgSends().some((c) => c.body.chat_id === "u4"));
    assert.ok(tgSends().some((c) => c.body.chat_id === "u6"));
    const st2 = await env.USERS.get("s:morning:2026-11-03:6", "json");
    assert.equal(st2.pending, undefined);
    assert.deepEqual(st2.failed, ["flaky"]);

    // Ще хвилина: досилає лише flaky
    const beforeRetry = tgSends().length;
    await run("2026-11-03T04:02:00Z");
    assert.equal(tgSends().length, beforeRetry + 1);
    assert.equal(tgSends()[beforeRetry].body.chat_id, "flaky");
    assert.deepEqual((await env.USERS.get("s:morning:2026-11-03:6", "json")).failed, []);
  } finally {
    globalThis.fetch = origFetch;
  }
});

test("callback rs: зберігає відповідь адміна в KV, відхиляє чужих і тригерить dispatch", async () => {
  const env = mkEnv({ GH_DISPATCH_TOKEN: "ghp_x", GITHUB_REPO: "o/r", TELEGRAM_CHAT_ID: "900" });
  await api(env, "/api/heartbeat", { admins: ["900"], repo: "o/r" });
  await api(env, "/api/state/set", {
    key: "doubt:d123",
    value: {
      id: "d123",
      course: 3,
      pair: 3,
      groups: ["ПК-32-А"],
      options: [
        { type: "Лк", subject: "Основи національного спротиву", teacher: "Бородай С.О." },
        { type: "Пр", subject: "Криміналістичні експертизи", teacher: "Мороз С.Г." },
      ],
    },
    ttl: 86400,
  });

  calls = [];
  let done;
  // Не адмін — заборонено
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({
      callback_query: { id: "cb1", from: { id: 123 }, message: { chat: { id: 123 }, message_id: 1, text: "Питання" }, data: "rs:d123:1" },
    }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  assert.equal(await env.USERS.get("s:resolutions", "json"), null);
  assert.ok(calls.some((c) => c.url.endsWith("/answerCallbackQuery") && c.body.show_alert === true));

  // Адмін натиснув кнопку 2 (індекс 1)
  calls = [];
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({
      callback_query: { id: "cb2", from: { id: 900 }, message: { chat: { id: 900 }, message_id: 2, text: "Питання щодо PDF" }, data: "rs:d123:1" },
    }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  const saved = await env.USERS.get("s:resolutions", "json");
  assert.deepEqual(saved.d123.keep, ["Пр Криміналістичні експертизи"]);
  assert.equal(saved.d123.by, "900");
  assert.ok(calls.some((c) => c.url.endsWith("/answerCallbackQuery") && c.body.text === "Прийнято"));
  assert.ok(calls.some((c) => c.url.endsWith("/editMessageText") && /Прийнято/.test(c.body.text)));
  assert.ok(calls.some((c) => c.url.includes("/dispatches")));
});
