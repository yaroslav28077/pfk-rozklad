#!/usr/bin/env python3
"""Бот розкладу ПФК НЮУ для всіх груп і курсів.

  python scripts/bot.py run                 # усе разом (так запускає GitHub Actions щогодини)
  python scripts/bot.py run --mode morning  # примусово надіслати ранкову розсилку (тест)
  python scripts/bot.py show СОБ-11 [--date 2026-11-03]

Що робить `run`:
  1. перевіряє сторінку «Розклад занять» на plc.nlu.edu.ua, завантажує нові PDF і сам їх розбирає
     (розклад усіх курсів і «Зміни до розкладу») → data/sources/*.json;
  2. будує сайт: docs/data/index.json, docs/data/g/<група>.json, docs/ics/<група>.ics;
  3. розсилає в Telegram кожному підписнику розклад ЙОГО групи (список підписників — у Cloudflare Worker),
     о morning_hour — на сьогодні, о evening_hour — на завтра (у неділю — тиждень),
     а також одразу — нові заміни й новий розклад.

Змінні середовища: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (адмін: технічні помилки), SITE_URL.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parent))
import parse  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DATA, DOCS = ROOT / "data", ROOT / "docs"
SRC = DATA / "sources"
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
TZ = ZoneInfo(CFG.get("timezone", "Europe/Kyiv"))
PAGE = CFG["source_page"]
BELLS = {int(k): tuple(v) for k, v in CFG["bells"].items()}
UA = {"User-Agent": "Mozilla/5.0 (pfk-rozklad bot; +https://github.com)"}
SHORTD = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Нд"]
DAYN = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
NUM = "1️⃣ 2️⃣ 3️⃣ 4️⃣ 5️⃣ 6️⃣ 7️⃣ 8️⃣".split()
KEEP_DAYS = 21  # скільки днів тримати старі PDF після завершення їхнього періоду
TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
ADMINS = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
SITE = (os.environ.get("SITE_URL") or CFG.get("site_url") or "").rstrip("/") + "/"
WORKER_URL = (os.environ.get("WORKER_URL") or CFG.get("worker_url") or "").rstrip("/")


def log(*a):
    print(*a, file=sys.stderr)


def jload(p, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def jsave(p, obj, compact=False):
    p.parent.mkdir(parents=True, exist_ok=True)
    txt = json.dumps(obj, ensure_ascii=False, separators=(",", ":")) if compact else json.dumps(obj, ensure_ascii=False, indent=1)
    p.write_text(txt + "\n", encoding="utf-8")


def now():
    return datetime.now(TZ)


def today():
    return now().date()


TR = dict(zip("абвгґдеєжзиіїйклмнопрстуфхцчшщьюя", "a b v h g d e ie zh z y i i i k l m n o p r s t u f kh ts ch sh shch  iu ia".split(" ")))


def slug(name):
    s = "".join(TR.get(ch, ch) for ch in name.lower().replace("'", ""))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def secret():
    """Спільний секрет бота й воркера (виводиться з токена — окремо задавати не треба)."""
    return hashlib.sha256(f"pfk-rozklad:{TOKEN}".encode()).hexdigest()[:48]


# ---------------------------------------------------------------- сайт коледжу
def http(url, data=None, headers=None, timeout=60):
    req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


class Links(HTMLParser):
    def __init__(self):
        super().__init__(); self.links = []; self._h = None; self._t = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._h = dict(attrs).get("href"); self._t = []

    def handle_data(self, d):
        if self._h is not None:
            self._t.append(d)

    def handle_endtag(self, tag):
        if tag == "a" and self._h:
            self.links.append((" ".join("".join(self._t).split()), self._h)); self._h = None


def classify(text, href):
    name = urllib.parse.unquote(href).lower()
    if not name.split("?")[0].endswith(".pdf"):
        return None
    t = f"{text} {name}".lower()
    if "змін" in t:
        return "changes"
    if "розклад" in t and "курс" in t:
        return "schedule"
    return None


def page_url():
    p = urllib.parse.urlsplit(PAGE)
    return urllib.parse.urlunsplit((p.scheme, p.netloc, urllib.parse.quote(urllib.parse.unquote(p.path)), p.query, ""))


def update():
    """Завантажує нові/змінені PDF і розбирає їх. Повертає список подій для розсилки."""
    known = {p.stem: jload(p, {}) for p in SRC.glob("*.json")}
    bootstrap = not known
    html = http(page_url()).decode("utf-8", "replace")
    p = Links(); p.feed(html)
    events, on_page, seen = [], set(), set()
    for text, href in p.links:
        kind = classify(text, href)
        if not kind:
            continue
        url = urllib.parse.quote(urllib.parse.urljoin(page_url(), urllib.parse.unquote(href)), safe=":/?=&%#~+")
        if url in seen:
            continue
        seen.add(url)
        fname = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).stem
        key = re.sub(r"[^\w\-.]+", "_", fname)
        on_page.add(key)
        try:
            pdf = http(url)
        except Exception as e:  # noqa: BLE001
            log(f"ERR завантаження {fname}: {e}"); continue
        h = hashlib.sha256(pdf).hexdigest()
        old = known.get(key, {})
        if old.get("sha256") == h:
            continue
        rec = {"file": fname, "title": text or fname, "url": url, "kind": kind, "sha256": h,
               "first_seen": old.get("first_seen") or now().isoformat(timespec="minutes"),
               "updated": now().isoformat(timespec="minutes")}
        try:
            if kind == "schedule":
                rec["data"] = parse.parse_schedule(pdf, text)
                if not rec["data"]["items"]:
                    raise ValueError("не знайдено жодної пари")
            else:
                rec["data"] = parse.parse_changes(pdf)
        except Exception as e:  # noqa: BLE001
            rec["error"] = f"{type(e).__name__}: {e}"
            events.append({"type": "error", "text": f"Не вдалося розібрати PDF «{text}»: {rec['error']}", "url": url})
        jsave(SRC / f"{key}.json", rec)
        known[key] = rec
        if bootstrap or "error" in rec:
            continue
        if kind == "schedule":
            events.append({"type": "schedule", "course": rec["data"]["course"], "from": rec["data"]["from"],
                           "to": rec["data"]["to"], "title": rec["title"], "url": url, "changed": bool(old)})
        else:
            prev = {json.dumps(z, sort_keys=True, ensure_ascii=False) for z in old.get("data", [])}
            fresh = [z for z in rec["data"] if json.dumps(z, sort_keys=True, ensure_ascii=False) not in prev
                     and z["date"] >= today().isoformat()]
            if fresh:
                events.append({"type": "changes", "rows": fresh, "title": rec["title"], "url": url})
    if not seen:
        events.append({"type": "error", "text": "На сторінці розкладу не знайдено жодного PDF — можливо, змінився сайт коледжу", "url": PAGE})
    # прибираємо старі PDF, яких уже немає на сайті
    limit = (today() - timedelta(days=KEEP_DAYS)).isoformat()
    for key, rec in list(known.items()):
        if key in on_page or not seen:
            continue
        d = rec.get("data")
        end = d.get("to") if isinstance(d, dict) else max((z["date"] for z in d or []), default="")
        if (end or "") < limit:
            (SRC / f"{key}.json").unlink(missing_ok=True); known.pop(key)
    for k, rec in known.items():
        rec["on_page"] = k in on_page
    return known, events


# ---------------------------------------------------------------- збірка даних по групах
def build(sources):
    schedules = sorted([s for s in sources.values() if s.get("kind") == "schedule" and "data" in s],
                       key=lambda s: (s["first_seen"], s["data"]["from"]))
    changes_src = [s for s in sources.values() if s.get("kind") == "changes" and "data" in s]
    groups, by_course_date = {}, {}
    for s in schedules:  # пізніше опублікований PDF перекриває попередній на ті ж дати
        d = s["data"]
        c = d.get("course") or 0
        for g in d["groups"]:
            groups[g["name"]] = {**groups.get(g["name"], {}), **g, "course": c, "seen": d["to"]}
        day = date.fromisoformat(d["from"])
        while day.isoformat() <= d["to"]:
            by_course_date[(c, day.isoformat())] = s
            day += timedelta(days=1)
    # групи з найновіших PDF кожного курсу (щоб розформовані групи зникали зі списку)
    latest = {}
    for g in groups.values():
        latest[g["course"]] = max(latest.get(g["course"], ""), g["seen"])
    groups = {n: g for n, g in groups.items() if g["seen"] == latest[g["course"]]}
    glist = list(groups.values())
    out = {n: {"group": n, "course": g["course"], "room": g.get("room", ""), "slug": slug(n), "days": {}, "changes": []}
           for n, g in groups.items()}
    for (c, iso), s in by_course_date.items():
        if date.fromisoformat(iso).weekday() == 6:
            continue
        for n in out:
            if groups[n]["course"] != c:
                continue
            items = [dict(it) for it in s["data"]["items"] if it["group"] == n and it["date"] == iso]
            for it in items:
                it.pop("group"); it.pop("date")
                it.setdefault("room", "потік" if it.get("stream") else groups[n].get("room", ""))
            if date.fromisoformat(iso).weekday() == 5 and not items:
                continue
            out[n]["days"][iso] = {"items": items, "src": s["title"]}
    for s in changes_src:
        for z in s["data"]:
            for n in parse.change_targets(z["group"], glist):
                if n in out:
                    out[n]["changes"].append({**z, "src": s["title"], "url": s["url"]})
    for n, g in out.items():
        uniq = {(z["date"], z["pair"], z["subject"]): z for z in g["changes"]}
        g["changes"] = sorted(uniq.values(), key=lambda z: (z["date"], z["pair"]))
        for z in g["changes"]:
            day = g["days"].setdefault(z["date"], {"items": [], "src": ""})
            same = [it for it in day["items"] if it["pair"] == z["pair"]]
            st, en = (same[0]["start"], same[0]["end"]) if same else BELLS.get(z["pair"], ("", ""))
            if z["subject"].lower().strip() == "вікно":
                for it in same:  # «вікно» там, де пари й так немає, — нічого не змінює
                    it["cancelled"] = True
            else:
                kind = (z.get("kind") or "").lower()
                new = {"pair": z["pair"], "start": st, "end": en, "subject": z["subject"], "teacher": z.get("teacher", ""),
                       "type": "Пр" if "практ" in kind else "Лк" if "лек" in kind else "", "changed": True,
                       "room": z.get("room") or (same[0].get("room", "") if same else g["room"])}
                if same:
                    new["was"] = " / ".join(dict.fromkeys(it["subject"] for it in same))
                day["items"] = [it for it in day["items"] if it["pair"] != z["pair"]] + [new]
            day["items"].sort(key=lambda it: (it["pair"], it.get("subgroup", 0)))
        g["days"] = dict(sorted(g["days"].items()))
    pdfs = [{"title": s["title"], "url": s["url"], "kind": s["kind"],
             "course": (s.get("data") or {}).get("course") if s["kind"] == "schedule" else None}
            for s in sorted(sources.values(), key=lambda s: s.get("title", "")) if s.get("on_page")]
    return out, pdfs


def write_site(out, pdfs, bot_name):
    """docs/data/* і docs/ics/*. Повертає хеш вмісту (щоб не деплоїти сайт без змін)."""
    gdir, idir = DOCS / "data" / "g", DOCS / "ics"
    for d in (gdir, idir):
        d.mkdir(parents=True, exist_ok=True)
        for f in d.iterdir():
            f.unlink()
    courses = {}
    for n, g in sorted(out.items(), key=lambda kv: kv[1]["course"]):  # у межах курсу — порядок стовпців PDF
        courses.setdefault(str(g["course"]), []).append({"name": n, "slug": g["slug"], "room": g["room"]})
        jsave(gdir / f"{g['slug']}.json", g, compact=True)
        build_ics(g, idir / f"{g['slug']}.ics")
    index = {"college": CFG["college"], "short": CFG.get("short", ""), "bot": bot_name, "source_page": PAGE,
             "bells": CFG["bells"], "courses": courses, "pdfs": pdfs}
    h = hashlib.sha256(json.dumps([index, out], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    index.update({"hash": h, "updated": now().isoformat(timespec="minutes")})
    jsave(DOCS / "data" / "index.json", index)
    return h


def build_ics(g, target):
    stamp = now().astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    esc = lambda s: str(s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")  # noqa: E731
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//pfk-rozklad//UK", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
           f"X-WR-CALNAME:Розклад {g['group']}", "X-WR-TIMEZONE:Europe/Kyiv", "REFRESH-INTERVAL;VALUE=DURATION:PT6H",
           "BEGIN:VTIMEZONE", "TZID:Europe/Kyiv",
           "BEGIN:STANDARD", "DTSTART:19701025T040000", "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "TZOFFSETFROM:+0300", "TZOFFSETTO:+0200", "TZNAME:EET", "END:STANDARD",
           "BEGIN:DAYLIGHT", "DTSTART:19700329T030000", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0300", "TZNAME:EEST", "END:DAYLIGHT",
           "END:VTIMEZONE"]
    for iso, day in g["days"].items():
        d = iso.replace("-", "")
        for it in day["items"]:
            if it.get("cancelled") or not it.get("start"):
                continue
            uid = hashlib.md5(f"{g['group']}{iso}{it['pair']}{it.get('subgroup', '')}{it['subject']}".encode()).hexdigest()
            title = f"{(it.get('type', '') + ' ' + it['subject']).strip()}{' (заміна)' if it.get('changed') else ''}"
            out += ["BEGIN:VEVENT", f"UID:{uid}@pfk-rozklad", f"DTSTAMP:{stamp}",
                    f"DTSTART;TZID=Europe/Kyiv:{d}T{it['start'].replace(':', '')}00",
                    f"DTEND;TZID=Europe/Kyiv:{d}T{it['end'].replace(':', '')}00",
                    f"SUMMARY:{esc(title)}", f"LOCATION:{esc('онлайн' if it.get('online') else it.get('room', ''))}",
                    f"DESCRIPTION:{esc(' · '.join(x for x in [it.get('teacher', ''), str(it['pair']) + ' пара'] if x))}", "END:VEVENT"]
    out.append("END:VCALENDAR")
    target.write_text("\r\n".join(out) + "\r\n", encoding="utf-8")


# ---------------------------------------------------------------- тексти повідомлень
def lesson_line(it):
    n = NUM[it["pair"] - 1] if 0 < it["pair"] <= len(NUM) else f"{it['pair']}."
    if it.get("cancelled"):
        return f"{n} <s>{escape(it.get('subject', ''))}</s> — <b>вікно</b>"
    t = (it.get("type", "") + " ").lstrip()
    extra = []
    if it.get("subgroup"):
        extra.append(f"{it['subgroup']} підгр.")
    if it.get("teacher"):
        extra.append(escape(it["teacher"]))
    if it.get("online"):
        extra.append("онлайн")
    mark = " 🔄" if it.get("changed") else ""
    return f"{n} {it['start']}–{it['end']}  <b>{escape(t + it['subject'])}</b>{mark}" + (f"\n      <i>{', '.join(extra)}</i>" if extra else "")


def mine(items, sg=0):
    """Підгрупа: спільні пари + пари своєї підгрупи (sg=0 — усі)."""
    return [i for i in items if not sg or not i.get("subgroup") or i["subgroup"] == sg]


def fmt_day(g, d, title, sg=0):
    head = f"📅 <b>{title}: {DAYN[d.weekday()]}, {d:%d.%m}</b> · {escape(g['group'])}"
    day = g["days"].get(d.isoformat())
    if day is not None:
        day = {**day, "items": mine(day["items"], sg)}
    if day is None:
        return f"{head}\n😴 Вихідний" if d.weekday() >= 5 else f"{head}\n⚠️ Розклад на цю дату ще не опубліковано"
    live = [i for i in day["items"] if not i.get("cancelled")]
    if not live and not day["items"]:
        return f"{head}\nПар немає 🎉"
    lines = [head, ""] + [lesson_line(it) for it in day["items"]]
    if any(i.get("changed") or i.get("cancelled") for i in day["items"]):
        lines.append("\n🔄 — заміна з «Змін до розкладу»")
    if live:
        lines.append(f"\n🏁 {live[0]['start']}–{live[-1]['end']} · {len(live)} {plural(len(live))}")
    return "\n".join(lines)


def plural(n):
    return "пара" if n % 10 == 1 and n % 100 != 11 else "пари" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "пар"


def fmt_week(g, monday, sg=0):
    lines = [f"🗓 <b>Тиждень {monday:%d.%m}–{monday + timedelta(days=4):%d.%m}</b> · {escape(g['group'])}"]
    for i in range(6):
        d = monday + timedelta(days=i)
        day = g["days"].get(d.isoformat())
        if day is None:
            if i < 5:
                lines.append(f"<b>{SHORTD[i]} {d:%d.%m}</b>: ⚠️ ще не опубліковано")
            continue
        live = [x for x in mine(day["items"], sg) if not x.get("cancelled")]
        if not live:
            lines.append(f"<b>{SHORTD[i]} {d:%d.%m}</b>: пар немає 🎉"); continue
        subj = "\n".join(f"  {x['pair']}. {escape(x['subject'])}{' 🔄' if x.get('changed') else ''}" for x in live)
        lines.append(f"<b>{SHORTD[i]} {d:%d.%m}</b> · {live[0]['start']}–{live[-1]['end']}\n{subj}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------- Telegram і воркер
def tg(method, payload):
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = json.loads(e.read().decode() or "{}")
            if e.code == 429 and attempt < 2:
                time.sleep(body.get("parameters", {}).get("retry_after", 3) + 1); continue
            return body
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                return {"ok": False, "description": str(e)}
            time.sleep(2)


class Worker:
    """Список підписників і «один раз» — живуть у Cloudflare Worker (KV). URL беремо з вебхука бота."""

    def __init__(self):
        self.url = WORKER_URL or None
        if TOKEN and not self.url:
            info = tg("getWebhookInfo", {}) or {}
            u = (info.get("result") or {}).get("url") or ""
            self.url = u.rstrip("/") or None

    def call(self, path, body=None):
        if not self.url:
            return None
        try:
            data = json.dumps(body).encode() if body is not None else None
            r = http(self.url + path, data=data, timeout=30,
                     headers={"Authorization": f"Bearer {secret()}", "Content-Type": "application/json"})
            return json.loads(r)
        except Exception as e:  # noqa: BLE001
            log(f"Воркер {path}: {e}")
            return None

    def users(self):
        r = self.call("/api/users")
        return r.get("users") if isinstance(r, dict) else None

    def once(self, key):
        """True, якщо ця подія ще не відбувалась (захист від дублів, коли GitHub запускає двічі)."""
        r = self.call("/api/once", {"key": key})
        return True if r is None else bool(r.get("first"))


def broadcast(users, make_text, kb=None):
    """make_text(user) → текст або None. Повертає id чатів, які заблокували бота."""
    gone, sent = [], 0
    for u in users:
        text = make_text(u)
        if not text:
            continue
        p = {"chat_id": u["id"], "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
             "disable_notification": bool(int(u.get("s") or 0))}
        if kb:
            p["reply_markup"] = kb(u)
        r = tg("sendMessage", p) or {}
        if r.get("ok"):
            sent += 1
        else:
            desc = r.get("description", "")
            log(f"…{str(u['id'])[-4:]}: {desc}")
            if r.get("error_code") in (400, 403) and re.search(r"blocked|deactivated|not found|kicked|PEER_ID", desc, re.I):
                gone.append(u["id"])
        time.sleep(0.05)
    log(f"Надіслано: {sent}")
    return gone


def site_kb(g):
    if not SITE.startswith("http") or not g:
        return None
    return {"inline_keyboard": [[{"text": "🌐 Сайт", "url": f"{SITE}?g={urllib.parse.quote(g['group'])}"},
                                 {"text": "📲 У календар", "url": f"{SITE}ics/{g['slug']}.ics"}]]}


def notify(out, events, mode):
    if not TOKEN:
        log("TELEGRAM_BOT_TOKEN не задано — розсилку пропущено")
        return
    w = Worker()
    users = w.users()
    if users is None:  # воркер ще не налаштовано — як раніше: шлемо в TELEGRAM_CHAT_ID для default_group
        log("Воркер недоступний — розсилка лише в TELEGRAM_CHAT_ID")
        users = [{"id": c, "g": CFG.get("default_group")} for c in ADMINS]
    else:
        known = {str(u["id"]) for u in users}
        for c in ADMINS:  # автоматично переносимо старий чат у список підписників
            if c not in known and w.once(f"migrate:{c}"):
                w.call("/api/add", {"id": c, "g": CFG.get("default_group")})
                users.append({"id": c, "g": CFG.get("default_group")})
    t, h = today(), now().hour
    gone = []
    G = lambda u: out.get(u.get("g") or "")  # noqa: E731
    kb = lambda u: site_kb(G(u))  # noqa: E731
    on = lambda u, k: int(u.get(k, 1) or 0)  # noqa: E731
    sg = lambda u: int(u.get("sg") or 0)  # noqa: E731

    def due(name, key, default):
        """Підписники, яким саме зараз час для розсилки `name` (у кожного своя година u[key]).
        GitHub інколи запускає із запізненням — тож вікно 2 години, а дублі відсікає воркер (once)."""
        if mode == name:
            return users
        if mode != "auto":
            return []
        res = []
        for slot in ((h, h - 1) if w.url else (h,)):
            batch = [u for u in users if int(u.get(key) or default) == slot]
            if batch and w.once(f"{name}:{t}:{slot}"):
                res += batch
        return res

    batch = due("morning", "mh", CFG["morning_hour"])
    if batch:
        gone += broadcast(batch, lambda u: G(u) and on(u, "m") and t.isoformat() in G(u)["days"]
                          and fmt_day(G(u), t, "Сьогодні", sg(u)), kb)
    batch = due("evening", "eh", CFG["evening_hour"])
    if batch:
        tm = t + timedelta(days=1)
        mon = t + timedelta(days=7 - t.weekday())

        def evening(u):
            g = G(u)
            if not g:
                return None
            if t.weekday() == 6 and on(u, "w"):  # неділя — огляд наступного тижня
                return fmt_week(g, mon, sg(u))
            if t.weekday() != 5 and on(u, "e") and tm.isoformat() in g["days"]:
                return fmt_day(g, tm, "Завтра", sg(u))
            return None
        gone += broadcast(batch, evening, kb)
    if mode == "weekly":
        mon = t - timedelta(days=t.weekday()) if t.weekday() < 5 else t + timedelta(days=7 - t.weekday())
        gone += broadcast(users, lambda u: G(u) and fmt_week(G(u), mon, sg(u)), kb)
    for e in events:
        if e["type"] == "changes":
            def txt(u, e=e):
                g = G(u)
                if not g or not on(u, "c"):
                    return None
                mine = [z for z in e["rows"] if g["group"] in parse.change_targets(z["group"], [{"name": g["group"], "course": g["course"]}])]
                if not mine:
                    return None
                rows = "\n".join(f"• {date.fromisoformat(z['date']):%d.%m} ({SHORTD[date.fromisoformat(z['date']).weekday()]}), {z['pair']} пара: "
                                 + ("<b>вікно</b>" if z["subject"].lower() == "вікно" else escape(z["subject"] + (f", {z['teacher']}" if z.get("teacher") else "")))
                                 for z in mine)
                return f"🔔 <b>Нові заміни</b> · {escape(g['group'])}\n\n{rows}"
            gone += broadcast(users, txt, kb)
        elif e["type"] == "schedule":
            d0 = date.fromisoformat(e["from"])
            mon = d0 - timedelta(days=d0.weekday())
            word = "Оновлено розклад" if e.get("changed") else "Новий розклад"
            gone += broadcast(users, lambda u, e=e: G(u) and G(u)["course"] == e["course"] and on(u, "c")
                              and f"🆕 <b>{word}</b> ({date.fromisoformat(e['from']):%d.%m}–{date.fromisoformat(e['to']):%d.%m})\n\n" + fmt_week(G(u), mon, sg(u)), kb)
        elif e["type"] == "error" and ADMINS:
            for c in ADMINS:
                tg("sendMessage", {"chat_id": c, "text": f"⚠️ {e['text']}\n{e.get('url', '')}"})
    if gone and w.url:
        w.call("/api/drop", {"ids": gone})


def ensure_webhook():
    """Направляє вебхук бота на воркер зі спільним секретом. Воркер розгортає Cloudflare Workers Builds
    при кожному пуші в main, а вебхук ставимо звідси — тож токен Cloudflare в GitHub не потрібен."""
    if not WORKER_URL:
        return
    # секрет вебхука з getWebhookInfo не прочитати, тож просто ставимо щоразу (це безпечно й дешево)
    r = tg("setWebhook", {"url": WORKER_URL, "secret_token": secret(),
                          "allowed_updates": ["message", "callback_query", "my_chat_member"]}) or {}
    log(f"Вебхук → {WORKER_URL}: {'ok' if r.get('ok') else r.get('description')}")


def setup_bot():
    """Меню команд і опис бота (раз на добу; BotFather вручну не потрібен)."""
    me = (tg("getMe", {}) or {}).get("result") or {}
    w = Worker()
    if w.url and not w.once(f"setup:{today()}"):
        return me.get("username", "")
    tg("setMyCommands", {"commands": [
        {"command": "today", "description": "Пари сьогодні"},
        {"command": "tomorrow", "description": "Пари завтра"},
        {"command": "week", "description": "Розклад на тиждень"},
        {"command": "changes", "description": "Заміни"},
        {"command": "group", "description": "Обрати / змінити групу"},
        {"command": "settings", "description": "Сповіщення"},
        {"command": "help", "description": "Допомога"}]})
    tg("setMyDescription", {"description": f"Розклад усіх груп {CFG.get('short', '')}: пари на сьогодні й завтра, тиждень, заміни. "
                                           "Натисни «Почати» й обери свою групу. Дані — з plc.nlu.edu.ua."})
    tg("setMyShortDescription", {"short_description": f"Розклад і заміни всіх груп · {CFG.get('short', '')}"})
    if SITE.startswith("https://"):  # кнопка «Розклад» біля поля вводу — сайт як Mini App
        tg("setChatMenuButton", {"menu_button": {"type": "web_app", "text": "Розклад", "web_app": {"url": SITE}}})
    return me.get("username", "")


# ---------------------------------------------------------------- команди
def deployed_hash():
    if not SITE.startswith("http"):
        return None
    try:
        return json.loads(http(SITE + "data/index.json?" + str(int(time.time())), timeout=20)).get("hash")
    except Exception:  # noqa: BLE001
        return None


def cmd_run(a):
    try:
        sources, events = update()
    except Exception as e:  # noqa: BLE001  сайт коледжу недоступний — працюємо з тим, що є
        log(f"Сайт коледжу недоступний: {e}")
        sources = {p.stem: jload(p, {}) for p in SRC.glob("*.json")}
        for k, rec in sources.items():
            rec["on_page"] = True
        events = []
    out, pdfs = build(sources)
    if TOKEN:
        ensure_webhook()
    bot_name = setup_bot() if TOKEN else ""
    h = write_site(out, pdfs, bot_name)
    changed = h != deployed_hash()
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write(f"deploy={'true' if changed or a.mode != 'auto' else 'false'}\n")
    log(f"Груп: {len(out)}, PDF: {len(pdfs)}, подій: {len(events)}, сайт {'змінився' if changed else 'без змін'}")
    notify(out, events, a.mode)


def cmd_show(a):
    sources = {p.stem: jload(p, {}) for p in SRC.glob("*.json")}
    for rec in sources.values():
        rec["on_page"] = True
    out, _ = build(sources)
    g = out.get(a.group) or next((v for k, v in out.items() if parse._norm(k) == parse._norm(a.group)), None)
    if not g:
        sys.exit(f"Групу не знайдено. Є: {', '.join(sorted(out))}")
    d = date.fromisoformat(a.date) if a.date else today()
    print(re.sub(r"<[^>]+>", "", fmt_day(g, d, "День")))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("--mode", default="auto", choices=["auto", "morning", "evening", "weekly", "none"])
    s = sub.add_parser("show"); s.add_argument("group"); s.add_argument("--date")
    a = ap.parse_args()
    {"run": cmd_run, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    main()
