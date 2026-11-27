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

test("режим викладача у воркері: кнопки, пошук за прізвищем і cron-розсилка", async () => {
  const env = mkEnv();
  const realToday = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Kyiv", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  const day = "2026-11-03";
  const emptyDay = "2026-11-04";
  const lessonSample = { pair: 1, start: "08:00", end: "09:20", type: "Пр", subject: "Інформатика", groups: ["СОБ-11", "СОІ-12"], group_label: "СОБ-11, СОІ-12", room: "205" };
  const idx = {
    hash: "ht1",
    courses: { 1: [{ name: "СОБ-11", slug: "sob-11", room: "202" }] },
    teachers: [
      { name: "Губанов А.Б.", slug: "t-hubanov-a-b", degree: "", courses: [1], subjects: ["Інформатика"] },
      { name: "Гнедик Ю.О.", aliases: ["Стешенко Ю.О."], slug: "t-hnedyk-iu-o", degree: "", courses: [1], subjects: ["Всесвітня історія"] },
    ],
  };
  const teacherDoc = {
    kind: "teacher",
    group: "Губанов А.Б.",
    teacher: "Губанов А.Б.",
    slug: "t-hubanov-a-b",
    days: {
      [realToday]: { items: [lessonSample] },
      [day]: { items: [lessonSample] },
      [emptyDay]: { items: [] },
    },
    changes: [],
  };
  const hnedDoc = {
    kind: "teacher",
    group: "Гнедик Ю.О.",
    teacher: "Гнедик Ю.О.",
    slug: "t-hnedyk-iu-o",
    days: { [realToday]: { items: [{ pair: 2, start: "09:30", end: "10:50", type: "Пр", subject: "Всесвітня історія", groups: ["ПД-11"], group_label: "ПД-11", room: "" }] } },
    changes: [],
  };
  await api(env, "/api/state/set", {
    key: "site:bundle",
    value: { hash: "ht1", updated: "2026-11-03T05:00:00+02:00", index: idx, groups: { "t-hubanov-a-b": teacherDoc, "t-hnedyk-iu-o": hnedDoc } },
    ttl: 86400,
  });

  // 1) Пошук за прізвищем у повідомленні
  calls = [];
  let done;
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ message: { chat: { id: 777, type: "private" }, text: "Губанов" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  const saved = await env.USERS.getWithMetadata("u:777");
  assert.equal(saved.metadata.g, "t-hubanov-a-b");
  assert.ok(tgSends().some((c) => c.body.text.includes("Обрано розклад викладача") && c.body.text.includes("Губанов А.Б.")));
  assert.ok(tgSends().some((c) => c.body.text.includes("Інформатика") && c.body.text.includes("СОБ-11, СОІ-12")));

  // 2) Кнопка "Я викладач" → список teachers:0
  calls = [];
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ callback_query: { id: "cb_t", from: { id: 778 }, message: { chat: { id: 778 }, message_id: 5, text: "Обери" }, data: "teachers:0" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  assert.ok(calls.some((c) => c.url.endsWith("/editMessageText") && JSON.stringify(c.body.reply_markup).includes("t-hubanov-a-b")));

  // 3) Cron о 06:00: 03.11 (є пара) → шле; 04.11 (порожній день) → мовчить
  const run = (iso) => new Promise((res) => worker.scheduled({ scheduledTime: Date.parse(iso) }, env, { waitUntil: (p) => p.then(res) }));
  calls = [];
  await run("2026-11-03T04:00:00Z");
  assert.equal(tgSends().length, 1);
  assert.ok(tgSends()[0].body.text.includes("СОБ-11, СОІ-12"));

  calls = [];
  await run("2026-11-04T04:00:00Z");
  assert.equal(tgSends().length, 0);

  // 4) Пошук за колишнім прізвищем "Стешенко" → підписує на t-hnedyk-iu-o (Гнедик Ю.О.)
  calls = [];
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ message: { chat: { id: 779, type: "private" }, text: "Стешенко" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  assert.equal((await env.USERS.getWithMetadata("u:779")).metadata.g, "t-hnedyk-iu-o");
});

test("Mini App (docs/app.js): вибір викладача завантажує data/t/*.json і працює ?t=Стешенко", async () => {
  const vm = await import("node:vm");
  const { readFileSync } = await import("node:fs");
  const appSrc = readFileSync(new URL("../docs/app.js", import.meta.url), "utf8");
  const todayIso = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Kyiv", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  const idx = {
    hash: "v7", short: "ПФК НЮУ", bot: "pfkrozkladBot", source_page: "https://nlu.edu.ua/", updated: `${todayIso}T14:00`,
    courses: { 1: [{ name: "СОБ-11", slug: "sob-11", room: "202" }] },
    teachers: [
      { name: "Губанов А.Б.", slug: "t-hubanov-a-b", degree: "", courses: [1], subjects: ["Інформатика"] },
      { name: "Гнедик Ю.О.", aliases: ["Стешенко Ю.О."], slug: "t-hnedyk-iu-o", degree: "", courses: [1], subjects: ["Всесвітня історія"] },
    ],
    pdfs: [],
  };
  const tHub = {
    kind: "teacher", group: "Губанов А.Б.", teacher: "Губанов А.Б.", slug: "t-hubanov-a-b", courses: [1], subjects: ["Інформатика"],
    days: { [todayIso]: { items: [{ pair: 1, start: "08:00", end: "09:20", type: "Пр", subject: "Інформатика", groups: ["СОБ-11"], group_label: "СОБ-11", room: "305" }] } },
    changes: [],
  };
  const tHned = {
    kind: "teacher", group: "Гнедик Ю.О.", teacher: "Гнедик Ю.О.", slug: "t-hnedyk-iu-o", courses: [1], subjects: ["Всесвітня історія"],
    days: { [todayIso]: { items: [{ pair: 2, start: "09:30", end: "10:50", type: "Пр", subject: "Всесвітня історія", groups: ["ПД-11"], group_label: "ПД-11", room: "" }] } },
    changes: [],
  };

  const els = new Map();
  const mkEl = (id) => ({ id, innerHTML: "", textContent: "", href: "", hidden: false, dataset: {}, classList: { toggle() {} }, setAttribute() {}, addEventListener() {} });
  for (const id of ["#view", "#title", "#subtitle", "#ics", "#banner", "#changes-dot", "#checked", "#src", "#brand-btn"]) els.set(id, mkEl(id));
  const fetched = [];
  const ctx = vm.createContext({
    URL, URLSearchParams, Intl, Date, Set, Promise, Math, Object, Array, String, Number, Boolean, RegExp,
    location: { href: "https://site.test/?t=%D0%A1%D1%82%D0%B5%D1%88%D0%B5%D0%BD%D0%BA%D0%BE", pathname: "/", search: "?t=%D0%A1%D1%82%D0%B5%D1%88%D0%B5%D0%BD%D0%BA%D0%BE", hash: "" },
    history: { replaceState() {} },
    localStorage: { _m: {}, getItem(k) { return this._m[k] || null; }, setItem(k, v) { this._m[k] = v; } },
    navigator: {},
    window: { addEventListener() {}, matchMedia: () => ({ matches: false }), scrollTo() {} },
    document: { hidden: false, title: "", querySelector: (s) => els.get(s) || null, querySelectorAll: () => [], addEventListener() {} },
    setInterval: () => 0,
    fetch: async (url) => {
      fetched.push(url);
      if (url.startsWith("data/index.json")) return { ok: true, json: async () => idx };
      if (url.startsWith("data/t/t-hnedyk-iu-o.json")) return { ok: true, json: async () => tHned };
      if (url.startsWith("data/t/t-hubanov-a-b.json")) return { ok: true, json: async () => tHub };
      throw new Error("unexpected fetch: " + url);
    },
  });
  vm.runInContext(appSrc, ctx);
  await new Promise((r) => setTimeout(r, 20));
  assert.ok(fetched.some((u) => u.startsWith("data/t/t-hnedyk-iu-o.json")));
  assert.equal(els.get("#title").textContent, "Гнедик Ю.О.");
  assert.ok(els.get("#view").innerHTML.includes("Всесвітня історія") && els.get("#view").innerHTML.includes("ПД-11"));

  await ctx.selectGroup("t-hubanov-a-b");
  assert.ok(fetched.some((u) => u.startsWith("data/t/t-hubanov-a-b.json")));
  assert.ok(!fetched.some((u) => u.startsWith("data/g/")));
  assert.equal(els.get("#title").textContent, "Губанов А.Б.");
  assert.ok(els.get("#view").innerHTML.includes("Інформатика") && els.get("#view").innerHTML.includes("СОБ-11"));
});

test("воркер: консультації (/consult), пошук «Теорія права», підписка, нагадування за 30 хв і rs:fix", async () => {
  const env = mkEnv({ GH_DISPATCH_TOKEN: "ghp_x", GITHUB_REPO: "o/r", TELEGRAM_CHAT_ID: "900" });
  const cons = {
    updated: "2026-11-27T18:00",
    sources: [{ title: "ЦК права", url: "https://nlu.edu.ua/pravo.pdf" }],
    items: [
      { id: "t-noha-p-p:1:1410", teacher: "Нога П.П.", full_name: "Нога Петро Петрович", slug: "t-noha-p-p", day: "Понеділок", day_num: 1, start: "14:10", end: "15:30", subjects: ["Фінансове право", "Теорія права (ПД-14)"] },
      { id: "t-kryshtal-iu-v:2:1410", teacher: "Кришталь Ю.В.", full_name: "Кришталь Юлія Віталіївна", slug: "t-kryshtal-iu-v", day: "Вівторок", day_num: 2, start: "14:10", end: "16:00", subjects: ["Теорія права"] },
    ],
  };
  await api(env, "/api/state/set", {
    key: "site:bundle",
    value: { hash: "hc1", updated: "2026-11-27T18:00", index: INDEX, groups: { "sob-11": GROUP }, consultations: cons },
    ttl: 86400,
  });
  await env.USERS.put("u:50", "", { metadata: { g: "СОБ-11", m: 1, e: 1, c: 1, w: 1, s: 0, sg: 0, mh: 6, eh: 18 } });

  // 1) Пошук за предметом "Теорія права" у чаті знаходить і Нога П.П., і Кришталь Ю.В.
  calls = [];
  let done;
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ message: { chat: { id: 50, type: "private" }, text: "Теорія права" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  const searchMsg = tgSends().at(-1).body;
  assert.ok(searchMsg.text.includes("Нога П.П.") && searchMsg.text.includes("Кришталь Ю.В."));

  // 2) Підписка на нагадування через callback co:sub:t-kryshtal-iu-v:2:1410:today
  calls = [];
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ callback_query: { id: "cb_co", from: { id: 50 }, message: { chat: { id: 50 }, message_id: 9, text: "Відпрацювання" }, data: "co:sub:t-kryshtal-iu-v:2:1410:today" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  assert.deepEqual(await env.USERS.get("cs:50", "json"), ["t-kryshtal-iu-v:2:1410"]);

  // 3) Cron о 13:40 у вівторок (за 30 хв до 14:10) надсилає нагадування 1 раз без дублів
  const run = (iso) => new Promise((res) => worker.scheduled({ scheduledTime: Date.parse(iso) }, env, { waitUntil: (p) => p.then(res) }));
  calls = [];
  // Вівторок 2026-12-01 13:40 за Києвом = 11:40 UTC
  await run("2026-12-01T11:40:00Z");
  assert.equal(tgSends().length, 1);
  assert.ok(tgSends()[0].body.text.includes("Нагадування про відпрацювання") && tgSends()[0].body.text.includes("Кришталь Ю.В."));
  await run("2026-12-01T11:40:00Z");
  assert.equal(tgSends().length, 1);

  // 4) Callback rs:<did>:fix для сумніву kind="date"
  await api(env, "/api/state/set", {
    key: "doubt:ddate1",
    value: { id: "ddate1", kind: "date", field: "to", suggested: "2026-12-10", lesson: { pair: 2, subject: "Фінансове право" } },
    ttl: 86400,
  });
  calls = [];
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ callback_query: { id: "cb_fix", from: { id: 900 }, message: { chat: { id: 900 }, message_id: 11, text: "Сумнівна дата" }, data: "rs:ddate1:fix" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  const savedRes = await env.USERS.get("s:resolutions", "json");
  assert.equal(savedRes.ddate1.action, "fix");
  assert.equal(savedRes.ddate1.suggested, "2026-12-10");
});

test("воркер: автооновлення MENU для давніх підписників, інлайн co:today та co:mine лише за викладачами групи", async () => {
  const env = mkEnv();
  const cons = {
    updated: "2026-11-27T18:00",
    sources: [],
    items: [
      { id: "t-noha-p-p:1:1410", teacher: "Нога П.П.", full_name: "Нога Петро Петрович", slug: "t-noha-p-p", day: "Понеділок", day_num: 1, start: "14:10", end: "15:30", subjects: ["Теорія права"] },
      { id: "t-kryshtal-iu-v:2:1410", teacher: "Кришталь Ю.В.", full_name: "Кришталь Юлія Віталіївна", slug: "t-kryshtal-iu-v", day: "Вівторок", day_num: 2, start: "14:10", end: "16:00", subjects: ["Теорія права"] },
    ],
  };
  const grp = {
    group: "СОБ-11", course: 1, slug: "sob-11", changes: [],
    days: { "2026-12-01": { items: [{ pair: 1, start: "08:00", end: "09:20", type: "Пр", subject: "Теорія права", teacher: "Кришталь Ю.В." }] } },
  };
  await api(env, "/api/state/set", {
    key: "site:bundle",
    value: { hash: "hm1", updated: "2026-11-27T18:00", index: INDEX, groups: { "sob-11": grp }, consultations: cons },
    ttl: 86400,
  });
  // Старий підписник без mv
  await env.USERS.put("u:55", "", { metadata: { g: "СОБ-11", m: 1, e: 1, c: 1, w: 1, s: 0, sg: 0, mh: 6, eh: 18 } });

  // 1) Перше /today: надсилає розклад з інлайн-кнопкою co:today + тихе оновлення MENU з Відпрацювання (mv: 2)
  calls = [];
  let done;
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ message: { chat: { id: 55, type: "private" }, text: "/today" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  const sends = tgSends();
  assert.equal(sends.length, 2);
  assert.ok(JSON.stringify(sends[0].body.reply_markup).includes('"co:today"'));
  assert.ok(JSON.stringify(sends[1].body.reply_markup).includes("📝 Відпрацювання"));
  assert.equal(sends[1].body.disable_notification, true);
  assert.equal((await env.USERS.getWithMetadata("u:55")).metadata.mv, 2);

  // 2) Наступний запит /today вже НЕ спамить оновленням меню
  calls = [];
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ message: { chat: { id: 55, type: "private" }, text: "/today" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  assert.equal(tgSends().length, 1);

  // 3) co:mine показує Кришталь Ю.В. і виключає Ногу П.П. (чужий викладач того ж предмета)
  calls = [];
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ callback_query: { id: "cb_m", from: { id: 55 }, message: { chat: { id: 55 }, message_id: 12, text: "Відпр" }, data: "co:mine" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  const edited = calls.find((c) => c.url.endsWith("/editMessageText")).body.text;
  assert.ok(edited.includes("Кришталь Ю.В.") && !edited.includes("Нога П.П."));
});

test("Mini App (docs/app.js): вкладка Відпрац. (вівторок 14:20 = 8 зараз), пошук Теорія права, вибір тижня та Сьогодні/Завтра", async () => {
  const vm = await import("node:vm");
  const { readFileSync } = await import("node:fs");
  const appSrc = readFileSync(new URL("../docs/app.js", import.meta.url), "utf8");
  const cons = JSON.parse(readFileSync(new URL("./golden/consultations.json", import.meta.url), "utf8"));
  let curHash = "v8";
  let curSubject2 = "Математика";
  const idx = () => ({
    hash: curHash, short: "ПФК НЮУ", bot: "pfkrozkladBot", source_page: "https://nlu.edu.ua/",
    updated: curHash === "v8" ? "2026-12-01T14:20" : "2026-12-01T15:10",
    courses: { 1: [{ name: "ПД-14", slug: "pd-14", room: "305" }] },
    teachers: [{ name: "Нога П.П.", slug: "t-noha-p-p", degree: "", courses: [1, 3], subjects: ["Фінансове право"] }],
    pdfs: [],
  });
  const grpDoc = () => ({
    group: "ПД-14", course: 1, slug: "pd-14", room: "305", changes: [],
    days: {
      "2026-12-01": { items: [
        { pair: 1, start: "08:00", end: "09:20", type: "Пр", subject: "Теорія права", teacher: "Кришталь Ю.В." },
        { pair: 2, start: "09:30", end: "10:50", type: "Пр", subject: curSubject2, teacher: "Харченко А.В." },
      ] },
      "2026-12-08": { items: [
        { pair: 1, start: "08:00", end: "09:20", type: "Лк", subject: "Історія України", teacher: "к.і.н. Крайсвітня Ю.В." },
      ] },
    },
  });

  // Фіксований час: вівторок 2026-12-01 14:20 за Києвом (12:20 UTC), далі можна зсунути на неділю
  let fakeNowMs = Date.parse("2026-12-01T12:20:00Z");
  const FixedDate = class extends Date {
    constructor(...args) { super(...(args.length ? args : [fakeNowMs])); }
    static now() { return fakeNowMs; }
  };
  const els = new Map();
  const listeners = {};
  const mkEl = (id) => ({
    id, innerHTML: "", textContent: "", href: "", hidden: false, dataset: {},
    classList: { toggle() {} },
    setAttribute() {},
    addEventListener(ev, fn) { (listeners[id] = listeners[id] || {})[ev] = fn; },
    querySelectorAll: () => [],
    querySelector: () => null,
  });
  for (const id of ["#view", "#title", "#subtitle", "#ics", "#banner", "#changes-dot", "#checked", "#src", "#brand-btn", "#sr-status"]) els.set(id, mkEl(id));
  const ctx = vm.createContext({
    URL, URLSearchParams, Intl, Date: FixedDate, Set, Map, Promise, Math, Object, Array, String, Number, Boolean, RegExp, encodeURIComponent,
    location: { href: "https://site.test/?g=ПД-14#consult", pathname: "/", search: "?g=ПД-14", hash: "#consult" },
    history: { replaceState() {} },
    localStorage: { _m: {}, getItem(k) { return this._m[k] || null; }, setItem(k, v) { this._m[k] = v; } },
    navigator: {},
    window: { addEventListener() {}, matchMedia: () => ({ matches: false }), scrollTo() {} },
    document: { hidden: false, title: "", querySelector: (s) => els.get(s) || null, querySelectorAll: () => [], addEventListener() {} },
    setInterval: () => 0,
    fetch: async (url) => {
      if (url.startsWith("data/index.json")) return { ok: true, json: async () => idx() };
      if (url.startsWith("data/consultations.json")) return { ok: true, json: async () => ({ items: cons, sources: [] }) };
      if (url.startsWith("data/g/pd-14.json")) return { ok: true, json: async () => grpDoc() };
      throw new Error("unexpected fetch: " + url);
    },
  });
  vm.runInContext(appSrc, ctx);
  await new Promise((r) => setTimeout(r, 20));
  const htmlConsult = els.get("#view").innerHTML;
  assert.ok(htmlConsult.includes("Консультують 8 викладачів"), "Expected 8 live teachers at Tue 14:20");
  assert.ok(htmlConsult.includes("consult-live") && !htmlConsult.includes('class="hero'), "Should use compact live bar without hero");

  // "Мої предмети": Кришталь Ю.В., Харченко А.В., Крайсвітня Ю.В. (з к.і.н.) є; чужі Нога П.П. та Овсяник С.А. відсутні
  ctx.CONSULT_MODE = "mine";
  ctx.render();
  const htmlMine = els.get("#view").innerHTML;
  assert.ok(htmlMine.includes("Кришталь Ю.В.") && htmlMine.includes("Харченко А.В.") && htmlMine.includes("Крайсвітня Ю.В."));
  assert.ok(!htmlMine.includes("Нога П.П.") && !htmlMine.includes("Овсяник С.А."));
  ctx.CONSULT_MODE = "today";

  // У неділю (поза годинами консультацій) верхнього банера/hero немає взагалі
  fakeNowMs = Date.parse("2026-12-06T10:00:00Z");
  ctx.render();
  const htmlSun = els.get("#view").innerHTML;
  assert.ok(!htmlSun.includes("consult-live") && !htmlSun.includes('class="hero'));
  fakeNowMs = Date.parse("2026-12-01T12:20:00Z");

  // Пошук "Теорія права" знаходить і Кришталь Ю.В., і Нога П.П.
  ctx.CONSULT_Q = "Теорія права";
  ctx.render();
  const htmlSearch = els.get("#view").innerHTML;
  assert.ok(htmlSearch.includes("Кришталь Ю.В.") && htmlSearch.includes("Нога П.П."));

  // Вкладка Тиждень: перемикання на 07.12–11.12 і відмінювання "2 пари"
  ctx.TAB = "week";
  ctx.render();
  const htmlWeek1 = els.get("#view").innerHTML;
  assert.ok(htmlWeek1.includes("30.11–04.12") && htmlWeek1.includes("2 пари"));
  assert.deepEqual([...ctx.availWeeks("2026-12-01").weeks], ["2026-11-30", "2026-12-07"]);
  ctx.WEEK_MON = "2026-12-07";
  ctx.WEEK_SEL = "2026-12-08";
  ctx.render();
  assert.ok(els.get("#view").innerHTML.includes("07.12–11.12") && els.get("#view").innerHTML.includes("Історія України"));

  // Підказка консультації у картці пари студента (зокрема з к.і.н.)
  assert.ok(els.get("#view").innerHTML.includes("Консультація: вт 14:10–15:00"));
  ctx.TAB = "today";
  ctx.render();
  assert.ok(els.get("#view").innerHTML.includes("Консультація: вт 14:10–16:00"));

  // Автооновлення при зміні index.json.hash
  curHash = "v9";
  curSubject2 = "Вища математика";
  const updated = await ctx.checkUpdates(true);
  assert.equal(updated, true);
  assert.ok(els.get("#view").innerHTML.includes("Вища математика"));
  assert.ok(els.get("#checked").textContent.includes("15:10"));
});

test("воркер: /stats доступно лише адміну і показує зведення, групи та викладачів", async () => {
  const env = mkEnv({ TELEGRAM_CHAT_ID: "900" });
  const idx = {
    hash: "hs1",
    updated: "2026-11-27T19:00+02:00",
    courses: {
      1: [{ name: "СОБ-11", slug: "sob-11" }, { name: "ПД-14", slug: "pd-14" }],
      2: [{ name: "ПД-21", slug: "pd-21" }],
    },
    teachers: [{ name: "Губанов А.Б.", slug: "t-hubanov-a-b", courses: [1, 2], subjects: ["Інформатика"] }],
  };
  await api(env, "/api/state/set", { key: "site:bundle", value: { hash: "hs1", updated: idx.updated, index: idx, groups: { "sob-11": GROUP } }, ttl: 86400 });
  await api(env, "/api/heartbeat", { admins: ["900"], repo: "o/r" });
  const today = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Kyiv", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
  await env.USERS.put("u:101", "", { metadata: { g: "СОБ-11", m: 1, e: 1, c: 1, w: 1, s: 0, sg: 0, mh: 6, eh: 18, mv: 2, at: today } });
  await env.USERS.put("u:102", "", { metadata: { g: "ПД-21", m: 1, e: 0, c: 1, w: 0, s: 1, sg: 0, mh: 7, eh: 19 } });
  await env.USERS.put("u:103", "", { metadata: { g: "t-hubanov-a-b", m: 1, e: 1, c: 1, w: 1, s: 0, sg: 0, mh: 6, eh: 18, mv: 2 } });
  await env.USERS.put("u:-500", "", { metadata: { g: "СОБ-11", m: 1, e: 1, c: 1, w: 1, s: 0, sg: 0, mh: 6, eh: 18 } });
  await env.USERS.put("cs:101", JSON.stringify(["t-noha-p-p:1:1410"]));

  // Новий користувач обирає групу СОБ-11 -> записується at = сьогодні
  calls = [];
  let done;
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ message: { chat: { id: 104, type: "private" }, from: { id: 104 }, text: "СОБ-11" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  assert.equal((await env.USERS.getWithMetadata("u:104")).metadata.at, today);

  // Чужий користувач не має доступу до /stats і st:*
  calls = [];
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ message: { chat: { id: 101, type: "private" }, from: { id: 101 }, text: "/stats" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  assert.match(tgSends()[0].body.text, /лише адміністратору/);

  calls = [];
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ callback_query: { id: "cb_no", from: { id: 101 }, message: { chat: { id: 101 }, message_id: 1, text: "x" }, data: "st:groups" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  assert.ok(calls.some((c) => c.url.endsWith("/answerCallbackQuery") && c.body.show_alert === true));

  // Адмін отримує /stats
  calls = [];
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ message: { chat: { id: 900, type: "private" }, from: { id: 900 }, text: "/stats" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  const stMsg = tgSends()[0].body;
  assert.ok(stMsg.text.includes("Усього підписників: <b>5</b>") && stMsg.text.includes("сьогодні: <b>+2</b>"));
  assert.ok(stMsg.text.includes("Приватних: <b>4</b>") && stMsg.text.includes("Груп-чатів: <b>1</b>"));
  assert.ok(stMsg.text.includes("Студентів: <b>4</b>") && stMsg.text.includes("Викладачів: <b>1</b>"));
  assert.ok(stMsg.text.includes("Охоплено груп: <b>2 / 3</b>"));
  assert.ok(stMsg.text.includes("СОБ-11 — <b>3</b>"));
  assert.ok(stMsg.text.includes("Відпрацювання (нагадування): <b>1</b>"));

  // Перемикання вкладок st:groups і st:teachers
  calls = [];
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ callback_query: { id: "cb_g", from: { id: 900 }, message: { chat: { id: 900 }, message_id: 50, text: "stats" }, data: "st:groups" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  const gText = calls.find((c) => c.url.endsWith("/editMessageText")).body.text;
  assert.ok(gText.includes("1 курс (3)") && gText.includes("ПД-21 — <b>1</b>") && gText.includes("ПД-14"));

  calls = [];
  await worker.fetch(new Request("https://w.test/", {
    method: "POST",
    headers: { "X-Telegram-Bot-Api-Secret-Token": SECRET },
    body: JSON.stringify({ callback_query: { id: "cb_t", from: { id: 900 }, message: { chat: { id: 900 }, message_id: 50, text: "stats" }, data: "st:teachers" } }),
  }), env, { waitUntil: (p) => (done = p) });
  await done;
  const tText = calls.find((c) => c.url.endsWith("/editMessageText")).body.text;
  assert.ok(tText.includes("Губанов А.Б. — <b>1</b>") && tText.includes("Групові чати (1)"));
});
