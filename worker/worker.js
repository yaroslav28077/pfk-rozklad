// Cloudflare Worker: Telegram-бот розкладу для всіх груп ПФК НЮУ (безкоштовний тариф).
// Розгортає Cloudflare Workers Builds при кожному пуші в main; прив'язки — у wrangler.json (KV створюється сам):
//   BOT_TOKEN (secret, задається в Cloudflare: Settings → Variables and Secrets) — токен бота; SITE_URL (var) — адреса сайту на GitHub Pages; USERS (KV) — підписники.
// Підписник = ключ "u:<chat id>" у KV, у metadata: { g: "СОБ-11", m: 1 (ранок), e: 1 (вечір), c: 1 (заміни) }.

const DAYS = ["Неділя", "Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота"];
const SHORT = ["Нд", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];
const NUM = ["0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"];
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const dow = (iso) => new Date(iso + "T12:00:00Z").getUTCDay();
const ddmm = (iso) => iso.slice(8, 10) + "." + iso.slice(5, 7);
const addDays = (iso, n) => { const d = new Date(iso + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
const kyivISO = () => new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Kyiv", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date());
const mondayOf = (iso) => addDays(iso, dow(iso) === 0 ? -6 : 1 - dow(iso));
const norm = (s) => String(s || "").toUpperCase().replace(/[\s\-–'’ʼ]/g, "").replace(/I/g, "І").replace(/C/g, "С").replace(/O/g, "О").replace(/P/g, "Р").replace(/K/g, "К").replace(/A/g, "А").replace(/E/g, "Е");
const plural = (n) => (n % 10 === 1 && n % 100 !== 11 ? "пара" : n % 10 >= 2 && n % 10 <= 4 && !(n % 100 >= 12 && n % 100 <= 14) ? "пари" : "пар");
// m/e/c/w — ранок/вечір/заміни/огляд тижня (1/0); mh/eh — година ранкової/вечірньої розсилки; s — беззвучно; sg — підгрупа (0 = усі)
const DEFAULTS = { m: 1, e: 1, c: 1, w: 1, s: 0, sg: 0, mh: 6, eh: 18 };
const MORNING_HOURS = [6, 7, 8, 9];
const EVENING_HOURS = [17, 18, 19, 20, 21];
const hh = (h) => String(h).padStart(2, "0") + ":00";

const MENU = {
  keyboard: [
    [{ text: "📅 Сьогодні", style: "primary" }, { text: "➡️ Завтра", style: "primary" }],
    [{ text: "🗓 Тиждень" }, { text: "🔄 Заміни" }],
    [{ text: "⚙️ Налаштування" }],
  ],
  resize_keyboard: true, is_persistent: true, input_field_placeholder: "Обери кнопку або напиши назву групи",
};

async function secretOf(token) {
  const h = await crypto.subtle.digest("SHA-256", new TextEncoder().encode("pfk-rozklad:" + token));
  return [...new Uint8Array(h)].map((b) => b.toString(16).padStart(2, "0")).join("").slice(0, 48);
}

// ---------------------------------------------------------------- дані з GitHub Pages
async function getJSON(env, path) {
  const base = env.SITE_URL.endsWith("/") ? env.SITE_URL : env.SITE_URL + "/";
  const r = await fetch(base + path, { cf: { cacheTtl: 120, cacheEverything: true } });
  if (!r.ok) throw new Error(path + ": " + r.status);
  return r.json();
}
const allGroups = (index) => Object.entries(index.courses).flatMap(([c, gs]) => gs.map((g) => ({ ...g, course: +c })));
const findGroup = (index, name) => allGroups(index).find((g) => norm(g.name) === norm(name) || g.slug === String(name || "").toLowerCase());

// ---------------------------------------------------------------- підписники (KV)
async function getUser(env, id) {
  const r = await env.USERS.getWithMetadata("u:" + id);
  return r.metadata ? { ...DEFAULTS, ...r.metadata } : null;
}
const bit = (v) => (v ? 1 : 0);
const saveUser = (env, id, u) => env.USERS.put("u:" + id, "", { metadata: {
  g: u.g, m: bit(u.m), e: bit(u.e), c: bit(u.c), w: bit(u.w), s: bit(u.s), sg: +u.sg || 0, mh: +u.mh || DEFAULTS.mh, eh: +u.eh || DEFAULTS.eh } });
// підгрупа: показуємо спільні пари і пари своєї підгрупи
const mine = (items, sg) => (sg ? items.filter((x) => !x.subgroup || x.subgroup === sg) : items);
const hasSubgroups = (g) => Object.values(g.days).some((d) => d.items.some((x) => x.subgroup));

// ---------------------------------------------------------------- тексти
function lessonLine(it) {
  const n = NUM[it.pair] || it.pair + ".";
  if (it.cancelled) return `${n} <s>${esc(it.subject)}</s> — <b>вікно</b>`;
  const extra = [it.subgroup ? `${it.subgroup} підгр.` : "", esc(it.teacher || ""), it.online ? "онлайн" : ""].filter(Boolean);
  return `${n} ${it.start}–${it.end}  <b>${esc(((it.type || "") + " " + it.subject).trim())}</b>${it.changed ? " 🔄" : ""}` + (extra.length ? `\n      <i>${extra.join(", ")}</i>` : "");
}

function fmtDay(g, iso, title, sg = 0) {
  const head = `📅 <b>${title}: ${DAYS[dow(iso)]}, ${ddmm(iso)}</b> · ${esc(g.group)}`;
  const day0 = g.days[iso];
  const day = day0 && { ...day0, items: mine(day0.items, sg) };
  if (!day) return [0, 6].includes(dow(iso)) ? `${head}\n😴 Вихідний` : `${head}\n⚠️ Розклад на цю дату ще не опубліковано`;
  if (!day.items.length) return `${head}\nПар немає 🎉`;
  const live = day.items.filter((x) => !x.cancelled);
  const out = [head, "", ...day.items.map(lessonLine)];
  if (day.items.some((x) => x.changed || x.cancelled)) out.push("\n🔄 — заміна з «Змін до розкладу»");
  if (live.length) out.push(`\n🏁 ${live[0].start}–${live[live.length - 1].end} · ${live.length} ${plural(live.length)}`);
  return out.join("\n");
}

function fmtWeek(g, monday, sg = 0) {
  const out = [`🗓 <b>Тиждень ${ddmm(monday)}–${ddmm(addDays(monday, 4))}</b> · ${esc(g.group)}`];
  for (let i = 0; i < 6; i++) {
    const iso = addDays(monday, i);
    const day = g.days[iso];
    if (!day) { if (i < 5) out.push(`<b>${SHORT[dow(iso)]} ${ddmm(iso)}</b>: ⚠️ ще не опубліковано`); continue; }
    const L = mine(day.items, sg).filter((x) => !x.cancelled);
    if (!L.length) out.push(`<b>${SHORT[dow(iso)]} ${ddmm(iso)}</b>: пар немає 🎉`);
    else out.push(`<b>${SHORT[dow(iso)]} ${ddmm(iso)}</b> · ${L[0].start}–${L[L.length - 1].end}\n` + L.map((x) => `  ${x.pair}. ${esc(x.subject)}${x.changed ? " 🔄" : ""}`).join("\n"));
  }
  return out.join("\n\n");
}

function fmtChanges(g) {
  const rows = g.changes.filter((z) => z.date >= kyivISO());
  if (!rows.length) return `🔄 <b>Заміни</b> · ${esc(g.group)}\n\nНових замін немає ✅`;
  return `🔄 <b>Заміни</b> · ${esc(g.group)}\n\n` + rows.map((z) => `• ${ddmm(z.date)} (${SHORT[dow(z.date)]}), ${z.pair} пара: ${z.subject.toLowerCase() === "вікно" ? "<b>вікно</b>" : esc(z.subject + (z.teacher ? ", " + z.teacher : ""))}`).join("\n");
}

const helpText = (u, chatId) => [
  `Я бот розкладу <b>усіх груп ПФК НЮУ</b> 👋`,
  u ? `👥 Твоя група: <b>${esc(u.g)}</b>` : "👥 Спершу обери групу — /group",
  `<b>Кнопки внизу</b>\n📅 Сьогодні · ➡️ Завтра · 🗓 Тиждень · 🔄 Заміни`,
  `<b>Сам пишу</b>\n🌅 зранку — пари на сьогодні\n🌆 увечері — пари на завтра\n🗓 у неділю — огляд тижня\n🔔 одразу — нові заміни й новий розклад`,
  `Час, підгрупу й беззвучний режим — у ⚙️ <b>Налаштуваннях</b>.`,
  `Працюю і в груповому чаті: додай мене в чат і надішли /group.`,
  `<code>${chatId}</code>`,
].join("\n\n");

// ---------------------------------------------------------------- клавіатури
function coursesKb(index) {
  const cs = Object.keys(index.courses).sort();
  return { inline_keyboard: [cs.map((c) => ({ text: `${c} курс`, callback_data: "c:" + c }))] };
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
function weekChips(monday, selected) {
  const today = kyivISO();
  return [0, 1, 2, 3, 4].map((i) => {
    const iso = addDays(monday, i);
    const label = `${SHORT[dow(iso)]} ${iso.slice(8, 10)}${iso === today ? " •" : ""}`;
    return { text: label, callback_data: "d:" + iso, ...(iso === selected ? { style: "primary" } : {}) };
  });
}
function dayKb(env, g, iso) {
  const today = kyivISO();
  return { inline_keyboard: [
    weekChips(mondayOf(iso), iso),
    [{ text: "◀️", callback_data: "d:" + schoolDay(iso, -1) }, { text: iso === today ? "📅 Сьогодні" : "↩️ Сьогодні", callback_data: "d:" + today }, { text: "▶️", callback_data: "d:" + schoolDay(iso, 1) }],
    [{ text: "🗓 Тиждень", callback_data: "w:" + mondayOf(iso) }, { text: "🔄 Заміни", callback_data: "changes" }, { text: "🌐 Сайт", url: siteLink(env, g) }],
  ] };
}
function weekKb(env, g, monday) {
  return { inline_keyboard: [
    weekChips(monday, null),
    [{ text: "◀️ Попередній", callback_data: "w:" + addDays(monday, -7) }, { text: "Наступний ▶️", callback_data: "w:" + addDays(monday, 7) }],
    [{ text: "📅 Сьогодні", callback_data: "d:" + kyivISO() }, { text: "🌐 Сайт", url: siteLink(env, g) }],
  ] };
}
const backKb = (env, g) => ({ inline_keyboard: [[{ text: "📅 Сьогодні", callback_data: "d:" + kyivISO() }, { text: "➡️ Завтра", callback_data: "d:" + schoolDay(kyivISO(), 1) }], [{ text: "🌐 Сайт", url: siteLink(env, g) }]] });
const siteLink = (env, g) => (env.SITE_URL.endsWith("/") ? env.SITE_URL : env.SITE_URL + "/") + "?g=" + encodeURIComponent(g.group);
const toggle = (on, label, data) => ({ text: `${on ? "✅" : "▫️"} ${label}`, callback_data: data, ...(on ? { style: "success" } : {}) });
function settingsText(u, g) {
  const sub = g && hasSubgroups(g) ? ` · ${u.sg ? u.sg + " підгрупа" : "усі підгрупи"}` : "";
  const line = (on, icon, text) => `${on ? "✅" : "▫️"} ${icon} ${text}`;
  return [
    `⚙️ <b>Налаштування</b>`,
    `👥 <b>${esc(u.g)}</b>${g ? ` · ${g.course} курс` : ""}${sub}`,
    [
      `<b>Що й коли надсилати</b>`,
      line(u.m, "🌅", `Зранку о ${hh(u.mh)} — пари на сьогодні`),
      line(u.e, "🌆", `Увечері о ${hh(u.eh)} — пари на завтра`),
      line(u.w, "🗓", `У неділю — огляд тижня`),
      line(u.c, "🔔", `Одразу — нові заміни й розклад`),
      `${u.s ? "🔕 Беззвучно" : "🔔 Зі звуком"}`,
    ].join("\n"),
    `<i>Бот запускається щогодини, тож повідомлення може прийти на кілька хвилин пізніше.</i>`,
  ].join("\n\n");
}
function settingsKb(u, g) {
  const rows = [[{ text: "👥 Змінити групу", callback_data: "courses" }]];
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
const send = (env, chat_id, text, reply_markup) => tg(env, "sendMessage", { chat_id, text, parse_mode: "HTML", reply_markup, link_preview_options: { is_disabled: true } });
const edit = (env, msg, text, reply_markup) => tg(env, "editMessageText", { chat_id: msg.chat.id, message_id: msg.message_id, text, parse_mode: "HTML", reply_markup, link_preview_options: { is_disabled: true } });

function keyFromText(t) {
  t = t.toLowerCase().replace(/@\w+/, "").trim();
  if (t.startsWith("/start")) return "start";
  if (t.startsWith("/help") || t.includes("допомог")) return "help";
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

// кнопка «Меню» біля поля вводу відкриває сайт розкладу своєї групи прямо в Telegram (Mini App)
const setMenuButton = (env, chatId, g) => tg(env, "setChatMenuButton", { chat_id: chatId, menu_button: { type: "web_app", text: "Розклад", web_app: { url: siteLink(env, { group: g.name }) } } });

async function showSettings(env, chatId, index, user, msg) {
  const g = findGroup(index, user.g);
  const data = g ? await getJSON(env, `data/g/${g.slug}.json`).catch(() => null) : null;
  const text = settingsText(user, data), kb = settingsKb(user, data);
  return msg ? edit(env, msg, text, kb) : send(env, chatId, text, kb);
}

async function chooseGroup(env, chatId, index, user, slugOrName, msg) {
  const g = findGroup(index, slugOrName);
  if (!g) return false;
  const u = { ...DEFAULTS, ...(user || {}), g: g.name };
  if (user && user.g !== g.name) u.sg = 0; // нова група — підгрупа скидається
  await saveUser(env, chatId, u);
  if (chatId > 0) await setMenuButton(env, chatId, g);
  const text = `✅ Твоя група: <b>${esc(g.name)}</b> (${g.course} курс)\n\nТепер я надсилатиму її розклад сам. Кнопки внизу — щоб подивитися будь-коли.`;
  if (msg) await edit(env, msg, text); else await send(env, chatId, text, MENU);
  if (msg) await send(env, chatId, "Меню 👇", MENU);
  const data = await getJSON(env, `data/g/${g.slug}.json`);
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
  const [index, user] = await Promise.all([getJSON(env, "data/index.json"), getUser(env, chatId)]);
  const groupData = async (u) => { const g = u && findGroup(index, u.g); return g ? getJSON(env, `data/g/${g.slug}.json`) : null; };

  if (cb) {
    const d = cb.data || "";
    tg(env, "answerCallbackQuery", { callback_query_id: cb.id });
    if (d === "courses") return edit(env, cb.message, "Обери свій курс 👇", coursesKb(index));
    if (d.startsWith("c:")) return edit(env, cb.message, `${d.slice(2)} курс — обери групу 👇`, groupsKb(index, d.slice(2)));
    if (d.startsWith("g:")) return chooseGroup(env, chatId, index, user, d.slice(2), cb.message);
    if (d === "help") return edit(env, cb.message, helpText(user, chatId), { inline_keyboard: [[{ text: "⚙️ Налаштування", callback_data: "settings" }]] });
    if (d === "stop?") return edit(env, cb.message, "Точно відписатися? Я перестану надсилати розклад.", stopKb);
    if (d === "stop") { await env.USERS.delete("u:" + chatId); return edit(env, cb.message, "Готово, я більше не писатиму. Повернутися — /start"); }
    if (["settings", "s:", "t:", "h:"].some((p) => d.startsWith(p))) {
      if (!user) return edit(env, cb.message, "Спершу обери групу 👇", coursesKb(index));
      if (d.startsWith("t:")) return edit(env, cb.message, d === "t:mh" ? "🌅 О котрій надсилати пари на сьогодні?" : "🌆 О котрій надсилати пари на завтра?", hoursKb(user, d.slice(2)));
      if (d.startsWith("h:")) { const [, k, h] = d.split(":"); if (["mh", "eh"].includes(k)) { user[k] = +h; user[k === "mh" ? "m" : "e"] = 1; } }
      else if (d === "s:sg") user.sg = (user.sg + 1) % 3;
      else if (d.startsWith("s:")) { const k = d.slice(2); if (["m", "e", "c", "w", "s"].includes(k)) user[k] = user[k] ? 0 : 1; }
      if (d !== "settings") await saveUser(env, chatId, user);
      return showSettings(env, chatId, index, user, cb.message);
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
    const payload = text.split(/\s+/)[1]; // t.me/<бот>?start=sob-11 — одразу обрати групу
    if (payload && (await chooseGroup(env, chatId, index, user, payload))) return;
    if (!user) return send(env, chatId, "Привіт! Я бот розкладу ПФК НЮУ 👋\nОбери свій курс 👇", coursesKb(index));
    await send(env, chatId, helpText(user, chatId), MENU);
    return;
  }
  if (key === "help") return send(env, chatId, helpText(user, chatId), MENU);
  if (key === "group") return send(env, chatId, "Обери свій курс 👇", coursesKb(index));
  if (key === "stop") { await env.USERS.delete("u:" + chatId); return send(env, chatId, "Готово, я більше не писатиму. Повернутися — /start", { remove_keyboard: true }); }
  if (key === "settings") return user ? showSettings(env, chatId, index, user) : send(env, chatId, "Спершу обери групу 👇", coursesKb(index));
  if (key) {
    const g = await groupData(user);
    if (!g) return send(env, chatId, "Спершу обери групу 👇", coursesKb(index));
    const [t, kb] = view(env, g, key, user.sg);
    return send(env, chatId, t, kb);
  }
  // просто написали назву групи, напр. «соб-11»
  if (findGroup(index, text)) return chooseGroup(env, chatId, index, user, text);
  if (msg.chat.type === "private") return send(env, chatId, "Не зрозумів 🤔 Натисни кнопку внизу або напиши назву групи (напр. СОБ-11).", MENU);
}

// ---------------------------------------------------------------- API для GitHub Actions
async function api(env, path, request) {
  if (path === "/api/users") {
    const users = [];
    let cursor;
    do {
      const r = await env.USERS.list({ prefix: "u:", cursor });
      for (const k of r.keys) if (k.metadata?.g) users.push({ id: k.name.slice(2), ...DEFAULTS, ...k.metadata });
      cursor = r.list_complete ? null : r.cursor;
    } while (cursor);
    return { users };
  }
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
  return null;
}

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const secret = await secretOf(env.BOT_TOKEN);
    if (url.pathname.startsWith("/api/")) {
      if (request.headers.get("Authorization") !== "Bearer " + secret) return new Response("forbidden", { status: 403 });
      const r = await api(env, url.pathname, request);
      return r ? Response.json(r) : new Response("not found", { status: 404 });
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
