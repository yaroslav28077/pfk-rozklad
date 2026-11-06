#!/usr/bin/env python3
"""Бот розкладу ПФК НЮУ для всіх груп і курсів.

  python scripts/bot.py run                 # усе разом: update + notify (локально)
  python scripts/bot.py update              # 1) отримати PDF, розібрати, зібрати сайт (GitHub Actions)
  python scripts/bot.py notify              # 2) розсилки й сповіщення адміну (після деплою сайту)
  python scripts/bot.py run --mode morning --only <chat id>   # тестова ранкова розсилка лише в один чат
  python scripts/bot.py show СОБ-11 [--date 2026-11-03]

Що робить `update`:
  1. перевіряє сторінку «Розклад занять» на plc.nlu.edu.ua (з повторними спробами), завантажує PDF і розбирає
     нові/змінені (розклад усіх курсів і «Зміни до розкладу») → data/sources/*.json.
     Якщо сайт/PDF недоступний або PDF не розібрався — лишається останній робочий розклад, а проблема
     потрапляє у звіт (адмін-повідомлення, червоний статус запуску). Невдалий PDF розбирається повторно
     на кожному запуску, навіть якщо файл не змінився;
  2. будує сайт: docs/data/index.json, docs/data/g/<група>.json, docs/ics/<група>.ics.
Що робить `notify`:
  3. розсилає в Telegram кожному підписнику розклад ЙОГО групи (список підписників — у Cloudflare Worker):
     зранку — на сьогодні, увечері — на завтра (у неділю — тиждень), одразу — нові заміни й новий розклад.
     Кожна розсилка позначається виконаною лише для тих, кому повідомлення справді дійшло (стан — у KV воркера),
     тож запізнілий чи повторний запуск не шле дублів, а невдалі відправки повторюються;
  4. повідомляє адміна (TELEGRAM_CHAT_ID) про збій і про відновлення — без повторів щогодини.

Змінні середовища: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (адмін: технічні помилки), SITE_URL, ROZKLAD_STATE.
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
from http.client import HTTPException
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
# мережа: обмежені повтори лише для тимчасових збоїв (тайм-аут, обрив, 5xx, 429)
RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
PAGE_TRY = (3, 30, (10, 30))   # спроб, тайм-аут (с), паузи між спробами (с) — сторінка розкладу
PDF_TRY = (3, 60, (5, 15))     # те саме для кожного PDF
SLEEP = time.sleep             # підміняється в тестах
# розсилки за розкладом: слот години h відправляється в будь-якому запуску від h до *_until (GitHub запізнюється)
MORNING_UNTIL = int(CFG.get("morning_until", 13))
EVENING_UNTIL = int(CFG.get("evening_until", 23))
SEND_TRIES = 4      # скільки запусків поспіль повторювати відправку тим, кому не дійшло
REMIND_HOURS = 24   # нагадування адміну про незакритий збій — не частіше
DEFAULTS = {"m": 1, "e": 1, "c": 1, "w": 1, "s": 0, "sg": 0, "mh": CFG.get("morning_hour", 6), "eh": CFG.get("evening_hour", 18)}


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


def stamp(t=None):
    return (t or now()).isoformat(timespec="minutes")


def hours_since(iso):
    try:
        return (now() - datetime.fromisoformat(iso)).total_seconds() / 3600
    except (TypeError, ValueError):
        return 1e9


def ddmm_hm(iso):
    try:
        return datetime.fromisoformat(iso).astimezone(TZ).strftime("%d.%m %H:%M")
    except (TypeError, ValueError):
        return str(iso)


TR = dict(zip("абвгґдеєжзиіїйклмнопрстуфхцчшщьюя", "a b v h g d e ie zh z y i i i k l m n o p r s t u f kh ts ch sh shch  iu ia".split(" ")))


def slug(name):
    s = "".join(TR.get(ch, ch) for ch in name.lower().replace("'", ""))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def secret():
    """Спільний секрет бота й воркера (виводиться з токена — окремо задавати не треба)."""
    return hashlib.sha256(f"pfk-rozklad:{TOKEN}".encode()).hexdigest()[:48]


# ---------------------------------------------------------------- сайт коледжу
def describe(e):
    """Коротко й зрозуміло: що саме сталося з запитом."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        e = e.reason
    if isinstance(e, TimeoutError) or "timed out" in str(e):
        return "тайм-аут (сервер не відповідає)"
    return f"{type(e).__name__}: {e}" if str(e) else type(e).__name__


def http(url, data=None, headers=None, timeout=60, tries=None):
    """GET/POST. tries=(спроб, тайм-аут, паузи) — повтор лише для тимчасових збоїв; 4xx повертається одразу."""
    attempts, timeout, pauses = tries or (1, timeout, ())
    for i in range(attempts):
        req = urllib.request.Request(url, data=data, headers={**UA, **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUS or i == attempts - 1:
                raise
            err = e
        except (OSError, HTTPException) as e:  # URLError, тайм-аут, обрив з'єднання, неповна відповідь
            if i == attempts - 1:
                raise
            err = e
        pause = pauses[min(i, len(pauses) - 1)] if pauses else 5
        log(f"  {describe(err)} — повтор {i + 2}/{attempts} через {pause} с")
        SLEEP(pause)


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


def load_sources():
    return {p.stem: jload(p, {}) for p in sorted(SRC.glob("*.json"))}


def parse_pdf(kind, pdf, title, old_data=None):
    """Розбір + перевірка на правдоподібність. Будь-яка помилка → PDF вважається нерозібраним."""
    if kind != "schedule":
        rows = parse.parse_changes(pdf)
        if not rows:
            raise ValueError("не знайдено жодного рядка замін")
        return rows
    d = parse.parse_schedule(pdf, title)
    if not d.get("items"):
        raise ValueError("не знайдено жодної пари")
    if not d.get("groups"):
        raise ValueError("не знайдено жодної групи")
    if d["from"] > d["to"]:
        raise ValueError(f"некоректний період {d['from']}…{d['to']}")
    if old_data and old_data.get("items") and len(d["items"]) < 0.3 * len(old_data["items"]):
        raise ValueError(f"знайдено лише {len(d['items'])} пар проти {len(old_data['items'])} у попередній версії — схоже на збій розбору")
    return d


def update():
    """Завантажує PDF і розбирає нові/змінені/раніше нерозібрані.

    Повертає (sources, events, problems, unchecked):
      problems  — [{id, text, url}] поточні збої (сайт, завантаження PDF, розбір PDF);
      unchecked — префікси id проблем, які цього разу неможливо було перевірити (щоб не оголосити їх «вирішеними»).
    Робочі дані ніколи не замінюються порожніми чи зламаними: при збої лишається остання вдала версія.
    """
    known = load_sources()
    bootstrap = not known
    events, problems = [], []
    try:
        html = http(page_url(), tries=PAGE_TRY).decode("utf-8", "replace")
    except Exception as e:  # noqa: BLE001
        problems.append({"id": "site", "text": f"Сайт коледжу недоступний: {describe(e)}", "url": PAGE})
        for rec in known.values():
            rec["on_page"] = True
        return known, events, problems, {"pdf:", "parse:", "nopdf"}
    p = Links(); p.feed(html)
    on_page, seen, unchecked = set(), set(), set()
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
        title = text or fname
        try:
            pdf = http(url, tries=PDF_TRY)
        except Exception as e:  # noqa: BLE001
            problems.append({"id": f"pdf:{key}", "text": f"Не вдалося завантажити PDF «{title}»: {describe(e)}", "url": url})
            unchecked.add(f"parse:{key}")
            continue
        h = hashlib.sha256(pdf).hexdigest()
        old = known.get(key, {})
        good = old.get("data") if "data" in old else None
        if good is not None and old.get("sha256") == h:  # уже розібрано, файл не змінився
            if old.pop("failed", None):  # коледж повернув робочу версію файлу
                jsave(SRC / f"{key}.json", old)
            continue
        # сюди потрапляє: новий файл, змінений файл, або файл, який минулого разу не розібрався (повтор)
        try:
            data = parse_pdf(kind, pdf, title, good if kind == "schedule" else None)
        except Exception as e:  # noqa: BLE001
            err = f"{type(e).__name__}: {e}"
            prev = old.get("failed") or ({"sha256": old.get("sha256"), "since": old.get("updated")} if "error" in old else {})
            same = prev.get("sha256") == h
            rec = {k: v for k, v in old.items() if k not in ("error", "failed")} if good is not None else {
                "file": fname, "title": title, "url": url, "kind": kind, "first_seen": old.get("first_seen") or stamp()}
            rec["failed"] = {"sha256": h, "error": err, "since": (prev.get("since") if same else None) or stamp(),
                             "attempts": (prev.get("attempts", 1) + 1) if same else 1, "title": title, "url": url}
            jsave(SRC / f"{key}.json", rec)
            known[key] = rec
            log(f"ERR розбору {fname} (спроба {rec['failed']['attempts']}): {err}")
            keep = " Показую попередню версію цього файлу." if good is not None else ""
            problems.append({"id": f"parse:{key}", "text": f"Не вдалося розібрати PDF «{title}»: {err}.{keep}", "url": url})
            continue
        rec = {"file": fname, "title": title, "url": url, "kind": kind, "sha256": h,
               "first_seen": old.get("first_seen") or stamp(), "updated": stamp(), "data": data}
        jsave(SRC / f"{key}.json", rec)
        known[key] = rec
        if bootstrap:
            continue
        ekey = f"{kind}:{key}:{h[:16]}"
        if kind == "schedule":
            if good == data:  # файл перезалили без змін у розкладі — не турбуємо людей
                continue
            events.append({"type": "schedule", "key": ekey, "course": data["course"], "from": data["from"],
                           "to": data["to"], "title": title, "url": url, "changed": good is not None})
        else:
            prev = {json.dumps(z, sort_keys=True, ensure_ascii=False) for z in good or []}
            fresh = [z for z in data if json.dumps(z, sort_keys=True, ensure_ascii=False) not in prev
                     and z["date"] >= today().isoformat()]
            if fresh:
                events.append({"type": "changes", "key": ekey, "rows": fresh, "title": title, "url": url})
    if not seen:
        problems.append({"id": "nopdf", "text": "На сторінці розкладу не знайдено жодного PDF — можливо, змінився сайт коледжу", "url": PAGE})
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
    return known, events, problems, unchecked


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


def write_site(out, pdfs, bot_name, status=None):
    """docs/data/* і docs/ics/*. Повертає хеш вмісту (щоб не деплоїти сайт без змін).
    status.source_down_since — з якого часу сайт коледжу недоступний (сайт покаже попередження)."""
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
             "bells": CFG["bells"], "courses": courses, "pdfs": pdfs,
             "source_down_since": (status or {}).get("source_down_since")}
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
    """Виклик Bot API. Завжди повертає dict (ok/description/error_code/parameters), ніколи не кидає виняток."""
    req = urllib.request.Request(f"https://api.telegram.org/bot{TOKEN}/{method}", json.dumps(payload).encode(),
                                 {"Content-Type": "application/json"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                body = json.loads(e.read().decode("utf-8", "replace") or "{}")
            except ValueError:  # 502 від проксі Telegram тощо — не JSON
                body = {}
            body = {"ok": False, "error_code": e.code, "description": f"HTTP {e.code}", **body}
            if attempt < 2 and (e.code == 429 or e.code >= 500):
                SLEEP(min(int((body.get("parameters") or {}).get("retry_after", 3)), 30) + 1); continue
            return body
        except Exception as e:  # noqa: BLE001
            if attempt == 2:
                return {"ok": False, "description": describe(e)}
            SLEEP(2)
    return {"ok": False, "description": "невідома помилка"}


MISSING = object()  # сховище стану недоступне / старий воркер без /api/state


class Worker:
    """Підписники (KV), стан розсилок і heartbeat — у Cloudflare Worker. URL беремо з config.json або вебхука."""

    def __init__(self):
        self.url = WORKER_URL or None
        self.errors = []
        if TOKEN and not self.url:
            info = tg("getWebhookInfo", {}) or {}
            u = (info.get("result") or {}).get("url") or ""
            self.url = u.rstrip("/") or None

    def call(self, path, body=None, quiet404=False):
        if not self.url:
            return None
        data = json.dumps(body).encode() if body is not None else None
        try:
            r = http(self.url + path, data=data, tries=(3, 30, (3, 10)),
                     headers={"Authorization": f"Bearer {secret()}", "Content-Type": "application/json"})
            return json.loads(r)
        except urllib.error.HTTPError as e:
            if e.code == 404 and quiet404:
                return MISSING
            self.errors.append(f"{path}: {describe(e)}")
        except Exception as e:  # noqa: BLE001
            self.errors.append(f"{path}: {describe(e)}")
        log(f"Воркер {self.errors[-1]}")
        return None

    def users(self):
        r = self.call("/api/users")
        return r.get("users") if isinstance(r, dict) else None

    def once(self, key):
        """Стара модель: True, якщо подія ще не відбувалася (ключ ставиться ДО відправки)."""
        r = self.call("/api/once", {"key": key})
        return None if r is None else bool(r.get("first"))

    def get(self, key):
        """Стан розсилки: dict (вже була), {} (ще не було) або MISSING (воркер недоступний чи старий)."""
        r = self.call("/api/state/get", {"keys": [key]}, quiet404=True)
        if r is MISSING or not isinstance(r, dict):
            return MISSING
        v = (r.get("values") or {}).get(key)
        return v if isinstance(v, dict) else {}

    def put(self, key, value, ttl):
        r = self.call("/api/state/set", {"key": key, "value": value, "ttl": ttl}, quiet404=True)
        return isinstance(r, dict) and r.get("ok")


def broadcast(users, make_text, kb=None):
    """make_text(user) → текст або None (не надсилати). Повертає {sent, failed, gone, moved}:
    failed — тимчасові збої (варто повторити), gone — бота заблокували / чату немає, moved — група стала супергрупою."""
    res = {"sent": [], "failed": [], "gone": [], "moved": {}, "rejected": []}
    for u in users:
        text = make_text(u)
        if not text:
            continue
        cid = u["id"]
        p = {"chat_id": cid, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True},
             "disable_notification": bool(int(u.get("s") or 0))}
        if kb and kb(u):
            p["reply_markup"] = kb(u)
        r = tg("sendMessage", p) or {}
        new_id = (r.get("parameters") or {}).get("migrate_to_chat_id")
        if not r.get("ok") and new_id:  # групу перетворили на супергрупу — шлемо на новий id і переносимо підписку
            res["moved"][str(cid)] = str(new_id)
            p["chat_id"] = new_id
            r = tg("sendMessage", p) or {}
        if r.get("ok"):
            res["sent"].append(str(cid))
        else:
            desc, code = r.get("description", ""), r.get("error_code")
            log(f"…{str(cid)[-4:]}: {code} {desc}")
            if code in (400, 403) and re.search(r"blocked|deactivated|not found|kicked|PEER_ID|chat_id is empty", desc, re.I):
                res["gone"].append(str(cid))
            elif code == 400:  # Telegram відхилив саме повідомлення — повтор не допоможе
                res["rejected"].append(str(cid))
            else:  # мережа, 429, 5xx — повторимо в наступному запуску
                res["failed"].append(str(cid))
        SLEEP(0.05)
    log(f"Надіслано: {len(res['sent'])}" + (f", не дійшло: {len(res['failed'])}" if res["failed"] else ""))
    return res


def site_kb(g):
    if not SITE.startswith("http") or not g:
        return None
    return {"inline_keyboard": [[{"text": "🌐 Сайт", "url": f"{SITE}?g={urllib.parse.quote(g['group'])}"},
                                 {"text": "📲 У календар", "url": f"{SITE}ics/{g['slug']}.ics"}]]}


class Delivery:
    """«Рівно один раз» для кожної розсилки (ключ), з повтором для тих, кому не дійшло.

    Стан у KV воркера: {"failed": [chat id], "tries": n}. Записується ПІСЛЯ відправки — тож збій посеред запуску
    не позначає розсилку виконаною. Зі старим воркером (без /api/state) — колишня поведінка через /api/once."""

    def __init__(self, w):
        self.w, self.total = w, {"sent": [], "failed": [], "gone": [], "moved": {}, "rejected": []}
        self.problems = []

    def _merge(self, r):
        for k in ("sent", "failed", "gone", "rejected"):
            self.total[k] += r[k]
        self.total["moved"].update(r["moved"])

    def run(self, key, users, make_text, kb, ttl=3 * 86400, force=False):
        tries = 0
        if not force and self.w.url:
            st = self.w.get(key)
            if st is MISSING:
                first = self.w.once(key)
                if first is None:  # воркер недоступний — краще пропустити зараз і надіслати в наступному запуску
                    self.problems.append(key)
                    return
                if not first:
                    return
                self._merge(broadcast(users, make_text, kb))
                return
            if st:
                left = {str(x) for x in st.get("failed") or []}
                tries = int(st.get("tries") or 1)
                if not left or tries >= SEND_TRIES:
                    return
                users = [u for u in users if str(u["id"]) in left]
        r = broadcast(users, make_text, kb)
        self._merge(r)
        if not force and self.w.url:
            if not self.w.put(key, {"failed": r["failed"], "tries": tries + 1, "at": stamp()}, ttl):
                log(f"Не вдалося зберегти стан розсилки {key} — можливий дубль у наступному запуску")
                self.problems.append(key)


def notify(out, events, mode, only=None, stale_since=None):
    """Розсилки. Повертає список технічних проблем (для звіту адміну)."""
    problems = []
    if not TOKEN:
        log("TELEGRAM_BOT_TOKEN не задано — розсилку пропущено")
        return problems
    w = Worker()
    users = w.users()
    if users is None and w.url:
        problems.append({"id": "worker", "text": "Не вдалося отримати список підписників з Cloudflare Worker: "
                         + "; ".join(w.errors[-2:]) + ". Розсилки цього запуску відкладено.", "url": w.url})
        return problems
    if users is None:  # воркер не налаштовано — як раніше: шлемо в TELEGRAM_CHAT_ID для default_group
        log("Воркер не налаштовано — розсилка лише в TELEGRAM_CHAT_ID")
        users = [{"id": c, "g": CFG.get("default_group")} for c in ADMINS]
    users = [{**DEFAULTS, **u} for u in users]
    if only:  # тестовий запуск: лише вказані чати (якщо чат не підписаний — з default_group)
        byid = {str(u["id"]): u for u in users}
        test_users = [byid.get(str(c)) or {**DEFAULTS, "id": c, "g": CFG.get("default_group")} for c in only]
    t, h = today(), now().hour
    D = Delivery(w)
    G = lambda u: out.get(u.get("g") or "")  # noqa: E731
    kb = lambda u: site_kb(G(u))  # noqa: E731
    on = lambda u, k: int(u.get(k, 1) or 0)  # noqa: E731
    sg = lambda u: int(u.get("sg") or 0)  # noqa: E731
    note = (f"\n\n<i>⚠️ Сайт коледжу зараз недоступний (з {ddmm_hm(stale_since)}), це останній отриманий розклад.</i>"
            if stale_since else "")
    glist = [{"name": n, "course": g["course"]} for n, g in out.items()]

    def scheduled(name, key, default, until, make):
        if mode == name:  # примусова (тестова) розсилка
            D.run(f"{name}:{t}:force", test_users if only else users, make, kb, force=True)
            return
        if mode != "auto":
            return
        if h >= until:  # надто пізно: ранкова о 15:00 чи вечірня після 23:00 вже не має сенсу
            return
        for slot in sorted({int(u.get(key) or default) for u in users}):
            # слот 06:00 обслуговує будь-який запуск 06:00…until (запуски GitHub запізнюються на години);
            # без воркера — лише запуск у ту саму годину, як раніше
            if not (slot <= h if w.url else slot == h):
                continue
            batch = [u for u in users if int(u.get(key) or default) == slot]
            D.run(f"{name}:{t}:{slot}", batch, make, kb)

    def morning(u):
        g = G(u)
        if not g or not on(u, "m") or t.isoformat() not in g["days"]:
            return None
        return fmt_day(g, t, "Сьогодні", sg(u)) + note

    tm = t + timedelta(days=1)
    next_mon = t + timedelta(days=7 - t.weekday())

    def evening(u):
        g = G(u)
        if not g:
            return None
        if t.weekday() == 6:  # неділя — огляд наступного тижня, якщо його вже опубліковано
            if on(u, "w") and any((next_mon + timedelta(days=i)).isoformat() in g["days"] for i in range(6)):
                return fmt_week(g, next_mon, sg(u)) + note
            if on(u, "w"):
                return None
        if t.weekday() != 5 and on(u, "e") and tm.isoformat() in g["days"]:
            return fmt_day(g, tm, "Завтра", sg(u)) + note
        return None

    scheduled("morning", "mh", DEFAULTS["mh"], MORNING_UNTIL, morning)
    scheduled("evening", "eh", DEFAULTS["eh"], EVENING_UNTIL, evening)
    if mode == "weekly":
        mon = t - timedelta(days=t.weekday()) if t.weekday() < 5 else next_mon
        D.run(f"weekly:{t}:force", test_users if only else users, lambda u: G(u) and fmt_week(G(u), mon, sg(u)) + note, kb, force=True)
    for e in events:
        if e["type"] == "changes":
            targets = [(z, set(parse.change_targets(z["group"], glist))) for z in e["rows"]]

            def txt(u, targets=targets):
                g = G(u)
                if not g or not on(u, "c"):
                    return None
                mine_rows = [z for z, gs in targets if g["group"] in gs]
                if not mine_rows:
                    return None
                rows = "\n".join(f"• {date.fromisoformat(z['date']):%d.%m} ({SHORTD[date.fromisoformat(z['date']).weekday()]}), {z['pair']} пара: "
                                 + ("<b>вікно</b>" if z["subject"].lower() == "вікно" else escape(z["subject"] + (f", {z['teacher']}" if z.get("teacher") else "")))
                                 for z in mine_rows)
                return f"🔔 <b>Нові заміни</b> · {escape(g['group'])}\n\n{rows}"
            D.run("ev:" + e["key"], users, txt, kb, ttl=14 * 86400)
        elif e["type"] == "schedule":
            d0 = date.fromisoformat(e["from"])
            mon = d0 - timedelta(days=d0.weekday())
            word = "Оновлено розклад" if e.get("changed") else "Новий розклад"
            D.run("ev:" + e["key"], users, lambda u, e=e, mon=mon, word=word: G(u) and G(u)["course"] == e["course"] and on(u, "c")
                  and f"🆕 <b>{word}</b> ({date.fromisoformat(e['from']):%d.%m}–{date.fromisoformat(e['to']):%d.%m})\n\n" + fmt_week(G(u), mon, sg(u)),
                  kb, ttl=14 * 86400)
    tot = D.total
    if w.url and tot["gone"]:
        w.call("/api/drop", {"ids": tot["gone"]})
    for old, new in tot["moved"].items():
        if w.url:
            w.call("/api/move", {"from": old, "to": new}, quiet404=True)
    if D.problems:
        problems.append({"id": "state", "text": "Сховище стану розсилок у воркері недоступне: " + "; ".join(w.errors[-2:])
                         + ". Частину розсилок відкладено до наступного запуску.", "url": w.url or ""})
    if tot["failed"]:
        log(f"::warning::Не дійшло {len(tot['failed'])} повідомлень — повтор у наступному запуску")
    return problems


# ---------------------------------------------------------------- стан збоїв і адмін-повідомлення
def health_path():
    return DATA / "health.json"


def report(problems, unchecked=(), send=True):
    """Порівнює поточні збої з попереднім запуском: адміну — про новий збій, нагадування раз на REMIND_HOURS
    і повідомлення про відновлення. Стан — data/health.json (комітиться). Не вдалося надіслати → повтор наступного разу."""
    old = jload(health_path(), {}).get("issues", {})
    cur, fresh, remind, fixed = {}, [], [], []
    for p in problems:
        o = old.get(p["id"]) or {}
        if o.get("resolved"):  # збій повернувся раніше, ніж ми встигли повідомити про відновлення
            o = {k: v for k, v in o.items() if k != "resolved"}
        # текст першої помилки не переписуємо щоразу (тайм-аут/обрив чергуються) — інакше коміт data/ щогодини
        st = {"text": o.get("text") or p["text"], "url": p.get("url", ""), "since": o.get("since") or stamp(), "alerted": o.get("alerted")}
        if not st["alerted"]:
            fresh.append(st)
        elif hours_since(st["alerted"]) >= REMIND_HOURS:
            remind.append(st)
        cur[p["id"]] = st
    for pid, o in old.items():
        if pid in cur:
            continue
        if any(pid == x or (x.endswith(":") and pid.startswith(x)) for x in unchecked):  # не перевірялося — лишаємо як було
            cur[pid] = o
        elif o.get("alerted"):
            fixed.append((pid, o))
    lines = []
    if fresh:
        lines += ["⚠️ <b>Розклад ПФК: проблема</b>"] + [f"• {escape(s['text'])}" + (f"\n  {escape(s['url'])}" if s["url"] else "") for s in fresh]
    if remind:
        lines += ["⏳ <b>Досі не вирішено</b>"] + [f"• {escape(s['text'])} (з {ddmm_hm(s['since'])})" for s in remind]
    if fresh or remind:
        lines.append("<i>Бот і сайт показують останній успішно отриманий розклад. Нагадаю не раніше ніж через "
                     f"{REMIND_HOURS} год, а коли запрацює — напишу.</i>")
    if fixed:
        def back(pid, o):
            hrs = max(1, round(hours_since(o.get("since"))))
            what = {"site": "Сайт коледжу знову доступний", "worker": "Воркер знову відповідає",
                    "state": "Сховище стану розсилок знову працює", "nopdf": "PDF на сторінці розкладу знову знайдено"}.get(pid)
            if not what:
                what = ("PDF знову завантажується" if pid.startswith("pdf:") else "PDF розібрано") + f": {o.get('text', '').split('«')[-1].split('»')[0]}"
            return f"• {escape(what)} (збій тривав ~{hrs} год, з {ddmm_hm(o.get('since'))})"
        lines += ["✅ <b>Розклад ПФК: відновлено</b>"] + [back(pid, o) for pid, o in fixed]
    delivered = False
    tried = bool(lines and send and TOKEN and ADMINS)
    if tried:
        text = "\n".join(lines)
        for c in ADMINS:
            r = tg("sendMessage", {"chat_id": c, "text": text, "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}})
            delivered = delivered or bool(r.get("ok"))
            if not r.get("ok"):
                log(f"Адмін-повідомлення не дійшло: {r.get('description')}")
    elif lines:
        log("Адмін-повідомлення (не надіслано — немає TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID):\n" + re.sub(r"<[^>]+>", "", "\n".join(lines)))
    if delivered:
        for s in fresh + remind:
            s["alerted"] = stamp()
    elif tried:  # про відновлення не вдалося повідомити — лишаємо запис, спробуємо наступного разу
        for pid, o in fixed:
            cur[pid] = {**o, "resolved": True}
    new = {"issues": cur}
    if new != jload(health_path(), {}):
        jsave(health_path(), new)
    return cur


def issue_since(pid, problems):
    """Час початку збою (з попередніх запусків), якщо він триває зараз."""
    if not any(p["id"] == pid for p in problems):
        return None
    return (jload(health_path(), {}).get("issues", {}).get(pid) or {}).get("since") or stamp()


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
    if w.url and w.once(f"setup:{today()}") is False:
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


def wait_deployed(h, timeout=240):
    """Після деплою GitHub Pages чекаємо, поки сайт справді віддає нові дані (воркер бере дані звідти)."""
    for _ in range(max(1, timeout // 15)):
        if deployed_hash() == h:
            log("Сайт уже віддає нові дані")
            return True
        SLEEP(15)
    log("::warning::Сайт ще не віддає нові дані (кеш/деплой Pages) — кнопки в боті можуть кілька хвилин показувати старе")
    return False


def state_file(a):
    return Path(getattr(a, "state", None) or os.environ.get("ROZKLAD_STATE") or (ROOT / ".rozklad-state.json"))


def gh_output(**kv):
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def summary(lines):
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def cmd_update(a):
    sources, events, problems, unchecked = update()
    out, pdfs = build(sources)
    status = {"source_down_since": issue_since("site", problems)}
    if not out:  # нічого не розібралося — не затираємо робочий сайт порожнім
        problems.append({"id": "empty", "text": "Після розбору не лишилося жодної групи — сайт не оновлено", "url": PAGE})
        h, changed = None, False
    else:
        if TOKEN:
            ensure_webhook()
        bot_name = setup_bot() if TOKEN else ""
        h = write_site(out, pdfs, bot_name, status)
        changed = h != deployed_hash()
    deploy = bool(out) and (changed or a.mode != "auto")
    state = {"events": events, "problems": problems, "unchecked": sorted(unchecked), "hash": h, "deploy": deploy,
             "stale_since": status["source_down_since"]}
    jsave(state_file(a), state)
    gh_output(deploy="true" if deploy else "false", degraded="true" if problems else "false")
    log(f"Груп: {len(out)}, PDF: {len(pdfs)}, подій: {len(events)}, проблем: {len(problems)}, сайт {'змінився' if changed else 'без змін'}")
    for p in problems:
        log(f"::error::{p['text']}")
    return state


def cmd_notify(a, state=None):
    state = state or jload(state_file(a), None)
    if state is None:
        sys.exit("Немає файла стану від `update` — спершу запусти update")
    out, _ = build(load_sources())
    if state.get("events") and state.get("deploy") and os.environ.get("PAGES_DEPLOYED") == "success" and state.get("hash"):
        wait_deployed(state["hash"])
    only = [c.strip() for c in (a.only or "").split(",") if c.strip()]
    problems = list(state.get("problems") or [])
    problems += notify(out, state.get("events") or [], a.mode, only=only, stale_since=state.get("stale_since"))
    report(problems, state.get("unchecked") or [])
    heartbeat(problems)
    gh_output(degraded="true" if problems else "false")
    lines = ["### Розклад ПФК", f"- подій (новий розклад / заміни): {len(state.get('events') or [])}"]
    lines += [f"- ❌ {p['text']}" for p in problems] or ["- ✅ збоїв немає"]
    summary(lines)
    if problems:
        log("Перевірка завершилась зі збоями: " + "; ".join(p["text"] for p in problems))
    return problems


def heartbeat(problems):
    """Сигнал «запуск відбувся» для зовнішнього контролю (cron воркера) — він напише адміну, якщо запуски зникнуть."""
    if not TOKEN:
        return
    w = Worker()
    if w.url:
        run = (f"{os.environ.get('GITHUB_SERVER_URL', 'https://github.com')}/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{os.environ['GITHUB_RUN_ID']}"
               if os.environ.get("GITHUB_RUN_ID") and os.environ.get("GITHUB_REPOSITORY") else "")
        w.call("/api/heartbeat", {"admins": ADMINS, "degraded": bool(problems), "run": run,
                                  "repo": os.environ.get("GITHUB_REPOSITORY", "")}, quiet404=True)


def cmd_run(a):
    state = cmd_update(a)
    problems = cmd_notify(a, state)
    if problems and a.strict:
        sys.exit(2)


def cmd_show(a):
    sources = load_sources()
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
    modes = ["auto", "morning", "evening", "weekly", "none"]
    for name in ("run", "update", "notify"):
        r = sub.add_parser(name)
        r.add_argument("--mode", default="auto", choices=modes)
        r.add_argument("--state", help="файл стану між update і notify (типово $ROZKLAD_STATE)")
        r.add_argument("--only", default="", help="лише для morning/evening/weekly: chat id тестового чату (через кому)")
        r.add_argument("--strict", action="store_true", help="run: код виходу 2, якщо були збої")
    s = sub.add_parser("show"); s.add_argument("group"); s.add_argument("--date")
    a = ap.parse_args()
    {"run": cmd_run, "update": cmd_update, "notify": cmd_notify, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    main()
