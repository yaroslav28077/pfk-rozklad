/* Розклад ПФК — клієнтська частина. Дані: data/index.json + data/consultations.json + data/g|t/<slug>.json. */
const DAYS = ["Неділя", "Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота"];
const SHORT = ["Нд", "Пн", "Вт", "Ср", "Чт", "Пт", "Сб"];
var SITE = null;       // дані обраної групи або викладача
var INDEX = null;      // список курсів, груп і викладачів
var CONSULT = null;    // графік консультацій (відпрацювань)
var TAB = location.hash.replace("#", "") || "today";
var DAY_MODE = "today";       // "today" | "tomorrow" на першій вкладці
var WEEK_MON = null;          // понеділок обраного тижня (YYYY-MM-DD)
var WEEK_SEL = null;          // обраний день тижня (YYYY-MM-DD)
var PICK_MODE = "student";
var CONSULT_MODE = "today";   // "today" | "week" | "mine"
var CONSULT_Q = "";
if (TAB === "tomorrow") { TAB = "today"; DAY_MODE = "tomorrow"; }
const isTeacher = () => !!(SITE && SITE.kind === "teacher");

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const plural = (n, one, few, many) => (n % 10 === 1 && n % 100 !== 11 ? one : n % 10 >= 2 && n % 10 <= 4 && !(n % 100 >= 12 && n % 100 <= 14) ? few : many);
const pluralPairs = (n) => plural(n, "пара", "пари", "пар");
const pluralTeachers = (n) => plural(n, "викладач", "викладачі", "викладачів");
const normSubj = (s) => String(s || "").toLowerCase().replace(/\([^)]*\)/g, " ").replace(/[«»"'’ʼ.,;]/g, " ").replace(/\s+/g, " ").trim();
const normTeacher = (s) => String(s || "")
  .trim()
  .replace(/^(?:(?:(?:к|д)\.\s*[а-яіїєґ]+\.\s*н\.|д\.\s*ф\.|доц\.|проф\.|ст\.\s*викл\.|викл\.)[\s,]*)+/i, "")
  .replace(/\s+/g, " ")
  .replace(/\.+$/, "")
  .toLowerCase();
const surnameOf = (nt) => nt.split(/\s+/)[0] || "";
function myConsultSlots(items, site) {
  if (!items?.length) return [];
  if (!site) return items;
  if (site.kind === "teacher") return items.filter((c) => c.slug === site.slug);
  const teacherSubjs = new Map();
  const addFrom = (includeChanged) => {
    for (const d of Object.values(site.days || {})) {
      for (const it of d.items || []) {
        if (it.cancelled || (!includeChanged && it.changed) || !it.teacher) continue;
        const k = normTeacher(it.teacher);
        if (!k) continue;
        if (!teacherSubjs.has(k)) teacherSubjs.set(k, new Set());
        if (it.subject) teacherSubjs.get(k).add(normSubj(it.subject));
      }
    }
  };
  addFrom(false);
  if (!teacherSubjs.size) addFrom(true);
  const bySurname = new Map();
  for (const k of teacherSubjs.keys()) {
    const sn = surnameOf(k);
    if (sn.length >= 3) {
      if (!bySurname.has(sn)) bySurname.set(sn, []);
      bySurname.get(sn).push(k);
    }
  }
  const consSlugsBySurname = new Map();
  for (const c of items) {
    for (const nm of [c.teacher, ...(c.aliases || [])]) {
      const sn = surnameOf(normTeacher(nm));
      if (sn.length >= 3) {
        if (!consSlugsBySurname.has(sn)) consSlugsBySurname.set(sn, new Set());
        consSlugsBySurname.get(sn).add(c.slug);
      }
    }
  }
  const matched = [];
  for (const c of items) {
    const names = [c.teacher, ...(c.aliases || [])].map(normTeacher).filter(Boolean);
    let keys = names.filter((n) => teacherSubjs.has(n));
    if (!keys.length) {
      for (const n of names) {
        const sn = surnameOf(n);
        if (sn.length >= 3 && consSlugsBySurname.get(sn)?.size === 1 && bySurname.has(sn)) {
          keys = bySurname.get(sn);
          break;
        }
      }
    }
    if (keys.length) matched.push({ c, keys });
  }
  const bySlug = new Map();
  for (const m of matched) {
    if (!bySlug.has(m.c.slug)) bySlug.set(m.c.slug, []);
    bySlug.get(m.c.slug).push(m);
  }
  const keepIds = new Set();
  for (const arr of bySlug.values()) {
    if (arr.length === 1) { keepIds.add(arr[0].c.id); continue; }
    const subjs = new Set();
    for (const m of arr) for (const k of m.keys) for (const s of teacherSubjs.get(k) || []) subjs.add(s);
    const refined = arr.filter((m) => (m.c.subjects || []).some((s) => subjs.has(normSubj(s))));
    for (const m of (refined.length ? refined : arr)) keepIds.add(m.c.id);
  }
  return items.filter((c) => keepIds.has(c.id));
}

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
  check: '<path d="M20 6L9 17l-5-5"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6M9 13h6M9 17h6"/>',
  cal: '<rect x="3" y="4" width="18" height="18" rx="3"/><path d="M16 2v4M8 2v4M3 10h18M12 14v5M9.5 16.5h5"/>',
  phone: '<rect x="6" y="2" width="12" height="20" rx="3"/><path d="M11 18h2"/>',
  bot: '<rect x="4" y="8" width="16" height="12" rx="3"/><path d="M12 4v4M9 13h.01M15 13h.01M9 17h6"/>',
  ext: '<path d="M15 3h6v6M10 14L21 3M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>',
  clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
  sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41"/>',
  moon: '<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/>',
};
const svg = (k) => `<svg viewBox="0 0 24 24" aria-hidden="true">${I[k] || I.cap}</svg>`;

/* ---------- тема (світла / темна / системна) ---------- */
function currentThemeMode() {
  const saved = localStorage.getItem("theme");
  return saved === "light" || saved === "dark" ? saved : "auto";
}
function resolvedTheme() {
  const m = currentThemeMode();
  if (m === "light" || m === "dark") return m;
  return window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
function applyTheme(mode) {
  if (mode === "light" || mode === "dark") {
    localStorage.setItem("theme", mode);
    if (document.documentElement) document.documentElement.dataset.theme = mode;
  } else {
    if (localStorage.removeItem) localStorage.removeItem("theme"); else localStorage.setItem("theme", "");
    if (document.documentElement) delete document.documentElement.dataset.theme;
  }
  const actual = resolvedTheme();
  const btn = $("#theme-btn");
  if (btn) {
    btn.innerHTML = svg(actual === "dark" ? "sun" : "moon");
    btn.setAttribute("aria-label", actual === "dark" ? "Увімкнути світлу тему" : "Увімкнути темну тему");
  }
  const color = actual === "dark" ? "#15132A" : "#3E3577";
  document.querySelectorAll?.('meta[name="theme-color"]').forEach((m) => m.setAttribute("content", color));
}

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
/* стабільні кольори предметів (усі ≥ 7:1 WCAG AAA на білому) */
const PALETTE = ["#075985", "#3E3577", "#0F5E46", "#9A3412", "#8B1E4F", "#155E75", "#5B21B6", "#991B1B", "#3F6212", "#6B21A8"];
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
const mondayOf = (iso) => addDays(iso, dow(iso) === 0 ? -6 : 1 - dow(iso));
const ddmm = (iso) => `${iso.slice(8, 10)}.${iso.slice(5, 7)}`;
function dur(s) {
  s = Math.max(0, Math.round(s / 60));
  const h = Math.floor(s / 60), m = s % 60;
  return h ? `${h} год ${m} хв` : `${m} хв`;
}
function findDay(iso) {
  const d = SITE.days[iso];
  if (d) return { date: iso, items: d.items, url: d.url || "" };
  return { date: iso, items: [], error: [0, 6].includes(dow(iso)) ? null : "Розклад на цю дату ще не опубліковано" };
}
const live = (day) => (day.items || []).filter((i) => !i.cancelled);

function consultSlotsForTeacher(nameOrSlug) {
  const items = (CONSULT && CONSULT.items) || [];
  if (!nameOrSlug || !items.length) return [];
  const raw = String(nameOrSlug).trim().toLowerCase();
  const q = normTeacher(nameOrSlug);
  const base = surnameOf(q);
  return items.filter((c) => {
    const names = [c.teacher, ...(c.aliases || [])].map(normTeacher);
    return c.slug === raw || names.includes(q)
      || (base.length >= 3 && names.some((n) => n.startsWith(base + " ")));
  });
}
function fmtConsultSummary(slots) {
  return slots.map((c) => `${SHORT[c.day_num].toLowerCase()} ${c.start}–${c.end}`).join(", ");
}

/* ---------- компоненти ---------- */
function lessonHTML(it, state, dayUrl = "") {
  const c = it.cancelled ? "#8F1D21" : subjectColor(it.subject);
  const cls = ["lesson", "fade"];
  if (state === "now") cls.push("now");
  if (state === "done") cls.push("done");
  if (it.cancelled) cls.push("cancelled");
  const chip = it.type ? `<span class="chip ${it.type === "Лк" ? "lk" : "pr"}">${it.type === "Лк" ? "Лекція" : it.type === "Пр" ? "Практичне" : esc(it.type)}</span>` : "";
  const room = it.room ? (/^\d+$/.test(it.room) ? `ауд. ${esc(it.room)}` : esc(it.room)) : "";
  const grpChip = isTeacher() && it.group_label ? `<span class="chip grp">${esc(it.group_label)}</span>` : "";
  const info = isTeacher()
    ? [grpChip, it.choice ? "за вибором" : "", it.online ? "онлайн" : (room && room !== "потік" ? room : it.stream ? "потік" : "")].filter(Boolean).map((x) => x.startsWith("<span") ? x : `<span>${x}</span>`).join("")
    : [it.subgroup ? `${it.subgroup} підгр.` : it.choice ? "за вибором" : "", it.teacher ? esc(it.teacher) : "", it.online ? "онлайн" : room].filter(Boolean).map((x) => `<span>${x}</span>`).join("");
  let consultRow = "";
  if (!isTeacher() && !it.cancelled && it.teacher) {
    const cslots = consultSlotsForTeacher(it.teacher);
    if (cslots.length) {
      consultRow = `<button type="button" class="consult-hint" data-find-consult="${esc(cslots[0].teacher)}" aria-label="Консультація викладача ${esc(cslots[0].teacher)}: ${esc(fmtConsultSummary(cslots))}">${svg("clock")}<span>Консультація: ${esc(fmtConsultSummary(cslots))}</span></button>`;
    }
  }
  let note = "";
  if (it.cancelled) note = `<div class="note">${esc(it.cancel_note || "Вікно — пари немає")}</div>`;
  else if (it.changed) note = `<div class="note">Заміна${it.was ? ` · було: ${esc(it.was)}` : ""}</div>`;
  else if (it.doubt) note = `<div class="note warn">⚠ Можлива неточність у PDF — ${dayUrl ? `<a href="${esc(dayUrl)}" target="_blank" rel="noopener">звірте з офіційним розкладом</a>` : "звірте з офіційним розкладом"}</div>`;
  return `<article class="${cls.join(" ")}" style="--c:${c};--cs:${c}18">
    <div class="when"><span class="n">${it.pair}</span><span class="t">${esc(it.start)}</span></div>
    <div class="what">
      <div class="row1"><span class="ic">${svg(subjectIcon(it.subject))}</span><span class="name">${esc(it.subject)}</span></div>
      <div class="info">${chip}<span>${esc(it.start)}–${esc(it.end)}</span>${info}</div>${consultRow}${note}
    </div></article>`;
}

function dayListHTML(day, isToday) {
  const wd = dow(day.date);
  if ((wd === 0 || wd === 6) && !day.items.length) return `<div class="empty">${svg("coffee")}Вихідний — відпочивай</div>`;
  if (day.error) return `<div class="empty">${svg("file")}${esc(day.error)}<br><small>Щойно коледж його викладе — він з'явиться тут сам</small></div>`;
  if (!day.items.length) return `<div class="empty">${svg("check")}Пар немає</div>`;
  const now = kyivNow().sec;
  const out = [];
  day.items.forEach((it, k) => {
    let st = "";
    if (isToday && !it.cancelled) st = now >= toSec(it.end) ? "done" : now >= toSec(it.start) ? "now" : "";
    out.push(lessonHTML(it, st, day.url));
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
  if (day.error || !L.length) {
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
    const curWho = isTeacher() ? cur.group_label : cur.teacher;
    return `<section class="hero fade"><div class="label">Зараз · ${cur.pair} пара</div>
      <div class="big">${esc(cur.subject)}</div>
      <div class="meta">${[curWho, cur.room && /^\d+$/.test(cur.room) ? "ауд. " + cur.room : cur.room].filter(Boolean).map(esc).join(" · ")}</div>
      <div class="timer" aria-live="polite"><b data-countdown="${toSec(cur.end)}">${dur(toSec(cur.end) - now)}</b><span>до кінця</span></div>
      <div class="bar"><i style="width:${Math.min(100, (passed / total) * 100).toFixed(1)}%"></i></div>
      ${nxt ? `<div class="meta" style="margin-top:10px">Далі о ${esc(nxt.start)} — ${esc(nxt.subject)}${isTeacher() && nxt.group_label ? ` (${esc(nxt.group_label)})` : ""}</div>` : `<div class="meta" style="margin-top:10px">Це остання пара сьогодні</div>`}</section>`;
  }
  if (nxt) {
    const first = nxt === L[0];
    return `<section class="hero fade"><div class="label">${first ? "Перша пара" : "Наступна пара"} · ${nxt.pair}</div>
      <div class="big">${esc(nxt.subject)}</div>
      <div class="meta">о ${esc(nxt.start)}${isTeacher() && nxt.group_label ? ` · ${esc(nxt.group_label)}` : ""}${nxt.room && /^\d+$/.test(nxt.room) ? " · ауд. " + esc(nxt.room) : ""}</div>
      <div class="timer" aria-live="polite"><b data-countdown="${toSec(nxt.start)}">${dur(toSec(nxt.start) - now)}</b><span>до початку</span></div></section>`;
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

function availWeeks(today) {
  const curMon = mondayOf(today);
  const startMon = [0, 6].includes(dow(today)) ? addDays(curMon, 7) : curMon;
  const set = new Set([startMon]);
  for (const iso of Object.keys((SITE && SITE.days) || {})) set.add(mondayOf(iso));
  return { startMon, weeks: [...set].sort() };
}

function teacherConsultCardHTML() {
  if (!isTeacher()) return "";
  const slots = consultSlotsForTeacher(SITE.slug) || [];
  if (!slots.length) return "";
  return `<button type="button" class="consult-bar" data-find-consult="${esc(SITE.teacher)}" aria-label="Відпрацювання: ${esc(fmtConsultSummary(slots))}">
    <span class="cb-ic">${svg("clock")}</span>
    <span class="cb-body"><b>Консультація: ${esc(fmtConsultSummary(slots))}</b><small>Аудиторію уточнюйте у викладача · натисніть, щоб переглянути графік</small></span>
  </button>`;
}

function consultHTML() {
  const items = (CONSULT && CONSULT.items) || [];
  if (!items.length) return `<div class="section-title"><h2>Відпрацювання</h2></div><div class="empty">${svg("clock")}Графік консультацій ще завантажується або поки недоступний.</div>`;
  const kn = kyivNow();
  const todayWd = dow(kn.iso);
  const nowSec = kn.sec;
  const liveNow = items.filter((c) => c.day_num === todayWd && toSec(c.start) <= nowSec && nowSec < toSec(c.end));
  const laterToday = items.filter((c) => c.day_num === todayWd && toSec(c.start) > nowSec).sort((a, b) => toSec(a.start) - toSec(b.start));
  let liveBar = "";
  if (liveNow.length) {
    const nextOne = laterToday[0];
    const maxEnd = liveNow.map((c) => c.end).sort().pop();
    const meta = nextOne ? `Далі: ${nextOne.teacher} о ${nextOne.start}` : `До ${maxEnd} · аудиторію уточнюйте у викладача`;
    liveBar = `<div class="consult-live fade" role="status"><span class="chip live">● зараз</span><div class="cl-body"><b>Консультують ${liveNow.length} ${pluralTeachers(liveNow.length)}</b><small>${esc(meta)}</small></div></div>`;
  }

  const qRaw = CONSULT_Q.trim();
  const qNorm = normSubj(qRaw);
  const qLow = qRaw.toLowerCase();
  let shown;
  if (qLow) {
    shown = items.filter((c) =>
      c.teacher.toLowerCase().includes(qLow)
      || (c.full_name || "").toLowerCase().includes(qLow)
      || (c.aliases || []).some((a) => a.toLowerCase().includes(qLow))
      || (c.subjects || []).some((s) => s.toLowerCase().includes(qLow) || (qNorm && normSubj(s).includes(qNorm)))
    );
  } else if (CONSULT_MODE === "today") {
    shown = items.filter((c) => c.day_num === todayWd);
  } else if (CONSULT_MODE === "mine") {
    shown = myConsultSlots(items, SITE);
  } else {
    shown = items;
  }

  const seg = `<div class="seg seg-3" role="tablist" aria-label="Фільтр відпрацювань">
    <button type="button" role="tab" aria-selected="${CONSULT_MODE === "today"}" tabindex="${CONSULT_MODE === "today" ? 0 : -1}" data-cmode="today" class="${CONSULT_MODE === "today" ? "active" : ""}">Сьогодні</button>
    <button type="button" role="tab" aria-selected="${CONSULT_MODE === "week"}" tabindex="${CONSULT_MODE === "week" ? 0 : -1}" data-cmode="week" class="${CONSULT_MODE === "week" ? "active" : ""}">Тиждень</button>
    <button type="button" role="tab" aria-selected="${CONSULT_MODE === "mine"}" tabindex="${CONSULT_MODE === "mine" ? 0 : -1}" data-cmode="mine" class="${CONSULT_MODE === "mine" ? "active" : ""}">Мої предмети</button>
  </div>`;
  const search = `<div class="search-wrap"><label class="sr-only" for="consult-q">Пошук консультацій за предметом або викладачем</label>
    <input type="search" id="consult-q" class="search-input" value="${esc(CONSULT_Q)}" placeholder="Предмет або викладач, напр. «Теорія права»…" autocomplete="off"></div>`;
  const secTitle = qLow
    ? `<div class="section-title"><h2>Результати пошуку</h2><span>${shown.length}</span></div>`
    : CONSULT_MODE === "today"
      ? `<div class="section-title"><h2>${DAYS[todayWd] || "Сьогодні"}, ${ddmm(kn.iso)}</h2><span>${shown.length}</span></div>`
      : CONSULT_MODE === "mine"
        ? `<div class="section-title"><h2>${isTeacher() ? "Мої консультації" : "Мої викладачі (" + esc(SITE.group) + ")"}</h2><span>${shown.length}</span></div>`
        : `<div class="section-title"><h2>Увесь тиждень</h2><span>${shown.length}</span></div>`;

  const cards = shown.length
    ? `<div class="list">${shown.map((c) => {
        const isLive = c.day_num === todayWd && toSec(c.start) <= nowSec && nowSec < toSec(c.end);
        const showDay = qLow || CONSULT_MODE !== "today";
        return `<article class="lesson fade ${isLive ? "now" : ""}">
          <div class="when consult-slot ${isLive ? "live" : ""}">
            <span class="n">${showDay ? SHORT[c.day_num] : esc(c.start)}</span>
            <span class="t">${showDay ? `${esc(c.start)}–${esc(c.end)}` : esc(c.end)}</span>
          </div>
          <div class="what">
            <div class="row1"><span class="name">${esc(c.teacher)}</span>${isLive ? `<span class="chip live">● зараз</span>` : ""}</div>
            <div class="info"><span>${esc((c.subjects || []).join("; "))}</span></div>
            <div class="muted" style="font-size:.78rem;margin-top:4px">${esc(c.day)} ${esc(c.start)}–${esc(c.end)} · аудиторію уточнюйте у викладача</div>
          </div>
        </article>`;
      }).join("")}</div>`
    : `<div class="empty">${svg("clock")}${qLow ? `Нічого не знайдено за запитом «${esc(qRaw)}»` : CONSULT_MODE === "today" ? "Сьогодні консультацій немає — відкрийте «Тиждень» або «Мої предмети»" : "Не знайдено консультацій для обраного фільтра"}</div>`;

  const srcCard = (CONSULT.sources || []).length
    ? `<div class="section-title"><h2>Офіційні графіки (PDF)</h2></div><div class="card">${CONSULT.sources.map((s) =>
        `<a class="link-row" href="${esc(s.url)}" target="_blank" rel="noopener">${svg("file")}<div>${esc(s.title)}<small>nlu.edu.ua</small></div></a>`).join("")}</div>`
    : "";

  return liveBar + search + seg + secTitle + cards + srcCard;
}

/* ---------- вкладки ---------- */
function renderTabsState() {
  document.querySelectorAll(".tabbar button").forEach((b) => {
    const active = b.dataset.tab === TAB;
    b.classList.toggle("active", active);
    b.setAttribute("aria-selected", active ? "true" : "false");
    b.setAttribute("tabindex", active ? "0" : "-1");
  });
}

function wireConsultHints(container) {
  if (!container || !container.querySelectorAll) return;
  container.querySelectorAll("[data-find-consult]").forEach((btn) => {
    btn.addEventListener("click", () => {
      CONSULT_Q = btn.dataset.findConsult || "";
      CONSULT_MODE = "week";
      TAB = "consult";
      history.replaceState(null, "", location.pathname + location.search + "#consult");
      render();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  });
}

function render() {
  renderTabsState();
  const today = kyivNow().iso;
  const v = $("#view");
  if (TAB === "today") {
    const daySeg = `<div class="seg" role="tablist" aria-label="День розкладу">
      <button type="button" role="tab" aria-selected="${DAY_MODE === "today"}" tabindex="${DAY_MODE === "today" ? 0 : -1}" data-dmode="today" class="${DAY_MODE === "today" ? "active" : ""}">Сьогодні</button>
      <button type="button" role="tab" aria-selected="${DAY_MODE === "tomorrow"}" tabindex="${DAY_MODE === "tomorrow" ? 0 : -1}" data-dmode="tomorrow" class="${DAY_MODE === "tomorrow" ? "active" : ""}">Завтра</button>
    </div>`;
    if (DAY_MODE === "tomorrow") {
      const iso = nextSchoolDay(1);
      const d = findDay(iso);
      const L = live(d);
      const title = iso === addDays(today, 1) ? "Завтра" : "Наступний навчальний день";
      const nP = new Set(L.map((x) => x.pair)).size;
      const summary = nP ? `${nP} ${pluralPairs(nP)} · ${L[0].start}–${L[L.length - 1].end}` : "";
      const pdfLnk = d.url ? ` · <a href="${esc(d.url)}" target="_blank" rel="noopener">PDF ↗</a>` : "";
      v.innerHTML = daySeg + `<div class="section-title"><h2>${title}</h2><span>${DAYS[dow(iso)]}, ${ddmm(iso)}${pdfLnk}</span></div>` +
        (summary ? `<p class="muted" style="margin:0 2px 10px">${summary}</p>` : "") + dayListHTML(d, false) + teacherConsultCardHTML();
    } else {
      const d = findDay(today);
      const pdfLnk = d.url ? ` · <a href="${esc(d.url)}" target="_blank" rel="noopener">PDF ↗</a>` : "";
      v.innerHTML = daySeg + heroHTML(d) + `<div class="section-title"><h2>Сьогодні</h2><span>${DAYS[dow(today)]}, ${ddmm(today)}${pdfLnk}</span></div>` + dayListHTML(d, true) + teacherConsultCardHTML();
    }
    v.querySelectorAll?.("[data-dmode]").forEach((b) => b.addEventListener("click", () => { DAY_MODE = b.dataset.dmode; render(); }));
    wireConsultHints(v);
  } else if (TAB === "week") {
    const { startMon, weeks } = availWeeks(today);
    if (!WEEK_MON || !weeks.includes(WEEK_MON)) WEEK_MON = startMon;
    const wi = weeks.indexOf(WEEK_MON);
    const week = [0, 1, 2, 3, 4].map((i) => addDays(WEEK_MON, i));
    if (!WEEK_SEL || !week.includes(WEEK_SEL)) WEEK_SEL = week.includes(today) ? today : week[0];
    const wd = findDay(WEEK_SEL);
    const nP = new Set(live(wd).map((x) => x.pair)).size;
    const pdfLnk = wd.url ? ` · <a href="${esc(wd.url)}" target="_blank" rel="noopener">PDF ↗</a>` : "";
    v.innerHTML = `<div class="section-title"><h2>Тиждень</h2>
      <div class="week-nav">
        ${WEEK_MON !== startMon ? `<button type="button" class="nav-pill" data-wjump="${startMon}" aria-label="Повернутися на поточний тиждень">Цей тиждень</button>` : ""}
        <div class="week-capsule">
          <button type="button" class="cap-btn" data-wstep="-1" ${wi <= 0 ? "disabled" : ""} aria-label="Попередній тиждень">‹</button>
          <span class="week-range">${ddmm(week[0])}–${ddmm(week[4])}</span>
          <button type="button" class="cap-btn" data-wstep="1" ${wi >= weeks.length - 1 ? "disabled" : ""} aria-label="Наступний тиждень">›</button>
        </div>
      </div></div>
      <div class="days" role="tablist" aria-label="Дні тижня">${week.map((iso) => `<button type="button" role="tab" aria-selected="${iso === WEEK_SEL}" tabindex="${iso === WEEK_SEL ? 0 : -1}" aria-label="${DAYS[dow(iso)]}, ${ddmm(iso)}" data-day="${iso}" class="${iso === WEEK_SEL ? "active" : ""} ${iso === today ? "today" : ""}">${SHORT[dow(iso)]}<small>${ddmm(iso)}</small></button>`).join("")}</div>
      <div class="section-title"><h2>${DAYS[dow(WEEK_SEL)]}</h2><span>${nP} ${pluralPairs(nP)}${pdfLnk}</span></div>` + dayListHTML(wd, WEEK_SEL === today) + teacherConsultCardHTML();
    v.querySelectorAll?.("[data-day]").forEach((b) => b.addEventListener("click", () => { WEEK_SEL = b.dataset.day; render(); }));
    v.querySelectorAll?.("[data-wstep]").forEach((b) => b.addEventListener("click", () => {
      const nextIdx = wi + (+b.dataset.wstep);
      if (nextIdx >= 0 && nextIdx < weeks.length) {
        WEEK_MON = weeks[nextIdx];
        const nextWeek = [0, 1, 2, 3, 4].map((i) => addDays(WEEK_MON, i));
        WEEK_SEL = nextWeek.includes(today) ? today : nextWeek[0];
        render();
      }
    }));
    const jbtn = v.querySelector?.("[data-wjump]");
    if (jbtn) jbtn.addEventListener("click", () => { WEEK_MON = jbtn.dataset.wjump; WEEK_SEL = null; render(); });
    wireConsultHints(v);
  } else if (TAB === "consult") {
    v.innerHTML = consultHTML();
    v.querySelectorAll?.("[data-cmode]").forEach((b) => b.addEventListener("click", () => {
      CONSULT_MODE = b.dataset.cmode;
      render();
    }));
    const cq = $("#consult-q");
    if (cq) {
      cq.addEventListener("input", () => {
        const pos = cq.selectionStart;
        CONSULT_Q = cq.value;
        render();
        const nextQ = $("#consult-q");
        if (nextQ) { nextQ.focus(); if (typeof pos === "number") nextQ.setSelectionRange(pos, pos); }
      });
    }
  } else if (TAB === "changes") {
    const rows = SITE.changes.filter((z) => z.date >= addDays(today, -7)).sort((a, b) => (a.date + a.pair).localeCompare(b.date + b.pair));
    v.innerHTML = `<div class="section-title"><h2>Заміни</h2><span>${esc(isTeacher() ? SITE.teacher : SITE.group)}</span></div>` +
      (rows.length ? `<div class="list">${rows.map((z) => {
        const removed = isTeacher() && z.role === "removed";
        const win = removed ? z.to === "вікно" : z.subject.toLowerCase() === "вікно";
        const headTxt = removed ? `${esc(z.subject)} — ${win ? "вікно" : "заміна → " + esc(z.to || "")}` : win ? "Вікно — пари немає" : esc(z.subject);
        const subTxt = isTeacher()
          ? [z.group, z.kind, z.room ? "ауд. " + z.room : ""].filter(Boolean).map(esc).join(" · ")
          : [z.teacher, z.kind, z.group !== SITE.group ? z.group : ""].filter(Boolean).map(esc).join(" · ");
        return `<div class="change ${win || removed ? "win" : ""} ${z.date < today ? "past" : ""} fade"><div class="d">${ddmm(z.date)}<small>${SHORT[dow(z.date)]} · ${z.pair} п.</small></div>
          <div class="body"><b>${headTxt}</b><span>${subTxt}</span></div></div>`;
      }).join("")}</div>` : `<div class="empty">${svg("check")}Замін немає</div>`) +
      `<p class="muted" style="margin:12px 2px">Минулі заміни показуються блідими ще тиждень.</p>`;
  } else if (TAB === "pick") {
    v.innerHTML = pickerHTML();
    v.querySelectorAll?.("[data-pmode]").forEach((b) => b.addEventListener("click", () => { PICK_MODE = b.dataset.pmode; render(); }));
    const bindPick = () => v.querySelectorAll?.("[data-group]").forEach((b) => b.addEventListener("click", () => selectGroup(b.dataset.group)));
    bindPick();
    const q = $("#teacher-q");
    if (q) {
      q.addEventListener("input", () => {
        $("#teacher-list").innerHTML = teacherCardsHTML(q.value);
        bindPick();
      });
    }
    return;
  } else {
    const ics = `ics/${SITE.slug}.ics`;
    const webcal = new URL(ics, location.href).href.replace(/^https?:/, "webcal:");
    const pdfs = INDEX.pdfs.filter((p) => p.kind === "changes" || (isTeacher() ? (SITE.courses || []).includes(p.course) : p.course === SITE.course));
    const nowLabel = isTeacher() ? esc(SITE.teacher) : `${esc(SITE.group)}, ${SITE.course} курс`;
    const botDesc = isTeacher() ? `Розклад ${esc(SITE.teacher)} по всіх курсах і заміни одразу` : `Розклад ${esc(SITE.group)} щоранку й заміни одразу`;
    const tMode = currentThemeMode();
    const tLabel = tMode === "dark" ? "Темна («нічна лаванда»)" : tMode === "light" ? "Світла («лаванда коледжу»)" : `Системна (${resolvedTheme() === "dark" ? "темна" : "світла"})`;
    v.innerHTML = `<div class="section-title"><h2>Ще</h2></div>
      <div class="card">
        <a class="link-row" href="#pick" id="change-group">${svg("users")}<div>${isTeacher() ? "Змінити викладача або групу" : "Змінити групу"}<small>Зараз: ${nowLabel}</small></div></a>
        <a class="link-row" href="#theme" id="theme-cycle">${svg(resolvedTheme() === "dark" ? "sun" : "moon")}<div>Тема оформлення<small>${esc(tLabel)} · натисни, щоб змінити</small></div></a>
        ${INDEX.bot ? `<a class="link-row" href="https://t.me/${esc(INDEX.bot)}?start=${esc(SITE.slug)}" target="_blank" rel="noopener">${svg("bot")}<div>Telegram-бот<small>${botDesc}</small></div></a>` : ""}
        <a class="link-row" href="${esc(webcal)}">${svg("cal")}<div>Підписатися в календарі<small>Google Calendar / iPhone — оновлюється сам</small></div></a>
        <a class="link-row" href="${esc(ics)}" download>${svg("file")}<div>Завантажити .ics<small>Разовий імпорт у календар</small></div></a>
        <a class="link-row" href="#" id="install">${svg("phone")}<div>Встановити як застосунок<small id="install-hint">Іконка на головному екрані телефону</small></div></a>
        <a class="link-row" href="${esc(INDEX.source_page)}" target="_blank" rel="noopener">${svg("ext")}<div>Сторінка розкладу коледжу<small>nlu.edu.ua</small></div></a>
      </div>
      <div class="section-title"><h2>PDF на сайті коледжу</h2></div>
      <div class="card">${pdfs.map((p) => `<a class="link-row" href="${esc(p.url)}" target="_blank" rel="noopener">${svg("file")}<div>${esc(p.title)}</div></a>`).join("") || '<p class="muted">Немає</p>'}</div>`;
    $("#change-group").addEventListener("click", (e) => { e.preventDefault(); PICK_MODE = isTeacher() ? "teacher" : "student"; TAB = "pick"; render(); });
    $("#theme-cycle").addEventListener("click", (e) => {
      e.preventDefault();
      const cur = currentThemeMode();
      applyTheme(cur === "auto" ? (resolvedTheme() === "dark" ? "light" : "dark") : cur === "light" ? "dark" : "auto");
      render();
    });
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
let SW_REG = null;
if ("serviceWorker" in navigator) {
  const hadController = !!navigator.serviceWorker.controller;
  let reloaded = false;
  navigator.serviceWorker.addEventListener?.("controllerchange", () => {
    if (hadController && !reloaded) { reloaded = true; location.reload(); }
  });
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("sw.js").then((reg) => { SW_REG = reg; }).catch(() => {});
  });
}

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

/* ---------- вибір групи або викладача ---------- */
function teacherCardsHTML(query = "") {
  const q = String(query || "").trim().toLowerCase();
  const list = (INDEX.teachers || []).filter((t) =>
    !q || t.name.toLowerCase().includes(q)
    || (t.aliases || []).some((a) => a.toLowerCase().includes(q))
    || (t.subjects || []).some((s) => s.toLowerCase().includes(q))
  );
  if (!list.length) return `<div class="empty">${svg("users")}Нікого не знайдено за «${esc(query)}»</div>`;
  return `<div class="days pick teachers">${list.map((t) =>
    `<button type="button" data-group="${esc(t.slug)}" aria-label="Обрати викладача ${esc(t.name)}" class="${SITE && SITE.slug === t.slug ? "active" : ""}"><span>${esc(t.name)}</span><small>${esc((t.subjects || []).slice(0, 2).join(" · ") || t.degree || "Викладач")}</small></button>`
  ).join("")}</div>`;
}

function pickerHTML() {
  const hasT = (INDEX.teachers || []).length > 0;
  const seg = hasT ? `<div class="seg" role="tablist" aria-label="Режим розкладу">
    <button type="button" data-pmode="student" role="tab" aria-selected="${PICK_MODE === "student"}" tabindex="${PICK_MODE === "student" ? 0 : -1}" class="${PICK_MODE === "student" ? "active" : ""}">👥 Студент</button>
    <button type="button" data-pmode="teacher" role="tab" aria-selected="${PICK_MODE === "teacher"}" tabindex="${PICK_MODE === "teacher" ? 0 : -1}" class="${PICK_MODE === "teacher" ? "active" : ""}">🎓 Викладач</button>
  </div>` : "";
  if (hasT && PICK_MODE === "teacher") {
    return `${seg}<div class="section-title"><h2>Обери викладача</h2><span>${INDEX.teachers.length}</span></div>
      <div class="search-wrap"><label class="sr-only" for="teacher-q">Пошук викладача</label>
      <input type="search" id="teacher-q" class="search-input" placeholder="Пошук за прізвищем або дисципліною…" autocomplete="off"></div>
      <div id="teacher-list">${teacherCardsHTML()}</div>`;
  }
  return `${seg}<div class="section-title"><h2>Обери свою групу</h2></div>` + Object.entries(INDEX.courses).map(([c, gs]) =>
    `<div class="section-title"><h2>${esc(c)} курс</h2></div><div class="days pick">${gs.map((g) =>
      `<button type="button" data-group="${esc(g.name)}" aria-label="Обрати групу ${esc(g.name)}" class="${SITE && SITE.group === g.name && !isTeacher() ? "active" : ""}">${esc(g.name)}${g.room ? `<small>${esc(g.room)} ауд.</small>` : ""}</button>`).join("")}</div>`).join("");
}

function targetByNameOrSlug(name) {
  const raw = String(name || "").trim();
  if (!raw) return null;
  for (const gs of Object.values(INDEX.courses || {})) {
    for (const g of gs) if (g.name === raw || g.slug === raw) return { ...g, kind: "group" };
  }
  const low = raw.toLowerCase();
  const firstWord = low.split(/\s+/)[0];
  const t = (INDEX.teachers || []).find((x) =>
    x.name === raw || x.slug === low || x.name.toLowerCase() === low
    || (x.aliases || []).some((a) => a.toLowerCase() === low)
  ) || (firstWord.length >= 3 ? (INDEX.teachers || []).find((x) =>
    x.name.toLowerCase().startsWith(firstWord + " ")
    || (x.aliases || []).some((a) => a.toLowerCase().startsWith(firstWord + " "))
  ) : null);
  return t ? { ...t, kind: "teacher" } : null;
}

function syncSiteStatus() {
  if (!SITE) return;
  const today = kyivNow().iso;
  const banner = $("#banner");
  const weekDays = [0, 1, 2, 3, 4].map((i) => addDays(mondayOf(today), i));
  if (banner) {
    if (!weekDays.some((d) => SITE.days[d])) { banner.innerHTML = "Розклад на цей тиждень ще не опубліковано — щойно коледж його викладе, він з'явиться тут сам."; banner.hidden = false; }
    else banner.hidden = true;
  }
  const dot = $("#changes-dot");
  if (dot) dot.hidden = !SITE.changes.some((z) => z.date >= today);
}

function updateFooterMeta() {
  if (!INDEX) return;
  const ch = $("#checked");
  if (ch) {
    ch.textContent = INDEX.updated ? `Дані оновлено ${INDEX.updated.slice(8, 10)}.${INDEX.updated.slice(5, 7)} о ${INDEX.updated.slice(11, 16)}` : "";
    const down = INDEX.source_down_since;
    if (down) ch.textContent += ` · ⚠️ сайт коледжу недоступний з ${down.slice(8, 10)}.${down.slice(5, 7)} ${down.slice(11, 16)}, показано останній отриманий розклад`;
  }
  const src = $("#src");
  if (src) src.href = INDEX.source_page;
}

let LAST_CHECK = 0;
let CHECKING = false;
async function checkUpdates(force = false) {
  if (CHECKING || !INDEX || (!force && (document.hidden || Date.now() - LAST_CHECK < 60000))) return false;
  CHECKING = true;
  LAST_CHECK = Date.now();
  try {
    const r = await fetch("data/index.json?" + Date.now(), { cache: "no-store" });
    if (!r.ok) return false;
    const fresh = await r.json();
    const hashChanged = Boolean(fresh.hash && fresh.hash !== INDEX.hash);
    const metaChanged = fresh.updated !== INDEX.updated || fresh.source_down_since !== INDEX.source_down_since;
    if (hashChanged || metaChanged) {
      INDEX = fresh;
      updateFooterMeta();
    }
    if (!hashChanged) return false;
    const v = encodeURIComponent(fresh.hash);
    const reqs = [
      fetch(`data/consultations.json?${v}`, { cache: "no-store" }).then((x) => (x.ok ? x.json() : null)).catch(() => null),
      SITE ? fetch(`data/${isTeacher() ? "t" : "g"}/${SITE.slug}.json?${v}`, { cache: "no-store" }).then((x) => (x.ok ? x.json() : null)).catch(() => null) : Promise.resolve(null),
    ];
    const [cons, grp] = await Promise.all(reqs);
    if (cons) CONSULT = cons;
    if (grp) SITE = grp;
    syncSiteStatus();
    const sr = $("#sr-status");
    if (sr) sr.textContent = "Дані розкладу оновлено";
    render();
    return true;
  } catch {
    return false;
  } finally {
    CHECKING = false;
  }
}

async function selectGroup(name) {
  const g = targetByNameOrSlug(name);
  if (!g) { TAB = "pick"; render(); return; }
  const isT = g.kind === "teacher" || String(g.slug || "").startsWith("t-");
  const path = isT ? `data/t/${g.slug}.json` : `data/g/${g.slug}.json`;
  try {
    SITE = await (await fetch(`${path}?` + (INDEX.hash || Date.now()), { cache: "no-cache" })).json();
  } catch (e) {
    $("#view").innerHTML = `<div class="empty">${svg("file")}Не вдалося завантажити розклад. Перевір інтернет і онови сторінку.</div>`;
    return;
  }
  WEEK_MON = null;
  WEEK_SEL = null;
  localStorage.setItem("group", isT ? g.slug : g.name);
  const u = new URL(location.href);
  if (isT) { u.searchParams.delete("g"); u.searchParams.set("t", SITE.teacher); }
  else { u.searchParams.delete("t"); u.searchParams.set("g", SITE.group); }
  history.replaceState(null, "", u.pathname + u.search + location.hash);
  if (isT) {
    $("#title").textContent = SITE.teacher;
    document.title = `Розклад · ${SITE.teacher} · ${INDEX.short || "ПФК НЮУ"}`;
    const subParts = [SITE.degree, "Викладач", (SITE.courses || []).length ? `${SITE.courses.join(", ")} курс` : ""].filter(Boolean);
    $("#subtitle").textContent = subParts.join(" · ");
  } else {
    $("#title").textContent = `Розклад ${SITE.group}`;
    document.title = `Розклад ${SITE.group} · ${INDEX.short || "ПФК НЮУ"}`;
    $("#subtitle").textContent = `${SITE.course} курс · ${INDEX.short || "ПФК НЮУ"}${SITE.room ? ` · ${SITE.room} ауд.` : ""}`;
  }
  $("#ics").href = `ics/${SITE.slug}.ics`;
  syncSiteStatus();
  if (TAB === "pick") TAB = "today";
  render();
}

function wireTablistKeyboard() {
  document.addEventListener("keydown", (e) => {
    const btn = e.target && e.target.closest ? e.target.closest('[role="tab"]') : null;
    if (!btn) return;
    const list = btn.closest('[role="tablist"]');
    if (!list) return;
    const tabs = [...list.querySelectorAll('[role="tab"]')];
    const i = tabs.indexOf(btn);
    if (i < 0) return;
    let next = -1;
    if (e.key === "ArrowRight") next = (i + 1) % tabs.length;
    else if (e.key === "ArrowLeft") next = (i - 1 + tabs.length) % tabs.length;
    else if (e.key === "Home") next = 0;
    else if (e.key === "End") next = tabs.length - 1;
    if (next >= 0) {
      e.preventDefault();
      tabs[next].click();
      tabs[next].focus();
    }
  });
}

async function init() {
  try {
    const [idx, cons] = await Promise.all([
      fetch("data/index.json?" + Date.now(), { cache: "no-store" }).then((r) => r.json()),
      fetch("data/consultations.json?" + Date.now(), { cache: "no-store" }).then((r) => (r.ok ? r.json() : null)).catch(() => null),
    ]);
    INDEX = idx;
    CONSULT = cons;
  } catch (e) {
    $("#view").innerHTML = `<div class="empty">${svg("file")}Не вдалося завантажити дані. Перевір інтернет і онови сторінку.</div>`;
    return;
  }
  updateFooterMeta();
  document.querySelectorAll(".tabbar button").forEach((b) => b.addEventListener("click", () => {
    if (!SITE) return;
    TAB = b.dataset.tab; history.replaceState(null, "", location.pathname + location.search + "#" + TAB); render(); window.scrollTo({ top: 0, behavior: "smooth" });
  }));
  wireTablistKeyboard();
  const brand = $("#brand-btn");
  if (brand) {
    brand.addEventListener("click", () => {
      PICK_MODE = isTeacher() ? "teacher" : "student";
      TAB = "pick";
      render();
      window.scrollTo({ top: 0, behavior: "smooth" });
    });
  }
  if (!["today", "week", "consult", "changes", "more"].includes(TAB)) TAB = "today";
  const qs = new URLSearchParams(location.search);
  const themeParam = qs.get("theme");
  applyTheme(["light", "dark"].includes(themeParam) ? themeParam : currentThemeMode());
  const tb = $("#theme-btn");
  if (tb) tb.addEventListener("click", () => {
    applyTheme(resolvedTheme() === "dark" ? "light" : "dark");
    if (TAB === "more") render();
  });
  const tParam = qs.get("t");
  const wanted = tParam || qs.get("g") || localStorage.getItem("group");
  if (tParam) PICK_MODE = "teacher";
  if (wanted && targetByNameOrSlug(wanted)) await selectGroup(wanted);
  else { TAB = "pick"; $("#subtitle").textContent = INDEX.short || "ПФК НЮУ"; render(); }
  setInterval(() => SITE && tick(), 20000);
  setInterval(() => { if (!document.hidden && SITE) checkUpdates(); }, 600000);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) {
      if (SITE) render();
      checkUpdates();
      SW_REG?.update?.().catch(() => {});
    }
  });
}
init();
