// Cloudflare Worker: Telegram-бот розкладу для всіх груп ПФК НЮУ (безкоштовний тариф).
// Розгортає Cloudflare Workers Builds при кожному пуші в main; прив'язки — у wrangler.json (KV створюється сам):
//   BOT_TOKEN (secret, задається в Cloudflare: Settings → Variables and Secrets) — токен бота; SITE_URL (var) — адреса сайту на GitHub Pages; USERS (KV) — підписники.
// Підписник = ключ "u:<chat id>" у KV, у metadata: { g: "СОБ-11", m: 1 (ранок), e: 1 (вечір), c: 1 (заміни) }.
// Інші ключі KV: "s:<розсилка>" — стан розсилок (bot.py), "hb" — останній запуск Actions, "wd" — тривога watchdog.
// Опційно: GH_DISPATCH_TOKEN (secret) — воркер сам запускає workflow, якщо GitHub пропускає cron; WATCHDOG_HOURS (var).

const DAYS = ["Неділя", "Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота"];
const SHORT = ["Нд", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];
const NUM = ["0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"];
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const dow = (iso) => new Date(iso + "T12:00:00Z").getUTCDay();
const ddmm = (iso) => iso.slice(8, 10) + "." + iso.slice(5, 7);
const addDays = (iso, n) => { const d = new Date(iso + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
const kyivISO = (d = new Date()) => new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Kyiv", year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
const mondayOf = (iso) => addDays(iso, dow(iso) === 0 ? -6 : 1 - dow(iso));
const norm = (s) => String(s || "").toUpperCase().replace(/[\s\-–'’ʼ]/g, "").replace(/I/g, "І").replace(/C/g, "С").replace(/O/g, "О").replace(/P/g, "Р").replace(/K/g, "К").replace(/A/g, "А").replace(/E/g, "Е");
const plural = (n) => (n % 10 === 1 && n % 100 !== 11 ? "пара" : n % 10 >= 2 && n % 10 <= 4 && !(n % 100 >= 12 && n % 100 <= 14) ? "пари" : "пар");
// m/e/c/w — ранок/вечір/заміни/огляд тижня (1/0); mh/eh — година ранкової/вечірньої розсилки; s — беззвучно; sg — підгрупа (0 = усі)
const DEFAULTS = { m: 1, e: 1, c: 1, w: 1, s: 0, sg: 0, mh: 6, eh: 18 };
const MORNING_HOURS = [6, 7, 8, 9];
const EVENING_HOURS = [17, 18, 19, 20, 21];
const MORNING_UNTIL = 13;
const EVENING_UNTIL = 23;
const MAX_SENDS_PER_TICK = 35;
const SEND_TRIES = 3;
const MICRO_BATCH = 5;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const PERM_DROP_RE = /blocked|deactivated|not found|kicked|PEER_ID|chat_id is empty/i;
const hh = (h) => String(h).padStart(2, "0") + ":00";

const MENU = {
  keyboard: [
    [{ text: "📅 Сьогодні", style: "primary" }, { text: "➡️ Завтра", style: "primary" }],
    [{ text: "🗓 Тиждень" }, { text: "🔄 Заміни" }],
    [{ text: "⚙️ Налаштування" }],
  ],
  resize_keyboard: true, is_persistent: true, input_field_placeholder: "Обери кнопку або напиши групу чи прізвище",
};

async function secretOf(token) {
  const h = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("pfk-rozklad:" + token));
  return [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 48);
}

// ---------------------------------------------------------------- дані з GitHub Pages або KV воркера
async function getJSON(env, path) {
  const base = env.SITE_URL.endsWith("/") ? env.SITE_URL : env.SITE_URL + "/";
  const r = await fetch(base + path, { cf: { cacheTtlByStatus: { "200-299": 120, "400-599": 0 } } });
  if (!r.ok) throw new Error(path + ": " + r.status);
  return r.json();
}
const tsOf = (x) => { const t = Date.parse(x?.updated || ""); return Number.isFinite(t) ? t : 0; };
async function loadSite(env) {
  const [pagesIndex, bundle] = await Promise.all([
    getJSON(env, "data/index.json").catch(() => null),
    env.USERS.get("s:site:bundle", "json").catch(() => null),
  ]);
  if (bundle?.index && (!pagesIndex || tsOf(bundle.index) >= tsOf(pagesIndex))) {
    return { index: bundle.index, groups: bundle.groups || null, fallbackGroups: bundle.groups || null };
  }
  if (pagesIndex) return { index: pagesIndex, groups: null, fallbackGroups: bundle?.groups || null };
  throw new Error("data/index.json unavailable");
}
// дані групи/викладача з версією з index.json або зі свіжого KV-знімка
const groupJSON = (env, site, slug) => {
  if (site.groups?.[slug]) return Promise.resolve(site.groups[slug]);
  const dir = String(slug || "").startsWith("t-") ? "t" : "g";
  return getJSON(env, `data/${dir}/${slug}.json?v=${encodeURIComponent(site.index.hash || "")}`)
    .catch((err) => {
      if (site.fallbackGroups?.[slug]) return site.fallbackGroups[slug];
      throw err;
    });
};
const allGroups = (index) => Object.entries(index.courses || {}).flatMap(([c, gs]) => gs.map((g) => ({ ...g, course: +c, kind: "group" })));
const allTeachers = (index) => (index.teachers || []).map((t) => ({ ...t, kind: "teacher" }));
const findGroup = (index, name) => allGroups(index).find((g) => norm(g.name) === norm(name) || g.slug === String(name || "").toLowerCase());
const findTeacher = (index, q) => {
  const s = String(q || "").trim();
  if (!s) return null;
  const ns = norm(s);
  return allTeachers(index).find((t) =>
    t.slug === s.toLowerCase()
    || norm(t.name) === ns
    || (t.aliases || []).some((a) => norm(a) === ns)
  ) || null;
};
const findTarget = (index, q) => findGroup(index, q) || findTeacher(index, q);
const searchTeachers = (index, q) => {
  const nq = norm(q);
  if (nq.length < 3) return [];
  return allTeachers(index).filter((t) =>
    norm(t.name).startsWith(nq) || norm(t.name).includes(nq)
    || (t.aliases || []).some((a) => norm(a).startsWith(nq) || norm(a).includes(nq))
  );
};

// підписники (KV)
async function listUsers(env) {
  const users = [];
  let cursor;
  do {
    const r = await env.USERS.list({ prefix: "u:", cursor });
    for (const k of r.keys) if (k.metadata?.g) users.push({ id: k.name.slice(2), ...DEFAULTS, ...k.metadata });
    cursor = r.list_complete ? null : r.cursor;
  } while (cursor);
  return users;
}
async function getUser(env, id) {
  const r = await env.USERS.getWithMetadata("u:" + id);
  return r.metadata ? { ...DEFAULTS, ...r.metadata } : null;
}
const bit = (v) => (v ? 1 : 0);
const saveUser = (env, id, u) => env.USERS.put("u:" + id, "", { metadata: {
  g: u.g, m: bit(u.m), e: bit(u.e), c: bit(u.c), w: bit(u.w), s: bit(u.s), sg: +u.sg || 0, mh: +u.mh || DEFAULTS.mh, eh: +u.eh || DEFAULTS.eh } });
// підгрупа: показуємо спільні пари і пари своєї підгрупи (для викладачів підгруп немає)
const mine = (items, sg) => (sg ? items.filter((x) => !x.subgroup || x.subgroup === sg) : items);
const hasSubgroups = (g) => g?.kind !== "teacher" && Object.values(g.days).some((d) => d.items.some((x) => x.subgroup));

// ---------------------------------------------------------------- тексти
function lessonLine(it, isTeacher = false) {
  const n = NUM[it.pair] || it.pair + ".";
  if (it.cancelled) {
    const grp = isTeacher && it.group_label ? ` (${esc(it.group_label)})` : "";
    const note = it.cancel_note ? esc(it.cancel_note) : "вікно";
    return `${n} <s>${esc(it.subject)}${grp}</s> — <b>${note}</b>`;
  }
  const rm = it.room && !it.online ? (String(it.room).match(/^\d+$/) ? `ауд. ${it.room}` : it.room !== "потік" ? it.room : "") : "";
  const extra = isTeacher
    ? [it.choice ? "за вибором" : "", it.group_label ? `👥 ${esc(it.group_label)}` : "", esc(rm), it.online ? "онлайн" : ""].filter(Boolean)
    : [it.subgroup ? `${it.subgroup} підгр.` : it.choice ? "за вибором" : "", esc(it.teacher || ""), it.online ? "онлайн" : ""].filter(Boolean);
  return `${n} ${it.start}–${it.end}  <b>${esc(((it.type || "") + " " + it.subject).trim())}</b>${it.changed ? " 🔄" : ""}` + (extra.length ? `\n      <i>${extra.join(", ")}</i>` : "");
}

function fmtDay(g, iso, title, sg = 0) {
  const isT = g.kind === "teacher";
  const who = isT ? `🎓 ${esc(g.teacher || g.group)}` : esc(g.group);
  const head = `📅 <b>${title}: ${DAYS[dow(iso)]}, ${ddmm(iso)}</b> · ${who}`;
  const day0 = g.days[iso];
  const day = day0 && { ...day0, items: isT ? day0.items : mine(day0.items, sg) };
  if (!day) return [0, 6].includes(dow(iso)) ? `${head}\n😴 Вихідний` : `${head}\n⚠️ Розклад на цю дату ще не опубліковано`;
  if (!day.items.length) return `${head}\nПар немає 🎉`;
  const live = day.items.filter((x) => !x.cancelled);
  const out = [head, "", ...day.items.map((it) => lessonLine(it, isT))];
  if (day.items.some((x) => x.changed || x.cancelled)) out.push("\n🔄 — заміна з «Змін до розкладу»");
  if (live.length) {
    const nPairs = new Set(live.map((x) => x.pair)).size;
    out.push(`\n🏁 ${live[0].start}–${live[live.length - 1].end} · ${nPairs} ${plural(nPairs)}`);
  }
  return out.join("\n");
}

function fmtWeek(g, monday, sg = 0) {
  const isT = g.kind === "teacher";
  const who = isT ? `🎓 ${esc(g.teacher || g.group)}` : esc(g.group);
  const out = [`🗓 <b>Тиждень ${ddmm(monday)}–${ddmm(addDays(monday, 4))}</b> · ${who}`];
  for (let i = 0; i < 6; i++) {
    const iso = addDays(monday, i);
    const day = g.days[iso];
    if (!day) { if (i < 5) out.push(`<b>${SHORT[dow(iso)]} ${ddmm(iso)}</b>: ⚠️ ще не опубліковано`); continue; }
    const L = (isT ? day.items : mine(day.items, sg)).filter((x) => !x.cancelled);
    if (!L.length) out.push(`<b>${SHORT[dow(iso)]} ${ddmm(iso)}</b>: пар немає 🎉`);
    else out.push(`<b>${SHORT[dow(iso)]} ${ddmm(iso)}</b> · ${L[0].start}–${L[L.length - 1].end}\n` + L.map((x) => `  ${x.pair}. ${esc(x.subject)}${isT && x.group_label ? ` (${esc(x.group_label)})` : ""}${x.changed ? " 🔄" : ""}`).join("\n"));
  }
  return out.join("\n\n");
}

function fmtChanges(g) {
  const isT = g.kind === "teacher";
  const who = isT ? `🎓 ${esc(g.teacher || g.group)}` : esc(g.group);
  const rows = g.changes.filter((z) => z.date >= kyivISO());
  if (!rows.length) return `🔄 <b>Заміни</b> · ${who}\n\nНових замін немає ✅`;
  return `🔄 <b>Заміни</b> · ${who}\n\n` + rows.map((z) => {
    if (isT) {
      if (z.role === "removed") return `• ${ddmm(z.date)} (${SHORT[dow(z.date)]}), ${z.pair} пара · ${esc(z.group)}: <s>${esc(z.subject)}</s> — ${z.to === "вікно" ? "<b>вікно</b>" : "заміна → " + esc(z.to || "")}`;
      return `• ${ddmm(z.date)} (${SHORT[dow(z.date)]}), ${z.pair} пара · ${esc(z.group)}: <b>${esc(z.subject)}</b>`;
    }
    return `• ${ddmm(z.date)} (${SHORT[dow(z.date)]}), ${z.pair} пара: ${z.subject.toLowerCase() === "вікно" ? "<b>вікно</b>" : esc(z.subject + (z.teacher ? ", " + z.teacher : ""))}`;
  }).join("\n");
}

const helpText = (u, chatId, g) => [
  `Я бот розкладу <b>усіх груп і викладачів ПФК НЮУ</b> 👋`,
  u ? (g?.kind === "teacher" ? `🎓 Твій розклад: <b>${esc(g.teacher)}</b>` : `👥 Твоя група: <b>${esc(u.g)}</b>`) : "👥 Спершу обери групу (/group) або викладача (/teacher)",
  `<b>Кнопки внизу</b>\n📅 Сьогодні · ➡️ Завтра · 🗓 Тиждень · 🔄 Заміни`,
  `<b>Сам пишу</b>\n🌅 зранку — пари на сьогодні\n🌆 увечері — пари на завтра\n🗓 у неділю — огляд тижня\n🔔 одразу — нові заміни й новий розклад`,
  `Час, підгрупу й беззвучний режим — у ⚙️ <b>Налаштуваннях</b>.`,
  `Працюю і в груповому чаті: додай мене в чат і надішли /group.`,
  `<code>${chatId}</code>`,
].join("\n\n");

// ---------------------------------------------------------------- клавіатури
function coursesKb(index) {
  const cs = Object.keys(index.courses).sort();
  const rows = [cs.map((c) => ({ text: `${c} курс`, callback_data: "c:" + c }))];
  if (allTeachers(index).length) rows.push([{ text: "🎓 Я викладач", callback_data: "teachers:0" }]);
  return { inline_keyboard: rows };
}
function teachersKb(index, page = 0) {
  const list = allTeachers(index);
  const per = 14;
  const pages = Math.max(1, Math.ceil(list.length / per));
  const p = Math.max(0, Math.min(page, pages - 1));
  const slice = list.slice(p * per, (p + 1) * per);
  const rows = [];
  for (let i = 0; i < slice.length; i += 2) {
    rows.push(slice.slice(i, i + 2).map((t) => ({ text: t.name, callback_data: "g:" + t.slug })));
  }
  if (pages > 1) {
    rows.push([
      { text: p > 0 ? "◀️ Назад" : "·", callback_data: `teachers:${Math.max(0, p - 1)}` },
      { text: `${p + 1}/${pages}`, callback_data: `teachers:${p}` },
      { text: p + 1 < pages ? "Далі ▶️" : "·", callback_data: `teachers:${Math.min(pages - 1, p + 1)}` },
    ]);
  }
  rows.push([{ text: "◀️ До вибору курсу", callback_data: "courses" }]);
  return { inline_keyboard: rows };
}
function groupsKb(index, course) {
  const gs = index.courses[course] || [];
  const rows = [];
  for (let i = 0; i < gs.length; i += 3) rows.push(gs.slice(i, i + 3).map((g) => ({ text: g.name, callback_data: "g:" + g.slug })));
  rows.push([{ text: "◀️ Курси", callback_data: "courses" }]);
  return { inline_keyboard: rows };
}
const schoolDay = (iso, step) => { let d = addDays(iso, step); while ([0, 6].includes(dow(d))) d = addDays(d, step); return d; };
// рядок «Пн 02 · Вт 03 …» — швидкий перехід між днями тижня; обраний день підсвічено
function weekChips(monday, selected, today = kyivISO()) {
  return [0, 1, 2, 3, 4].map((i) => {
    const iso = addDays(monday, i);
    const label = `${SHORT[dow(iso)]} ${iso.slice(8, 10)}${iso === today ? " •" : ""}`;
    return { text: label, callback_data: "d:" + iso, ...(iso === selected ? { style: "primary" } : {}) };
  });
}
function dayKb(env, g, iso, today = kyivISO()) {
  return { inline_keyboard: [
    weekChips(mondayOf(iso), iso, today),
    [{ text: "◀️", callback_data: "d:" + schoolDay(iso, -1) }, { text: iso === today ? "📅 Сьогодні" : "↩️ Сьогодні", callback_data: "d:" + today }, { text: "▶️", callback_data: "d:" + schoolDay(iso, 1) }],
    [{ text: "🗓 Тиждень", callback_data: "w:" + mondayOf(iso) }, { text: "🔄 Заміни", callback_data: "changes" }, { text: "🌐 Сайт", url: siteLink(env, g) }],
  ] };
}
function weekKb(env, g, monday, today = kyivISO()) {
  return { inline_keyboard: [
    weekChips(monday, null, today),
    [{ text: "◀️ Попередній", callback_data: "w:" + addDays(monday, -7) }, { text: "Наступний ▶️", callback_data: "w:" + addDays(monday, 7) }],
    [{ text: "📅 Сьогодні", callback_data: "d:" + today }, { text: "🌐 Сайт", url: siteLink(env, g) }],
  ] };
}
const backKb = (env, g) => ({ inline_keyboard: [[{ text: "📅 Сьогодні", callback_data: "d:" + kyivISO() }, { text: "➡️ Завтра", callback_data: "d:" + schoolDay(kyivISO(), 1) }], [{ text: "🌐 Сайт", url: siteLink(env, g) }]] });
const siteLink = (env, g) => (env.SITE_URL.endsWith("/") ? env.SITE_URL : env.SITE_URL + "/") + (g?.kind === "teacher" || String(g?.slug || "").startsWith("t-") ? "?t=" + encodeURIComponent(g.teacher || g.group) : "?g=" + encodeURIComponent(g.group));
const toggle = (on, label, data) => ({ text: `${on ? "✅" : "▫️"} ${label}`, callback_data: data, ...(on ? { style: "success" } : {}) });
function settingsText(u, g) {
  const isT = g?.kind === "teacher";
  const sub = !isT && g && hasSubgroups(g) ? ` · ${u.sg ? u.sg + " підгрупа" : "усі підгрупи"}` : "";
  const titleLine = isT ? `🎓 <b>${esc(g.teacher || g.group)}</b>` : `👥 <b>${esc(u.g)}</b>${g ? ` · ${g.course} курс` : ""}${sub}`;
  const line = (on, icon, text) => `${on ? "✅" : "▫️"} ${icon} ${text}`;
  return [
    `⚙️ <b>Налаштування</b>`,
    titleLine,
    [
      `<b>Що й коли надсилати</b>`,
      line(u.m, "🌅", `Зранку о ${hh(u.mh)} — пари на сьогодні`),
      line(u.e, "🌆", `Увечері о ${hh(u.eh)} — пари на завтра`),
      line(u.w, "🗓", `У неділю — огляд тижня`),
      line(u.c, "🔔", `Одразу — нові заміни й розклад`),
      `${u.s ? "🔕 Беззвучно" : "🔔 Зі звуком"}`,
    ].join("\n"),
    `<i>Розсилка приходить точно в обрану годину за київським часом.</i>`,
  ].join("\n\n");
}
function settingsKb(u, g) {
  const rows = [[{ text: g?.kind === "teacher" ? "🎓 Змінити викладача / групу" : "👥 Змінити групу", callback_data: "courses" }]];
  if (g && hasSubgroups(g)) rows[0].push({ text: `👤 ${u.sg ? u.sg + " підгрупа" : "Усі підгрупи"}`, callback_data: "s:sg" });
  rows.push(
    [toggle(u.m, "Ранок", "s:m"), { text: `⏰ ${hh(u.mh)}`, callback_data: "t:mh" }],
    [toggle(u.e, "Вечір", "s:e"), { text: `⏰ ${hh(u.eh)}`, callback_data: "t:eh" }],
    [toggle(u.w, "Тиждень у неділю", "s:w"), toggle(u.c, "Заміни", "s:c")],
    [{ text: u.s ? "🔕 Беззвучно" : "🔔 Зі звуком", callback_data: "s:s" }],
    [{ text: "ℹ️ Допомога", callback_data: "help" }, { text: "🚫 Відписатися", callback_data: "stop?", style: "danger" }],
  );
  return { inline_keyboard: rows };
}
function hoursKb(u, key) {
  const hours = key === "mh" ? MORNING_HOURS : EVENING_HOURS;
  const btn = (h) => ({ text: hh(h), callback_data: `h:${key}:${h}`, ...(+u[key] === h ? { style: "primary" } : {}) });
  return { inline_keyboard: [hours.map(btn), [{ text: "◀️ Назад", callback_data: "settings" }]] };
}
const stopKb = { inline_keyboard: [[{ text: "Так, відписатися", callback_data: "stop", style: "danger" }, { text: "◀️ Ні, назад", callback_data: "settings" }]] };

// ---------------------------------------------------------------- Telegram
async function tg(env, method, body) {
  const r = await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
  return r.json().catch(() => ({}));
}
const send = (env, chat_id, text, reply_markup, silent = false) =>
  tg(env, "sendMessage", { chat_id, text, parse_mode: "HTML", reply_markup, ...(silent ? { disable_notification: true } : {}), link_preview_options: { is_disabled: true } });
const edit = (env, msg, text, reply_markup) => tg(env, "editMessageText", { chat_id: msg.chat.id, message_id: msg.message_id, text, parse_mode: "HTML", reply_markup, link_preview_options: { is_disabled: true } });

function keyFromText(t) {
  t = t.toLowerCase().replace(/@\w+/, "").trim();
  if (t.startsWith("/start")) return "start";
  if (t.startsWith("/help") || t.includes("допомог")) return "help";
  if (t.startsWith("/teacher") || t.startsWith("/vykladach") || t.includes("викладач")) return "teacher";
  if (t.startsWith("/group") || t.startsWith("/grupa") || t === "група" || t.includes("змінити групу")) return "group";
  if (t.startsWith("/settings") || t.includes("налаштув")) return "settings";
  if (t.startsWith("/stop")) return "stop";
  if (t.startsWith("/today") || t.includes("сьогодн")) return "today";
  if (t.startsWith("/tomorrow") || t.includes("завтра")) return "tomorrow";
  if (t.startsWith("/week") || t.includes("тижд")) return "week";
  if (t.startsWith("/changes") || t.includes("замін")) return "changes";
  return null;
}

function view(env, g, key, sg = 0) {
  const today = kyivISO();
  if (key.startsWith("d:")) { const iso = key.slice(2); return [fmtDay(g, iso, iso === today ? "Сьогодні" : iso === addDays(today, 1) ? "Завтра" : "День", sg), dayKb(env, g, iso)]; }
  if (key === "today") return [fmtDay(g, today, "Сьогодні", sg), dayKb(env, g, today)];
  if (key === "tomorrow") { const iso = schoolDay(today, 1); return [fmtDay(g, iso, iso === addDays(today, 1) ? "Завтра" : "Наступний навчальний день", sg), dayKb(env, g, iso)]; }
  if (key === "week" || key.startsWith("w:")) {
    const mon = key.startsWith("w:") ? key.slice(2) : [0, 6].includes(dow(today)) ? mondayOf(addDays(today, 2)) : mondayOf(today);
    return [fmtWeek(g, mon, sg), weekKb(env, g, mon)];
  }
  if (key === "changes") return [fmtChanges(g), backKb(env, g)];
  return null;
}

// кнопка «Меню» біля поля вводу відкриває сайт розкладу своєї групи або викладача прямо в Telegram (Mini App)
const setMenuButton = (env, chatId, g) => tg(env, "setChatMenuButton", { chat_id: chatId, menu_button: { type: "web_app", text: "Розклад", web_app: { url: siteLink(env, g) } } });

async function showSettings(env, chatId, site, user, msg) {
  const g = findTarget(site.index, user.g);
  const data = g ? await groupJSON(env, site, g.slug).catch(() => null) : null;
  const text = settingsText(user, data || g), kb = settingsKb(user, data || g);
  return msg ? edit(env, msg, text, kb) : send(env, chatId, text, kb);
}

async function chooseGroup(env, chatId, site, user, slugOrName, msg) {
  const g = findTarget(site.index, slugOrName);
  if (!g) return false;
  const isT = g.kind === "teacher";
  const keyVal = isT ? g.slug : g.name;
  const u = { ...DEFAULTS, ...(user || {}), g: keyVal };
  if (user && user.g !== keyVal) u.sg = 0; // нова група/режим — підгрупа скидається
  await saveUser(env, chatId, u);
  const data = await groupJSON(env, site, g.slug);
  if (chatId > 0) await setMenuButton(env, chatId, data);
  const text = isT
    ? `✅ Обрано розклад викладача: 🎓 <b>${esc(g.name)}</b>\n\nТепер я надсилатиму твій зведений розклад по всіх курсах і заміни. Кнопки внизу — щоб подивитися будь-коли.`
    : `✅ Твоя група: <b>${esc(g.name)}</b> (${g.course} курс)\n\nТепер я надсилатиму її розклад сам. Кнопки внизу — щоб подивитися будь-коли.`;
  if (msg) await edit(env, msg, text); else await send(env, chatId, text, MENU);
  if (msg) await send(env, chatId, "Меню 👇", MENU);
  const [t, kb] = view(env, data, "today", u.sg);
  await send(env, chatId, t, kb);
  return true;
}

async function onUpdate(env, upd) {
  if (upd.my_chat_member) { // бота заблокували/видалили з чату — прибираємо підписку
    const st = upd.my_chat_member.new_chat_member?.status;
    if (st === "kicked" || st === "left") await env.USERS.delete("u:" + upd.my_chat_member.chat.id);
    return;
  }
  const cb = upd.callback_query;
  const msg = upd.message;
  const chatId = cb ? cb.message.chat.id : msg?.chat.id;
  if (!chatId || (!cb && !msg.text)) return;
  const [site, user] = await Promise.all([loadSite(env), getUser(env, chatId)]);
  const index = site.index;
  const groupData = async (u) => { const g = u && findTarget(index, u.g); return g ? groupJSON(env, site, g.slug) : null; };

  if (cb) {
    const d = cb.data || "";
    if (d.startsWith("rs:")) {
      const hb = await env.USERS.get("hb", "json").catch(() => null);
      const admins = new Set([
        ...String(env.TELEGRAM_CHAT_ID || "").split(",").map((s) => s.trim()).filter(Boolean),
        ...((hb?.admins || []).map(String)),
      ]);
      const fromId = String(cb.from?.id || "");
      if (admins.size && !admins.has(String(chatId)) && !admins.has(fromId)) {
        return tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Лише адмін може відповісти", show_alert: true });
      }
      const [, did, choice] = d.split(":");
      const doubt = did ? await env.USERS.get("s:doubt:" + did, "json").catch(() => null) : null;
      const opt = doubt?.options?.[+choice];
      const keep = choice === "all" ? "all" : opt?.subject ? [((opt.type || "") + " " + opt.subject).trim()] : null;
      if (!did || !keep) {
        return tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Питання застаріло або варіант не знайдено" });
      }
      const res = (await env.USERS.get("s:resolutions", "json").catch(() => null)) || {};
      const label = keep === "all" ? "Обидва (підгрупи)" : keep.join(", ");
      res[did] = { keep, by: fromId || String(chatId), at: new Date().toISOString() };
      await env.USERS.put("s:resolutions", JSON.stringify(res), { expirationTtl: 180 * 86400 });
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id, text: "Прийнято" });
      const orig = esc(cb.message?.text || "Питання щодо розкладу");
      await edit(env, cb.message, `${orig}\n\n✅ <b>Прийнято:</b> ${esc(label)}`);
      const repo = hb?.repo || env.GITHUB_REPO || "";
      if (env.GH_DISPATCH_TOKEN && repo) {
        await fetch(`https://api.github.com/repos/${repo}/actions/workflows/rozklad.yml/dispatches`, {
          method: "POST",
          headers: { Authorization: "Bearer " + env.GH_DISPATCH_TOKEN, Accept: "application/vnd.github+json", "User-Agent": "pfk-rozklad-worker", "content-type": "application/json" },
          body: JSON.stringify({ ref: "main", inputs: { mode: "auto" } }),
        }).catch(() => {});
      }
      return;
    }
    tg(env, "answerCallbackQuery", { callback_query_id: cb.id });
    if (d === "courses") return edit(env, cb.message, "Обери свій курс або режим викладача 👇", coursesKb(index));
    if (d.startsWith("teachers:")) {
      const page = +d.split(":")[1] || 0;
      return edit(env, cb.message, "🎓 Обери викладача (або напиши прізвище в чат) 👇", teachersKb(index, page));
    }
    if (d.startsWith("c:")) return edit(env, cb.message, `${d.slice(2)} курс — обери групу 👇`, groupsKb(index, d.slice(2)));
    if (d.startsWith("g:")) return chooseGroup(env, chatId, site, user, d.slice(2), cb.message);
    if (d === "help") {
      const g = await groupData(user).catch(() => null);
      return edit(env, cb.message, helpText(user, chatId, g), { inline_keyboard: [[{ text: "⚙️ Налаштування", callback_data: "settings" }]] });
    }
    if (d === "stop?") return edit(env, cb.message, "Точно відписатися? Я перестану надсилати розклад.", stopKb);
    if (d === "stop") { await env.USERS.delete("u:" + chatId); return edit(env, cb.message, "Готово, я більше не писатиму. Повернутися — /start"); }
    if (["settings", "s:", "t:", "h:"].some((p) => d.startsWith(p))) {
      if (!user) return edit(env, cb.message, "Спершу обери групу 👇", coursesKb(index));
      if (d.startsWith("t:")) return edit(env, cb.message, d === "t:mh" ? "🌅 О котрій надсилати пари на сьогодні?" : "🌆 О котрій надсилати пари на завтра?", hoursKb(user, d.slice(2)));
      if (d.startsWith("h:")) { const [, k, h] = d.split(":"); if (["mh", "eh"].includes(k)) { user[k] = +h; user[k === "mh" ? "m" : "e"] = 1; } }
      else if (d === "s:sg") user.sg = (user.sg + 1) % 3;
      else if (d.startsWith("s:")) { const k = d.slice(2); if (["m", "e", "c", "w", "s"].includes(k)) user[k] = user[k] ? 0 : 1; }
      if (d !== "settings") await saveUser(env, chatId, user);
      return showSettings(env, chatId, site, user, cb.message);
    }
    const g = await groupData(user);
    if (!g) return edit(env, cb.message, "Спершу обери групу 👇", coursesKb(index));
    const r = view(env, g, d, user.sg);
    if (r) await edit(env, cb.message, r[0], r[1]);
    return;
  }

  const text = msg.text.trim();
  const key = keyFromText(text);
  if (key === "start") {
    const payload = text.split(/\s+/)[1]; // t.me/<бот>?start=sob-11 або t-... — одразу обрати групу/викладача
    if (payload && (await chooseGroup(env, chatId, site, user, payload))) return;
    if (!user) return send(env, chatId, "Привіт! Я бот розкладу ПФК НЮУ 👋\nОбери курс або режим викладача 👇", coursesKb(index));
    await send(env, chatId, helpText(user, chatId, await groupData(user).catch(() => null)), MENU);
    return;
  }
  if (key === "help") return send(env, chatId, helpText(user, chatId, await groupData(user).catch(() => null)), MENU);
  if (key === "group") return send(env, chatId, "Обери свій курс або режим викладача 👇", coursesKb(index));
  if (key === "teacher") return send(env, chatId, "🎓 Обери викладача (або просто напиши прізвище в чат) 👇", teachersKb(index, 0));
  if (key === "stop") { await env.USERS.delete("u:" + chatId); return send(env, chatId, "Готово, я більше не писатиму. Повернутися — /start", { remove_keyboard: true }); }
  if (key === "settings") return user ? showSettings(env, chatId, site, user) : send(env, chatId, "Спершу обери групу або викладача 👇", coursesKb(index));
  if (key) {
    const g = await groupData(user);
    if (!g) return send(env, chatId, "Спершу обери групу або викладача 👇", coursesKb(index));
    const [t, kb] = view(env, g, key, user.sg);
    return send(env, chatId, t, kb);
  }
  // просто написали назву групи («соб-11») або прізвище викладача («Губанов»)
  if (findGroup(index, text) || findTeacher(index, text)) return chooseGroup(env, chatId, site, user, text);
  const tMatches = searchTeachers(index, text);
  if (tMatches.length === 1) return chooseGroup(env, chatId, site, user, tMatches[0].slug);
  if (tMatches.length > 1 && tMatches.length <= 8) {
    return send(env, chatId, "🎓 Знайшов кількох викладачів — обери потрібного 👇", {
      inline_keyboard: tMatches.map((m) => [{ text: m.name, callback_data: "g:" + m.slug }]),
    });
  }
  if (msg.chat.type === "private") return send(env, chatId, "Не зрозумів 🤔 Натисни кнопку внизу або напиши назву групи (напр. СОБ-11) чи прізвище викладача.", MENU);
}

// ---------------------------------------------------------------- API для GitHub Actions
async function api(env, path, request) {
  if (path === "/api/users") return { users: await listUsers(env) };
  const body = request.method === "POST" ? await request.json() : {};
  if (path === "/api/once") {
    const k = "o:" + body.key;
    if (await env.USERS.get(k)) return { first: false };
    await env.USERS.put(k, "1", { expirationTtl: 3 * 86400 });
    return { first: true };
  }
  if (path === "/api/add") {
    if (!(await getUser(env, body.id))) await saveUser(env, body.id, { ...DEFAULTS, g: body.g });
    return { ok: true };
  }
  if (path === "/api/drop") {
    await Promise.all((body.ids || []).map((id) => env.USERS.delete("u:" + id)));
    return { ok: true };
  }
  // стан розсилок: bot.py читає ДО відправки і записує ПІСЛЯ (хто отримав / кому повторити)
  if (path === "/api/state/get") {
    const keys = (body.keys || []).slice(0, 50);
    const vals = await Promise.all(keys.map((k) => env.USERS.get("s:" + k, "json")));
    return { values: Object.fromEntries(keys.map((k, i) => [k, vals[i]])) };
  }
  if (path === "/api/state/set") {
    const ttl = Math.max(60, Math.min(+body.ttl || 3 * 86400, 30 * 86400));
    await env.USERS.put("s:" + body.key, JSON.stringify(body.value ?? {}), { expirationTtl: ttl });
    return { ok: true };
  }
  // група стала супергрупою (новий chat id) — переносимо підписку з усіма налаштуваннями
  if (path === "/api/move") {
    const u = await getUser(env, body.from);
    if (u && !(await getUser(env, body.to))) await saveUser(env, body.to, u);
    if (u) await env.USERS.delete("u:" + body.from);
    return { ok: true, moved: !!u };
  }
  // «запуск GitHub Actions відбувся» — для зовнішнього контролю в scheduled()
  if (path === "/api/heartbeat") {
    await env.USERS.put("hb", JSON.stringify({ ts: Date.now(), admins: body.admins || [], degraded: !!body.degraded, run: body.run || "", repo: body.repo || "" }));
    return { ok: true };
  }
  return null;
}

// ---------------------------------------------------------------- планові розсилки й зовнішній контроль (Cron Trigger воркера)
// Воркер запускається щохвилини з :00 до :15 щогодини (cron "0-15 * * * *"):
// - рівно о :00 розсилає ранок (06–09) та вечір / недільний тиждень (17–21) за київським часом;
// - не більше MAX_SENDS_PER_TICK (35) Telegram-запитів за один запуск, щоб не впиратися в ліміт 50 subrequests безкоштовного тарифу;
// - якщо підписників більше — решта досилається частинами на :01..:15 через один ключ стану `s:<name>:<date>:<slot>`;
// - GitHub Actions (bot.py) використовує той самий ключ стану, тож дублів немає, а заміни й оголошення приходять з Actions.
const kyivParts = (ms = Date.now()) => {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-GB", {
      timeZone: "Europe/Kyiv",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(new Date(ms)).map((p) => [p.type, p.value])
  );
  const hour = +parts.hour === 24 ? 0 : +parts.hour;
  return { iso: `${parts.year}-${parts.month}-${parts.day}`, hour, minute: +parts.minute };
};
const kyivTime = (ms) => new Intl.DateTimeFormat("uk-UA", { timeZone: "Europe/Kyiv", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }).format(new Date(ms));

async function loadSiteForCron(env) {
  const bundle = await env.USERS.get("s:site:bundle", "json").catch(() => null);
  if (bundle?.index) {
    const groups = bundle.groups || {};
    return { index: bundle.index, getGroup: async (slug) => groups[slug] || null };
  }
  const site = await loadSite(env);
  const cache = new Map();
  return {
    index: site.index,
    getGroup: (slug) => {
      if (!cache.has(slug)) cache.set(slug, groupJSON(env, site, slug).catch(() => null));
      return cache.get(slug);
    },
  };
}

function buildDigestMessage(env, g, u, kind, today, tomorrow, nextMon, staleNote) {
  if (!g) return null;
  const isT = g.kind === "teacher";
  const sg = +u.sg || 0;
  if (kind === "morning") {
    const day = g.days?.[today];
    if (!u.m || !day) return null;
    if (isT && !day.items?.some((x) => !x.cancelled || x.changed)) return null;
    return [fmtDay(g, today, "Сьогодні", sg) + staleNote, dayKb(env, g, today, today)];
  }
  const d = dow(today);
  if (d === 0) {
    const hasNextWeek = [0, 1, 2, 3, 4, 5].some((i) => {
      const dd = g.days?.[addDays(nextMon, i)];
      return isT ? !!dd?.items?.length : !!dd;
    });
    if (u.w && hasNextWeek) return [fmtWeek(g, nextMon, sg) + staleNote, weekKb(env, g, nextMon, today)];
    if (u.w) return null;
  }
  const tm = g.days?.[tomorrow];
  if (d !== 6 && u.e && tm) {
    if (isT && !tm.items?.some((x) => !x.cancelled || x.changed)) return null;
    return [fmtDay(g, tomorrow, "Завтра", sg) + staleNote, dayKb(env, g, tomorrow, today)];
  }
  return null;
}

async function deliverChunk(env, batch, budget) {
  const failed = [], rateLimited = [];
  let hit429 = false;
  for (let i = 0; i < batch.length; i += MICRO_BATCH) {
    if (hit429) {
      rateLimited.push(...batch.slice(i));
      break;
    }
    if (i > 0 && !env.NO_DELAY) await sleep(100);
    const chunk = batch.slice(i, i + MICRO_BATCH);
    await Promise.all(chunk.map(async (item) => {
      const { u, text, kb } = item;
      const silent = !!u.s;
      let r = null;
      try {
        budget.left--;
        r = await send(env, u.id, text, kb, silent);
      } catch {
        failed.push(String(u.id));
        return;
      }
      const newId = r?.parameters?.migrate_to_chat_id;
      if (!r?.ok && newId) {
        const meta = { g: u.g, m: bit(u.m), e: bit(u.e), c: bit(u.c), w: bit(u.w), s: bit(u.s), sg: +u.sg || 0, mh: +u.mh || DEFAULTS.mh, eh: +u.eh || DEFAULTS.eh };
        if (!(await getUser(env, newId))) await saveUser(env, newId, meta);
        await env.USERS.delete("u:" + u.id);
        if (budget.left > 0) {
          try {
            budget.left--;
            const r2 = await send(env, newId, text, kb, silent);
            if (r2?.ok) return;
            if (r2?.error_code === 429) { hit429 = true; rateLimited.push({ ...item, u: { ...u, id: String(newId) } }); return; }
          } catch { /* fall through to failed */ }
        }
        failed.push(String(newId));
        return;
      }
      if (r?.ok) return;
      const code = r?.error_code, desc = String(r?.description || "");
      if ((code === 400 || code === 403) && PERM_DROP_RE.test(desc)) {
        await env.USERS.delete("u:" + u.id);
      } else if (code === 429) {
        hit429 = true;
        rateLimited.push(item);
      } else if (code !== 400) {
        failed.push(String(u.id));
      }
    }));
  }
  return { failed, rateLimited, hit429 };
}

async function runScheduledDigests(env, nowMs = Date.now()) {
  const { iso: today, hour: h } = kyivParts(nowMs);
  const kind = h >= 6 && h < MORNING_UNTIL ? "morning" : h >= 17 && h < EVENING_UNTIL ? "evening" : null;
  if (!kind) return { skipped: "off-hours" };
  const users = await listUsers(env);
  if (!users.length) return { users: 0 };

  const hourKey = kind === "morning" ? "mh" : "eh";
  const slots = [...new Set(users.map((u) => +u[hourKey] || DEFAULTS[hourKey]))].filter((s) => s <= h).sort((a, b) => a - b);
  if (!slots.length) return { skipped: "no-due-slots" };

  const tomorrow = addDays(today, 1);
  const nextMon = addDays(today, dow(today) === 0 ? 1 : 8 - dow(today));
  const budget = { left: MAX_SENDS_PER_TICK };
  let site = null;
  const out = {};

  for (const slot of slots) {
    if (budget.left <= 0) break;
    const key = `${kind}:${today}:${slot}`;
    const st = await env.USERS.get("s:" + key, "json").catch(() => null);
    const pendingIds = Array.isArray(st?.pending) ? st.pending.map(String) : [];
    const retryIds = Array.isArray(st?.failed) ? st.failed.map(String) : [];
    const tries = st ? (+st.tries || (pendingIds.length ? 0 : 1)) : 0;

    if (st && !pendingIds.length && (!retryIds.length || tries >= SEND_TRIES)) continue;

    if (!site) site = await loadSiteForCron(env);
    const downTs = site.index?.source_down_since ? Date.parse(site.index.source_down_since) : NaN;
    const staleNote = Number.isFinite(downTs)
      ? `\n\n<i>⚠️ Сайт коледжу зараз недоступний (з ${kyivTime(downTs)}), це останній отриманий розклад.</i>`
      : "";

    let slotUsers = users.filter((u) => (+u[hourKey] || DEFAULTS[hourKey]) === slot);
    let prevFailed = [];
    if (pendingIds.length) {
      const want = new Set(pendingIds);
      slotUsers = slotUsers.filter((u) => want.has(String(u.id)));
      prevFailed = retryIds;
    } else if (st) {
      const want = new Set(retryIds);
      slotUsers = slotUsers.filter((u) => want.has(String(u.id)));
    }

    const queue = [];
    for (const u of slotUsers) {
      const metaG = findTarget(site.index, u.g);
      const g = metaG ? await site.getGroup(metaG.slug) : null;
      const msg = buildDigestMessage(env, g, u, kind, today, tomorrow, nextMon, staleNote);
      if (msg) queue.push({ u, text: msg[0], kb: msg[1] });
    }
    if (!queue.length && !st) continue;

    const batch = queue.slice(0, budget.left);
    const unstarted = queue.slice(budget.left);
    const res = batch.length ? await deliverChunk(env, batch, budget) : { failed: [], rateLimited: [], hit429: false };
    const nextPending = [...res.rateLimited, ...unstarted].map((x) => String(x.u.id));
    const nextFailed = [...new Set([...prevFailed, ...res.failed])];
    const nextTries = nextPending.length ? tries : tries + 1;

    await env.USERS.put("s:" + key, JSON.stringify({
      failed: nextFailed,
      ...(nextPending.length ? { pending: nextPending } : {}),
      tries: nextTries,
      at: new Date(nowMs).toISOString(),
      by: "worker",
    }), { expirationTtl: 3 * 86400 });

    out[key] = { sent: batch.length - res.failed.length - res.rateLimited.length, pending: nextPending.length, failed: nextFailed.length };
    if (res.hit429) break;
  }
  return out;
}

async function scheduled(env, nowMs = Date.now()) {
  const digest = await runScheduledDigests(env, nowMs).catch((e) => ({ error: String(e?.message || e) }));
  const { hour: h, minute } = kyivParts(nowMs);
  if (minute > 0 && minute < 15) return { digest };
  const hb = await env.USERS.get("hb", "json");
  if (!hb) return { digest, skipped: "no heartbeat yet" };
  const ageH = (nowMs - hb.ts) / 3600e3;
  const res = { digest, ageH: Math.round(ageH * 10) / 10 };
  const repo = hb.repo || env.GITHUB_REPO || "";
  if (minute >= 15 && env.GH_DISPATCH_TOKEN && repo && h >= 6 && h <= 22 && ageH >= 0.75) {
    const r = await fetch(`https://api.github.com/repos/${repo}/actions/workflows/rozklad.yml/dispatches`, {
      method: "POST",
      headers: { Authorization: "Bearer " + env.GH_DISPATCH_TOKEN, Accept: "application/vnd.github+json", "User-Agent": "pfk-rozklad-worker", "content-type": "application/json" },
      body: JSON.stringify({ ref: "main", inputs: { mode: "auto" } }),
    });
    res.dispatched = r.status;
  }
  const limit = +env.WATCHDOG_HOURS || 4;
  const wd = await env.USERS.get("wd", "json");
  const tell = (text) => Promise.all((hb.admins || []).map((c) => send(env, c, text)));
  const actions = repo ? `https://github.com/${repo}/actions` : "";
  if (ageH > limit && h >= 9 && h <= 22) {
    if (!wd || nowMs - wd.alerted > 12 * 3600e3) {
      await tell(`⏰ <b>Розклад ПФК: GitHub Actions не запускався вже ~${Math.round(ageH)} год</b>\nОстанній запуск: ${kyivTime(hb.ts)}.\nРозклад не оновлюється, ранкові/вечірні розсилки можуть не прийти.${actions ? "\n" + actions : ""}`);
      await env.USERS.put("wd", JSON.stringify({ alerted: nowMs, since: wd?.since || hb.ts }));
      res.alerted = true;
    }
  } else if (wd && ageH <= limit) {
    await tell(`✅ <b>Розклад ПФК: запуски GitHub Actions відновилися</b> (перерва з ${kyivTime(wd.since)}).`);
    await env.USERS.delete("wd");
    res.recovered = true;
  }
  return res;
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(scheduled(env, event.scheduledTime).then((r) => console.log("watchdog", JSON.stringify(r))));
  },
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const secret = await secretOf(env.BOT_TOKEN);
    if (url.pathname.startsWith("/api/")) {
      if (request.headers.get("Authorization") !== "Bearer " + secret) return new Response("forbidden", { status: 403 });
      const r = await api(env, url.pathname, request);
      return r ? Response.json(r) : new Response("not found", { status: 404 });
    }
    if (request.method === "GET" && url.pathname.startsWith("/data/")) {
      const cors = { "Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=120" };
      try {
        const site = await loadSite(env);
        if (url.pathname === "/data/index.json") return Response.json(site.index, { headers: cors });
        const m = url.pathname.match(/^\/data\/([gt])\/([a-z0-9-]+)\.json$/);
        if (m) {
          const g = await groupJSON(env, site, m[2]).catch(() => null);
          return g ? Response.json(g, { headers: cors }) : new Response("not found", { status: 404, headers: cors });
        }
      } catch {
        return new Response("unavailable", { status: 503, headers: cors });
      }
    }
    if (request.method !== "POST") return new Response("pfk-rozklad bot OK");
    if (request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== secret) return new Response("forbidden", { status: 403 });
    const upd = await request.json();
    ctx.waitUntil(onUpdate(env, upd).catch(async (e) => {
      console.error(e);
      const chatId = upd.callback_query?.message.chat.id || upd.message?.chat.id;
      if (chatId) await send(env, chatId, "⚠️ Не вдалося отримати розклад. Спробуй за хвилину.");
    }));
    return new Response("ok");
  },
};
