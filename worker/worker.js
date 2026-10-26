// Cloudflare Worker: Telegram-бот розкладу з кнопками (безкоштовний тариф).
// Змінні: BOT_TOKEN (secret), SITE_URL (https://USER.github.io/pfk-rozklad/), WEBHOOK_SECRET (secret)
const DAYS = ["Неділя", "Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота"];
const SHORT = ["Нд", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];
const NUM = ["0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"];
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
const dow = (iso) => new Date(iso + "T12:00:00Z").getUTCDay();
const ddmm = (iso) => iso.slice(8, 10) + "." + iso.slice(5, 7);
const addDays = (iso, n) => { const d = new Date(iso + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); };
const kyivISO = (o = 0) => new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Kyiv", year: "numeric", month: "2-digit", day: "2-digit" }).format(new Date(Date.now() + o * 86400000));
const schoolDay = (iso, step) => { let d = addDays(iso, step); while ([0, 6].includes(dow(d))) d = addDays(d, step); return d; };

const MENU = { keyboard: [[{ text: "📅 Сьогодні" }, { text: "➡️ Завтра" }], [{ text: "🗓 Тиждень" }, { text: "🔄 Заміни" }], [{ text: "ℹ️ Допомога" }]], resize_keyboard: true, is_persistent: true };

function fmtDay(site, iso, title) {
  const day = site.days.find((d) => d.date === iso);
  const head = `📅 <b>${title}: ${DAYS[dow(iso)]}, ${ddmm(iso)}</b> — ${esc(site.group)}`;
  if ([0, 6].includes(dow(iso))) return `${head}\n😴 Вихідний`;
  if (!day) return `${head}\nНемає даних на цю дату.`;
  if (day.error) return `${head}\n⚠️ ${esc(day.error)}`;
  if (!day.items.length) return `${head}\nПар немає 🎉`;
  const lines = day.items.map((it) => {
    const n = NUM[it.pair] || it.pair;
    if (it.cancelled) return `${n} <s>${esc(it.subject)}</s> — <b>вікно</b>`;
    return `${n} ${it.start}–${it.end}  ${esc(((it.type || "") + " " + it.subject).trim())}${it.teacher ? ` (${esc(it.teacher)})` : ""}${it.changed ? " 🔄" : ""}`;
  });
  return [head, "", ...lines].join("\n");
}

function fmtWeek(site) {
  const today = kyivISO(0);
  const monday = addDays(today, dow(today) === 0 ? 1 : dow(today) === 6 ? 2 : 1 - dow(today));
  const out = [`🗓 <b>Тиждень ${ddmm(monday)}–${ddmm(addDays(monday, 4))}</b> — ${esc(site.group)}`];
  for (let i = 0; i < 5; i++) {
    const iso = addDays(monday, i);
    const day = site.days.find((d) => d.date === iso);
    const L = day && !day.error ? day.items.filter((x) => !x.cancelled) : null;
    if (!day || day.error) out.push(`<b>${SHORT[dow(iso)]} ${ddmm(iso)}</b>: ⚠️ ще не оновлено`);
    else if (!L.length) out.push(`<b>${SHORT[dow(iso)]} ${ddmm(iso)}</b>: пар немає 🎉`);
    else out.push(`<b>${SHORT[dow(iso)]} ${ddmm(iso)}</b> (${L[0].start}–${L[L.length - 1].end}, ${L.length} пар)\n` + L.map((x) => esc(x.subject) + (x.changed ? " 🔄" : "")).join(" · "));
  }
  return out.join("\n\n");
}

function fmtChanges(site) {
  const rows = site.changes.filter((z) => z.date >= kyivISO(0));
  return rows.length ? "🔄 <b>Заміни</b>\n\n" + rows.map((z) => `${ddmm(z.date)} (${SHORT[dow(z.date)]}), ${z.pair} пара: ${z.subject.toLowerCase() === "вікно" ? "<b>вікно</b>" : esc(z.subject + (z.teacher ? ", " + z.teacher : ""))}`).join("\n") : "Нових замін немає ✅";
}

const helpText = (site, base, chatId) => `Привіт! Я бот розкладу <b>${esc(site.group)}</b> (ПФК НЮУ) 👋\n\nКнопки внизу:\n📅 Сьогодні — пари на сьогодні\n➡️ Завтра — наступний навчальний день\n🗓 Тиждень — огляд на тиждень\n🔄 Заміни — вікна й заміни\n\nАвтоматично пишу сам:\n• о 06:15 — пари на сьогодні\n• о 18:00 — пари на завтра (у неділю — огляд тижня)\n• одразу, коли на сайті коледжу з'являються заміни чи новий розклад\n\n🌐 Сайт: ${base}\n\nТвій chat id: <code>${chatId}</code>`;

function dayKeyboard(iso, base) {
  const today = kyivISO(0);
  return { inline_keyboard: [
    [{ text: "◀️", callback_data: "d:" + schoolDay(iso, -1) }, { text: iso === today ? "• Сьогодні •" : "Сьогодні", callback_data: "d:" + today }, { text: "▶️", callback_data: "d:" + schoolDay(iso, 1) }],
    [{ text: "🗓 Тиждень", callback_data: "week" }, { text: "🔄 Заміни", callback_data: "changes" }, { text: "🌐 Сайт", url: base }],
  ] };
}
const backKeyboard = (base) => ({ inline_keyboard: [[{ text: "📅 Сьогодні", callback_data: "d:" + kyivISO(0) }, { text: "➡️ Завтра", callback_data: "d:" + schoolDay(kyivISO(0), 1) }], [{ text: "🌐 Сайт", url: base }]] });

async function tg(env, method, body) {
  return fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/${method}`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify(body) });
}

function route(site, base, key) {
  const today = kyivISO(0);
  if (key.startsWith("d:")) { const iso = key.slice(2); return { text: fmtDay(site, iso, iso === today ? "Сьогодні" : iso === schoolDay(today, 1) ? "Завтра" : "День"), kb: dayKeyboard(iso, base) }; }
  if (key === "today") return { text: fmtDay(site, today, "Сьогодні"), kb: dayKeyboard(today, base) };
  if (key === "tomorrow") { const iso = schoolDay(today, 1); return { text: fmtDay(site, iso, iso === addDays(today, 1) ? "Завтра" : "Наступний навчальний день"), kb: dayKeyboard(iso, base) }; }
  if (key === "week") return { text: fmtWeek(site), kb: backKeyboard(base) };
  if (key === "changes") return { text: fmtChanges(site), kb: backKeyboard(base) };
  return null;
}
function keyFromText(t) {
  t = t.toLowerCase().replace(/@\w+/, "").trim();
  if (t.startsWith("/start") || t.startsWith("/help") || t.includes("допомог")) return "help";
  if (t.startsWith("/today") || t.startsWith("/tod") || t.includes("сьогодн")) return "today";
  if (t.startsWith("/tom") || t.includes("завтра") || t.includes("tomor")) return "tomorrow";
  if (t.startsWith("/week") || t.includes("тижд")) return "week";
  if (t.startsWith("/changes") || t.includes("замін") || t.includes("змін")) return "changes";
  return null;
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("pfk-rozklad bot OK");
    if (env.WEBHOOK_SECRET && request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) return new Response("forbidden", { status: 403 });
    const upd = await request.json();
    const base = env.SITE_URL.endsWith("/") ? env.SITE_URL : env.SITE_URL + "/";
    const cb = upd.callback_query;
    const msg = upd.message || upd.edited_message;
    const chatId = cb ? cb.message.chat.id : msg && msg.chat.id;
    if (!chatId) return new Response("ok");
    let site;
    try { site = await (await fetch(base + "site.json?" + Math.floor(Date.now() / 60000))).json(); }
    catch (e) { await tg(env, "sendMessage", { chat_id: chatId, text: "⚠️ Не вдалося отримати розклад. Спробуй пізніше." }); return new Response("ok"); }

    if (cb) {
      const r = route(site, base, cb.data);
      await tg(env, "answerCallbackQuery", { callback_query_id: cb.id });
      if (r) await tg(env, "editMessageText", { chat_id: chatId, message_id: cb.message.message_id, text: r.text, parse_mode: "HTML", reply_markup: r.kb, disable_web_page_preview: true });
      return new Response("ok");
    }
    if (!msg.text) return new Response("ok");
    const key = keyFromText(msg.text);
    if (key === "help") {
      await tg(env, "sendMessage", { chat_id: chatId, text: helpText(site, base, chatId), parse_mode: "HTML", reply_markup: MENU, disable_web_page_preview: true });
    } else if (key) {
      const r = route(site, base, key);
      await tg(env, "sendMessage", { chat_id: chatId, text: r.text, parse_mode: "HTML", reply_markup: r.kb, disable_web_page_preview: true });
    }
    return new Response("ok");
  },
};
