// Cloudflare Worker: відповідає на команди Telegram-бота розкладу (безкоштовний тариф Workers).
// Змінні (Settings → Variables and Secrets): BOT_TOKEN (secret), SITE_URL (напр. https://USER.github.io/pfk-rozklad/), WEBHOOK_SECRET (secret, будь-який рядок)
// Команди: /start, /today, /tomorrow, /week, /changes (або слова: сьогодні, завтра, тиждень, заміни)

const DAYS = ["Неділя", "Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота"];
const NUM = ["0️⃣", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣"];
const esc = (s) => String(s ?? "").replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));

function kyivISO(offsetDays = 0) {
  const d = new Date(Date.now() + offsetDays * 86400000);
  return new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Kyiv", year: "numeric", month: "2-digit", day: "2-digit" }).format(d);
}
const dow = (iso) => new Date(iso + "T12:00:00Z").getUTCDay();
const ddmm = (iso) => iso.slice(8, 10) + "." + iso.slice(5, 7);

function fmtDay(site, iso, title) {
  const day = site.days.find((d) => d.date === iso);
  const head = `📅 <b>${title}: ${DAYS[dow(iso)]}, ${ddmm(iso)}</b> — ${esc(site.group)}`;
  if (dow(iso) === 0 || dow(iso) === 6) return `${head}\n😴 Вихідний`;
  if (!day) return `${head}\nНемає даних на цю дату.`;
  if (day.error) return `${head}\n⚠️ ${esc(day.error)}`;
  if (!day.items.length) return `${head}\nПар немає 🎉`;
  const lines = day.items.map((it) => {
    const n = NUM[it.pair] || it.pair;
    if (it.cancelled) return `${n} <s>${esc(it.subject)}</s> — <b>вікно</b>`;
    return `${n} ${it.start}–${it.end}  ${esc(((it.type || "") + " " + it.subject).trim())}${it.teacher ? ` (${esc(it.teacher)})` : ""}${it.changed ? " 🔄" : ""}`;
  });
  return [head, ...lines].join("\n");
}

function nextSchoolDay(offset) {
  let o = offset;
  while ([0, 6].includes(dow(kyivISO(o)))) o++;
  return kyivISO(o);
}

async function reply(env, chatId, text) {
  await fetch(`https://api.telegram.org/bot${env.BOT_TOKEN}/sendMessage`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ chat_id: chatId, text, parse_mode: "HTML", disable_web_page_preview: true }),
  });
}

export default {
  async fetch(request, env) {
    if (request.method !== "POST") return new Response("pfk-rozklad bot OK");
    if (env.WEBHOOK_SECRET && request.headers.get("X-Telegram-Bot-Api-Secret-Token") !== env.WEBHOOK_SECRET) {
      return new Response("forbidden", { status: 403 });
    }
    const upd = await request.json();
    const msg = upd.message || upd.edited_message;
    if (!msg || !msg.text) return new Response("ok");
    const chatId = msg.chat.id;
    const t = msg.text.toLowerCase().replace(/@\w+/, "").trim();
    const base = env.SITE_URL.endsWith("/") ? env.SITE_URL : env.SITE_URL + "/";
    let site;
    try {
      site = await (await fetch(base + "site.json", { cf: { cacheTtl: 300 } })).json();
    } catch (e) {
      await reply(env, chatId, "⚠️ Не вдалося отримати розклад. Спробуй пізніше.");
      return new Response("ok");
    }
    let text;
    if (t.startsWith("/start") || t.startsWith("/help")) {
      text = `Привіт! Я бот розкладу <b>${esc(site.group)}</b> (ПФК НЮУ).\n\n/today — пари сьогодні\n/tomorrow — завтра\n/week — тиждень\n/changes — заміни\n\n🌐 ${base}\n\nID цього чату: <code>${chatId}</code>`;
    } else if (t.startsWith("/today") || t.includes("сьогодн")) {
      text = fmtDay(site, kyivISO(0), "Сьогодні");
    } else if (t.startsWith("/tomorrow") || t.includes("завтра")) {
      const iso = nextSchoolDay(1);
      text = fmtDay(site, iso, iso === kyivISO(1) ? "Завтра" : "Наступний навчальний день");
    } else if (t.startsWith("/week") || t.includes("тижд")) {
      const out = [];
      for (let i = 0; i < 7; i++) { const iso = kyivISO(i); if (![0, 6].includes(dow(iso))) out.push(fmtDay(site, iso, DAYS[dow(iso)])); }
      text = out.join("\n\n");
    } else if (t.startsWith("/changes") || t.includes("замін") || t.includes("змін")) {
      const today = kyivISO(0);
      const rows = site.changes.filter((z) => z.date >= today);
      text = rows.length ? "🔄 <b>Заміни</b>\n" + rows.map((z) => `${ddmm(z.date)}, ${z.pair} пара (${esc(z.group)}): ${z.subject === "вікно" ? "<b>вікно</b>" : esc(z.subject + (z.teacher ? ", " + z.teacher : ""))}`).join("\n") : "Нових замін немає ✅";
    } else {
      return new Response("ok");
    }
    await reply(env, chatId, text);
    return new Response("ok");
  },
};
