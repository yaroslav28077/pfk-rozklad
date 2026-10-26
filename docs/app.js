/* Розклад ПФК — клієнтська частина. Дані: site.json (генерує scripts/bot.py). */
const DAYS = ["Неділя", "Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота"];
const SHORT = ["Нд", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];
let SITE = null;
let TAB = location.hash.replace("#", "") || "today";
let WEEK_SEL = null;

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ---------- іконки (у стилі Lucide, MIT) ---------- */
const I = {
  scale: '<path d="M12 3v18M7 21h10M5 7h14M5 7l-3 7a3 3 0 0 0 6 0L5 7zM19 7l-3 7a3 3 0 0 0 6 0l-3-7z"/>',
  shield: '<path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/>',
  chart: '<path d="M3 3v18h18"/><path d="M7 15l4-4 3 3 5-6"/>',
  calc: '<rect x="5" y="2" width="14" height="20" rx="2"/><path d="M8 6h8M8 11h.01M12 11h.01M16 11h.01M8 15h.01M12 15h.01M16 15h.01M8 19h8"/>',
  book: '<path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20V3H6.5A2.5 2.5 0 0 0 4 5.5v14z"/><path d="M6.5 17A2.5 2.5 0 0 0 4 19.5 2.5 2.5 0 0 0 6.5 22H20v-5"/>',
  pen: '<path d="M12 20h9"/><path d="M16.5 3.5a2.1 2.1 0 0 1 3 3L7 19l-4 1 1-4z"/>',
  flask: '<path d="M9 3h6M10 3v6L4.5 19A2 2 0 0 0 6.3 22h11.4a2 2 0 0 0 1.8-3L14 9V3"/><path d="M7 15h10"/>',
  globe: '<circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3a14 14 0 0 1 0 18M12 3a14 14 0 0 0 0 18"/>',
  lang: '<path d="M5 8l6 6M4 14l6-6 2-3M2 5h12M7 2h1M22 22l-5-10-5 10M14 18h6"/>',
  monitor: '<rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8M12 17v4"/>',
  landmark: '<path d="M3 22h18M6 18v-7M10 18v-7M14 18v-7M18 18v-7M12 2l9 5H3z"/>',
  users: '<path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M22 21v-2a4 4 0 0 0-3-3.9M16 3.1a4 4 0 0 1 0 7.8"/>',
  run: '<circle cx="13" cy="4" r="2"/><path d="M4 22l4-6 3 2 2-5 3 3h4M10 12l-2-3 4-2 3 3"/>',
  cap: '<path d="M22 10L12 5 2 10l10 5 10-5z"/><path d="M6 12v5c3 2 9 2 12 0v-5"/>',
  coffee: '<path d="M17 8h1a4 4 0 0 1 0 8h-1M3 8h14v9a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4z"/><path d="M6 2v2M10 2v2M14 2v2"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
  check: '<path d="M20 6L9 17l-5-5"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h6"/>',
  cal: '<rect x="3" y="4" width="18" height="18" rx="3"/><path d="M16 2v4M8 2v4M3 10h18M12 14v5M9.5 16.5h5"/>',
  phone: '<rect x="6" y="2" width="12" height="20" rx="3"/><path d="M11 18h2"/>',
  bot: '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M9 17h6"/>',
  ext: '<path d="M15 3h6v6M10 14L21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
};
const svg = (k) => `<svg viewBox="0 0 24 24" aria-hidden="true">${I[k] || I.cap}</svg>`;

function subjectIcon(name) {
  const n = (name || "").toLowerCase();
  if (n.includes("теорія права") || n.includes("право")) return "scale";
  if (n.includes("захист")) return "shield";
  if (n.includes("економ")) return "chart";
  if (n.includes("математ")) return "calc";
  if (n.includes("літератур")) return "book";
  if (n.includes("українська мова") || n.includes("мова (за")) return "pen";
  if (n.includes("природн") || n.includes("хім") || n.includes("біолог")) return "flask";
  if (n.includes("історія")) return "landmark";
  if (n.includes("іноземна") || n.includes("англ")) return "lang";
  if (n.includes("інформат") || n.includes("комп")) return "monitor";
  if (n.includes("кураторськ")) return "users";
  if (n.includes("фізичн")) return "run";
  if (n.includes("всесвіт") || n.includes("географ")) return "globe";
  return "cap";
}
/* стабільний колір предмета (контраст ≥4.5:1 на білому) */
const PALETTE = ["#4338ca", "#be185d", "#047857", "#b45309", "#0369a1", "#7c3aed", "#b91c1c", "#0f766e", "#a21caf", "#4d7c0f"];
function subjectColor(name) {
  let h = 0;
  for (const ch of (name || "").toLowerCase()) h = (h * 31 + ch.codePointAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

/* ---------- час (Київ) ---------- */
function kyivNow() {
  const p = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Kyiv", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).formatToParts(new Date());
  const g = (t) => p.find((x) => x.type === t).value;
  const hh = g("hour") === "24" ? "00" : g("hour");
  return { iso: `${g("year")}-${g("month")}-${g("day")}`, hm: `${hh}:${g("minute")}`, sec: (+hh) * 3600 + (+g("minute")) * 60 + (+g("second")) };
}
const toSec = (hm) => { const [h, m] = hm.split(":").map(Number); return h * 3600 + m * 60; };
function addDays(iso, n) { const d = new Date(iso + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); }
const dow = (iso) => new Date(iso + "T12:00:00Z").getUTCDay();
const ddmm = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;
function dur(s) {
  s = Math.max(0, Math.round(s / 60));
  const h = Math.floor(s / 60), m = s % 60;
  return h ? `${h} год ${m} хв` : `${m} хв`;
}
function findDay(iso) { return SITE.days.find((d) => d.date === iso) || { date: iso, items: [], error: "Немає даних на цю дату" }; }
const live = (day) => (day.items || []).filter((i) => !i.cancelled);

/* ---------- компоненти ---------- */
function lessonHTML(it, state) {
  const c = it.cancelled ? "#b91c1c" : subjectColor(it.subject);
  const cls = ["lesson", "fade"];
  if (state === "now") cls.push("now");
  if (state === "done") cls.push("done");
  if (it.cancelled) cls.push("cancelled");
  const chip = it.type ? `<span class="chip ${it.type === "Лк" ? "lk" : "pr"}">${it.type === "Лк" ? "Лекція" : it.type === "Пр" ? "Практичне" : esc(it.type)}</span>` : "";
  const room = it.room ? (/^\d+$/.test(it.room) ? `ауд. ${esc(it.room)}` : esc(it.room)) : "";
  const info = [it.teacher ? esc(it.teacher) : "", room].filter(Boolean).map((x) => `<span>${x}</span>`).join("");
  let note = "";
  if (it.cancelled) note = `<div class="note">Вікно — пари немає</div>`;
  else if (it.changed) note = `<div class="note">Заміна${it.was ? ` · було: ${esc(it.was)}` : ""}</div>`;
  return `<article class="${cls.join(" ")}" style="--c:${c};--cs:${c}1a">
    <div class="when"><span class="n">${it.pair}</span><span class="t">${esc(it.start)}</span></div>
    <div class="what">
      <div class="row1"><span class="ic">${svg(subjectIcon(it.subject))}</span><span class="name">${esc(it.subject)}</span></div>
      <div class="info">${chip}<span>${esc(it.start)}–${esc(it.end)}</span>${info}</div>${note}
    </div></article>`;
}

function dayListHTML(day, isToday) {
  const wd = dow(day.date);
  if (wd === 0 || wd === 6) return `<div class="empty">${svg("coffee")}Вихідний — відпочивай</div>`;
  if (day.error) return `<div class="empty">${svg("file")}${esc(day.error)}<br><small>Дивись PDF у розділі «Ще»</small></div>`;
  if (!day.items.length) return `<div class="empty">${svg("check")}Пар немає</div>`;
  const now = kyivNow().sec;
  const out = [];
  day.items.forEach((it, k) => {
    let st = "";
    if (isToday && !it.cancelled) st = now >= toSec(it.end) ? "done" : now >= toSec(it.start) ? "now" : "";
    out.push(lessonHTML(it, st));
    const nx = day.items[k + 1];
    if (nx && !it.cancelled && !nx.cancelled) {
      const gap = (toSec(nx.start) - toSec(it.end)) / 60;
      if (gap >= 20) out.push(`<div class="break">перерва ${gap} хв</div>`);
    }
  });
  return `<div class="list">${out.join("")}</div>`;
}

function heroHTML(day) {
  const wd = dow(day.date);
  const L = live(day);
  const now = kyivNow().sec;
  if (wd === 0 || wd === 6 || day.error || !L.length) {
    const nextIso = nextSchoolDay(1);
    const nd = findDay(nextIso);
    const first = live(nd)[0];
    return `<section class="hero calm fade"><div class="label">${wd === 0 || wd === 6 ? "Вихідний" : "Сьогодні пар немає"}</div>
      <div class="big">${first ? `${DAYS[dow(nextIso)]}: перша пара о ${esc(first.start)}` : "Відпочивай"}</div>
      <div class="meta">${first ? esc(first.subject) : ""}</div></section>`;
  }
  const cur = L.find((i) => now >= toSec(i.start) && now < toSec(i.end));
  const nxt = L.find((i) => toSec(i.start) > now);
  if (cur) {
    const total = toSec(cur.end) - toSec(cur.start), passed = now - toSec(cur.start);
    return `<section class="hero fade"><div class="label">Зараз · ${cur.pair} пара</div>
      <div class="big">${esc(cur.subject)}</div>
      <div class="meta">${[cur.teacher, cur.room && /^\d+$/.test(cur.room) ? "ауд. " + cur.room : cur.room].filter(Boolean).map(esc).join(" · ")}</div>
      <div class="timer"><b data-countdown="${toSec(cur.end)}">${dur(toSec(cur.end) - now)}</b><span>до кінця</span></div>
      <div class="bar"><i style="width:${Math.min(100, (passed / total) * 100).toFixed(1)}%"></i></div>
      ${nxt ? `<div class="meta" style="margin-top:10px">Далі о ${esc(nxt.start)} — ${esc(nxt.subject)}</div>` : `<div class="meta" style="margin-top:10px">Це остання пара сьогодні</div>`}</section>`;
  }
  if (nxt) {
    const first = nxt === L[0];
    return `<section class="hero fade"><div class="label">${first ? "Перша пара" : "Наступна пара"} · ${nxt.pair}</div>
      <div class="big">${esc(nxt.subject)}</div>
      <div class="meta">о ${esc(nxt.start)}${nxt.room && /^\d+$/.test(nxt.room) ? " · ауд. " + esc(nxt.room) : ""}</div>
      <div class="timer"><b data-countdown="${toSec(nxt.start)}">${dur(toSec(nxt.start) - now)}</b><span>до початку</span></div></section>`;
  }
  return `<section class="hero calm fade"><div class="label">На сьогодні все</div><div class="big">Пари закінчились</div>
    <div class="meta">${(() => { const n = nextSchoolDay(1); const f = live(findDay(n))[0]; return f ? `${DAYS[dow(n)]}: перша о ${esc(f.start)} — ${esc(f.subject)}` : ""; })()}</div></section>`;
}

function nextSchoolDay(offset) {
  let o = offset;
  const t = kyivNow().iso;
  while ([0, 6].includes(dow(addDays(t, o)))) o++;
  return addDays(t, o);
}

/* ---------- вкладки ---------- */
function render() {
  document.querySelectorAll(".tabbar button").forEach((b) => b.classList.toggle("active", b.dataset.tab === TAB));
  const today = kyivNow().iso;
  const v = $("#view");
  if (TAB === "today") {
    const d = findDay(today);
    v.innerHTML = heroHTML(d) + `<div class="section-title"><h2>Сьогодні</h2><span>${DAYS[dow(today)]}, ${ddmm(today)}</span></div>` + dayListHTML(d, true);
  } else if (TAB === "tomorrow") {
    const iso = nextSchoolDay(1);
    const d = findDay(iso);
    const L = live(d);
    const title = iso === addDays(today, 1) ? "Завтра" : "Наступний навчальний день";
    const summary = L.length ? `${L.length} ${L.length === 1 ? "пара" : L.length < 5 ? "пари" : "пар"} · ${L[0].start}–${L[L.length - 1].end}` : "";
    v.innerHTML = `<div class="section-title"><h2>${title}</h2><span>${DAYS[dow(iso)]}, ${ddmm(iso)}</span></div>` +
      (summary ? `<p class="muted" style="margin:0 2px 10px">${summary}</p>` : "") + dayListHTML(d, false);
  } else if (TAB === "week") {
    const monday = addDays(today, dow(today) === 0 ? -6 : 1 - dow(today));
    const startMon = dow(today) === 6 || dow(today) === 0 ? addDays(monday, 7) : monday;
    const week = [0, 1, 2, 3, 4].map((i) => addDays(startMon, i));
    if (!WEEK_SEL || !week.includes(WEEK_SEL)) WEEK_SEL = week.includes(today) ? today : week[0];
    v.innerHTML = `<div class="section-title"><h2>Тиждень</h2><span>${ddmm(week[0])}–${ddmm(week[4])}</span></div>
      <div class="days" role="tablist">${week.map((iso) => `<button role="tab" aria-selected="${iso === WEEK_SEL}" data-day="${iso}" class="${iso === WEEK_SEL ? "active" : ""} ${iso === today ? "today" : ""}">${SHORT[dow(iso)]}<small>${ddmm(iso)}</small></button>`).join("")}</div>
      <div class="section-title"><h2>${DAYS[dow(WEEK_SEL)]}</h2><span>${live(findDay(WEEK_SEL)).length} пар</span></div>` + dayListHTML(findDay(WEEK_SEL), WEEK_SEL === today);
    v.querySelectorAll("[data-day]").forEach((b) => b.addEventListener("click", () => { WEEK_SEL = b.dataset.day; render(); }));
  } else if (TAB === "changes") {
    const rows = SITE.changes.filter((z) => z.date >= addDays(today, -7)).sort((a, b) => (a.date + a.pair).localeCompare(b.date + b.pair));
    v.innerHTML = `<div class="section-title"><h2>Заміни</h2><span>${esc(SITE.group)} + весь курс</span></div>` +
      (rows.length ? `<div class="list">${rows.map((z) => {
        const win = z.subject.toLowerCase() === "вікно";
        return `<div class="change ${win ? "win" : ""} ${z.date < today ? "past" : ""} fade"><div class="d">${ddmm(z.date)}<small>${SHORT[dow(z.date)]} · ${z.pair} п.</small></div>
          <div class="body"><b>${win ? "Вікно — пари немає" : esc(z.subject)}</b><span>${[z.teacher, z.kind, z.group].filter(Boolean).map(esc).join(" · ")}</span></div></div>`;
      }).join("")}</div>` : `<div class="empty">${svg("check")}Замін немає</div>`) +
      `<p class="muted" style="margin:12px 2px">Минулі заміни показуються блідими ще тиждень.</p>`;
  } else {
    v.innerHTML = `<div class="section-title"><h2>Ще</h2></div>
      <div class="card">
        <a class="link-row" href="rozklad.ics" download>${svg("cal")}<div>Додати в календар<small>Google Calendar / iPhone — усі пари з нагадуваннями</small></div></a>
        <a class="link-row" href="#" id="install">${svg("phone")}<div>Встановити як застосунок<small id="install-hint">Іконка на головному екрані телефону</small></div></a>
        <a class="link-row" href="${esc(SITE.source_page)}" target="_blank" rel="noopener">${svg("ext")}<div>Сторінка розкладу коледжу<small>plc.nlu.edu.ua</small></div></a>
      </div>
      <div class="section-title"><h2>PDF на сайті коледжу</h2></div>
      <div class="card">${SITE.pdfs.map((p) => `<a class="link-row" href="${esc(p.url)}" target="_blank" rel="noopener">${svg("file")}<div>${esc(p.title)}</div></a>`).join("") || '<p class="muted">Немає</p>'}</div>
      <div class="section-title"><h2>Колонка ${esc(SITE.group)} з PDF</h2></div>
      <div class="card"><p class="muted" style="margin:0">Фрагмент останнього PDF розкладу курсу — для звірки.</p><img class="preview" src="novyi_rozklad.png" alt="Фрагмент PDF розкладу" loading="lazy" onerror="this.remove()"></div>`;
    setupInstall();
  }
}

/* ---------- PWA ---------- */
let deferredPrompt = null;
window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); deferredPrompt = e; });
function setupInstall() {
  const a = $("#install"), hint = $("#install-hint");
  if (!a) return;
  const ios = /iphone|ipad|ipod/i.test(navigator.userAgent);
  if (window.matchMedia("(display-mode: standalone)").matches) hint.textContent = "Вже встановлено ✓";
  else if (ios) hint.textContent = "Safari → «Поділитися» → «На початковий екран»";
  a.addEventListener("click", async (e) => {
    e.preventDefault();
    if (deferredPrompt) { deferredPrompt.prompt(); await deferredPrompt.userChoice; deferredPrompt = null; }
    else if (!ios) hint.textContent = "Меню браузера (⋮) → «Встановити застосунок» / «Додати на головний екран»";
  });
}
if ("serviceWorker" in navigator) window.addEventListener("load", () => navigator.serviceWorker.register("sw.js").catch(() => {}));

/* ---------- таймер ---------- */
function tick() {
  const now = kyivNow().sec;
  let rerender = false;
  document.querySelectorAll("[data-countdown]").forEach((el) => {
    const left = +el.dataset.countdown - now;
    if (left <= 0) rerender = true; else el.textContent = dur(left);
  });
  if (rerender) render();
}

async function init() {
  try {
    SITE = await (await fetch("site.json?" + Date.now(), { cache: "no-store" })).json();
  } catch (e) {
    $("#view").innerHTML = `<div class="empty">${svg("file")}Не вдалося завантажити дані. Перевір інтернет і онови сторінку.</div>`;
    return;
  }
  $("#title").textContent = `Розклад ${SITE.group}`;
  document.title = `Розклад ${SITE.group} · ПФК НЮУ`;
  const s = SITE.schedule;
  $("#subtitle").textContent = `${SITE.course} курс · ПФК НЮУ${s.from ? ` · діє ${ddmm(s.from)}–${ddmm(s.to)}` : ""}`;
  $("#checked").textContent = SITE.last_check ? `Сайт коледжу перевірено ${SITE.last_check.slice(8, 10)}.${SITE.last_check.slice(5, 7)} о ${SITE.last_check.slice(11, 16)}` : "";
  $("#src").href = SITE.source_page;
  const today = kyivNow().iso;
  const msgs = [];
  if (SITE.new_schedule) msgs.push(`На сайті коледжу новий розклад: «${esc(SITE.new_schedule.title)}». Тут поки старий — <a href="${esc(SITE.new_schedule.url)}" target="_blank" rel="noopener">відкрити PDF</a>.`);
  if (s.to && today > s.to) msgs.push(`Розклад діяв до ${ddmm(s.to)}. Новий ще не внесено — дивись PDF у розділі «Ще».`);
  if (msgs.length) { $("#banner").innerHTML = msgs.join("<br>"); $("#banner").hidden = false; }
  if (SITE.changes.some((z) => z.date >= today)) $("#changes-dot").hidden = false;
  document.querySelectorAll(".tabbar button").forEach((b) => b.addEventListener("click", () => {
    TAB = b.dataset.tab; history.replaceState(null, "", "#" + TAB); render(); window.scrollTo({ top: 0, behavior: "smooth" });
  }));
  if (!["today", "tomorrow", "week", "changes", "more"].includes(TAB)) TAB = "today";
  render();
  setInterval(tick, 20000);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) render(); });
}
init();
