const DAYS = ["Неділя", "Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота"];
let SITE = null;
let TAB = "today";

const $ = (s) => document.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

function kyivNow() {
  const p = new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Kyiv", year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).formatToParts(new Date());
  const g = (t) => p.find((x) => x.type === t).value;
  return { iso: `${g("year")}-${g("month")}-${g("day")}`, hm: `${g("hour") === "24" ? "00" : g("hour")}:${g("minute")}` };
}
function addDays(iso, n) { const d = new Date(iso + "T12:00:00Z"); d.setUTCDate(d.getUTCDate() + n); return d.toISOString().slice(0, 10); }
function dowName(iso) { return DAYS[new Date(iso + "T12:00:00Z").getUTCDay()]; }
function ddmm(iso) { const [, m, d] = iso.split("-"); return `${d}.${m}`; }

function pairHTML(it, nowHm, isToday) {
  const cls = ["pair"];
  if (it.changed) cls.push("changed");
  if (it.cancelled) cls.push("cancelled");
  if (isToday && !it.cancelled && it.start <= nowHm && nowHm <= it.end) cls.push("now");
  const tag = it.type ? `<span class="tag ${it.type === "Лк" ? "lk" : ""}">${esc(it.type)}</span>` : "";
  const meta = [it.teacher, it.room && /^\d+$/.test(it.room) ? `ауд. ${it.room}` : it.room].filter(Boolean).map(esc).join(" · ");
  let note = "";
  if (it.cancelled) note = `<div class="note">Вікно — пари немає (зміни до розкладу)</div>`;
  else if (it.changed) note = `<div class="note">Заміна${it.was ? ` (було: ${esc(it.was)})` : ""}</div>`;
  return `<div class="${cls.join(" ")}"><div class="num">${it.pair}</div><div>
    <div class="time">${esc(it.start)}–${esc(it.end)}</div>
    <div class="subj">${tag}${esc(it.subject)}</div>
    ${meta ? `<div class="meta">${meta}</div>` : ""}${note}</div></div>`;
}

function dayCard(day, title) {
  const now = kyivNow();
  const isToday = day.date === now.iso;
  const wd = new Date(day.date + "T12:00:00Z").getUTCDay();
  let body;
  if (day.error) body = `<div class="empty">⚠️ ${esc(day.error)}</div>`;
  else if (wd === 0 || wd === 6) body = `<div class="empty">😴 Вихідний</div>`;
  else if (!day.items.length) body = `<div class="empty">Пар немає 🎉</div>`;
  else body = day.items.map((it) => pairHTML(it, now.hm, isToday)).join("");
  return `<section class="day"><h2><span>${title || dowName(day.date)}</span><span class="date">${title ? dowName(day.date) + ", " : ""}${ddmm(day.date)}</span></h2>${body}</section>`;
}

function nextPairHTML(day) {
  if (!day || day.error) return "";
  const now = kyivNow().hm;
  const live = day.items.filter((i) => !i.cancelled);
  const cur = live.find((i) => i.start <= now && now <= i.end);
  const nxt = live.find((i) => i.start > now);
  if (cur) return `<div class="next">Зараз: <b>${esc(cur.subject)}</b><br>до ${esc(cur.end)}${nxt ? ` · далі ${esc(nxt.start)} — ${esc(nxt.subject)}` : " · це остання пара"}</div>`;
  if (nxt) return `<div class="next">Наступна пара о <b>${esc(nxt.start)}</b> — ${esc(nxt.subject)}</div>`;
  return "";
}

function findDay(iso) { return SITE.days.find((d) => d.date === iso) || { date: iso, items: [], error: "Немає даних на цю дату" }; }

function render() {
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === TAB));
  const now = kyivNow();
  const v = $("#view");
  if (TAB === "today") {
    const d = findDay(now.iso);
    v.innerHTML = nextPairHTML(d) + dayCard(d, "Сьогодні");
  } else if (TAB === "tomorrow") {
    let t = addDays(now.iso, 1);
    const wd = new Date(t + "T12:00:00Z").getUTCDay();
    if (wd === 6) t = addDays(t, 2); else if (wd === 0) t = addDays(t, 1);
    v.innerHTML = dayCard(findDay(t), t === addDays(now.iso, 1) ? "Завтра" : "Наступний навчальний день");
  } else if (TAB === "week") {
    const days = SITE.days.filter((d) => { const w = new Date(d.date + "T12:00:00Z").getUTCDay(); return w >= 1 && w <= 5; }).slice(0, 10);
    v.innerHTML = `<div class="week-grid">${days.map((d) => dayCard(d)).join("")}</div>`;
  } else if (TAB === "changes") {
    const rows = SITE.changes.filter((z) => z.date >= addDays(now.iso, -7));
    v.innerHTML = `<section class="day"><h2>Зміни до розкладу (${esc(SITE.group)} + весь курс)</h2>${rows.length ? `<div class="table-wrap"><table><tr><th>Дата</th><th>Пара</th><th>Група</th><th>Що</th></tr>${rows.map((z) => `<tr><td>${dowName(z.date).slice(0, 3)}, ${ddmm(z.date)}</td><td>${z.pair}</td><td>${esc(z.group)}</td><td>${z.subject === "вікно" ? "<b>вікно</b>" : `${esc(z.subject)}<br><small>${esc(z.teacher)} ${esc(z.kind)}</small>`}</td></tr>`).join("")}</table></div>` : `<div class="empty">Немає змін за останній тиждень і наперед.</div>`}</section>`;
  } else {
    v.innerHTML = `<section class="day"><h2>PDF на сайті коледжу</h2>${SITE.pdfs.map((p) => `<div class="pair" style="grid-template-columns:1fr"><a href="${esc(p.url)}" target="_blank" rel="noopener">${esc(p.title)}</a></div>`).join("") || '<div class="empty">Немає</div>'}</section>
    <section class="day"><h2>Колонка ${esc(SITE.group)} з останнього PDF</h2><img class="preview" src="novyi_rozklad.png" alt="Фрагмент PDF розкладу" loading="lazy"></section>`;
  }
}

async function init() {
  try {
    const r = await fetch("site.json?" + Date.now());
    SITE = await r.json();
  } catch (e) {
    $("#view").innerHTML = `<div class="day">Не вдалося завантажити дані. Спробуй пізніше.</div>`;
    return;
  }
  $("#title").textContent = `Розклад ${SITE.group}`;
  document.title = `Розклад ${SITE.group} · ПФК НЮУ`;
  const s = SITE.schedule;
  $("#subtitle").textContent = `${SITE.course} курс · ПФК НЮУ${s.from ? ` · діє ${ddmm(s.from)}–${ddmm(s.to)}` : ""}`;
  $("#checked").textContent = SITE.last_check ? `Сайт коледжу перевірено: ${SITE.last_check.replace("T", " ").slice(0, 16)}` : "";
  $("#src").href = SITE.source_page;
  const today = kyivNow().iso;
  const msgs = [];
  if (SITE.new_schedule) msgs.push(`🆕 На сайті коледжу новий розклад: «${esc(SITE.new_schedule.title)}». Тут поки старий — <a href="${esc(SITE.new_schedule.url)}" target="_blank" rel="noopener">відкрити PDF</a>.`);
  if (s.to && today > s.to) msgs.push(`⚠️ Розклад діяв до ${ddmm(s.to)}. Новий ще не внесено — дивись PDF на вкладці «PDF».`);
  if (msgs.length) { $("#banner").innerHTML = msgs.join("<br>"); $("#banner").classList.remove("hidden"); }
  document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => { TAB = b.dataset.tab; render(); }));
  render();
  setInterval(render, 60000);
}
init();
