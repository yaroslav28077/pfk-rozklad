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
UK_ORDER = {ch: i for i, ch in enumerate("АБВГҐДЕЄЖЗИІЇЙКЛМНОПРСТУФХЦЧШЩЬЮЯ")}
TEACHER_ALIASES = {"ХАРЧНКО": "Харченко"}
TEACHER_DEG_RE = re.compile(r"^\s*((?:(?:[а-яіїє]{1,4}\.){1,4}\s*)+)", re.I)
TEACHER_NAME_RE = re.compile(r"^([А-ЯІЇЄҐ][а-яіїєґ'’ʼ\-]+),?\s+([А-ЯІЇЄҐ])\.\s*([А-ЯІЇЄҐ])\.?\s*$")


def uk_sort_key(s):
    return tuple((0, UK_ORDER[ch]) if ch in UK_ORDER else (1, ord(ch)) for ch in str(s or "").upper())


def _lev1(a, b):
    """True, якщо відстань Левенштейна між двома рядками <= 1."""
    if a == b:
        return True
    la, lb = len(a), len(b)
    if abs(la - lb) > 1:
        return False
    if la == lb:
        return sum(x != y for x, y in zip(a, b)) <= 1
    if la > lb:
        a, b = b, a
    i = j = diffs = 0
    while i < len(a) and j < len(b):
        if a[i] != b[j]:
            diffs += 1
            if diffs > 1:
                return False
            j += 1
        else:
            i += 1
            j += 1
    return True


def parse_teacher_raw(raw):
    """'к.п.н. Тараненко К.С.' / 'Бородай С.О' → (канонічне_ПІБ, ступінь, ключ)."""
    s = " ".join(parse._apos(str(raw or "")).split()).strip(" ,;")
    if not s:
        return None
    deg = ""
    md = TEACHER_DEG_RE.match(s)
    if md and md.end() < len(s):
        rest = s[md.end():].strip()
        if re.match(r"^[А-ЯІЇЄҐ]", rest):
            deg = " ".join(md.group(1).split())
            s = rest
    m = TEACHER_NAME_RE.match(s)
    if m:
        sur, i1, i2 = m.group(1), m.group(2), m.group(3)
        sur = TEACHER_ALIASES.get(sur.upper(), sur)
        init = f"{i1}.{i2}."
        return f"{sur} {init}", deg, (sur, init)
    parts = s.split()
    sur = TEACHER_ALIASES.get(parts[0].upper(), parts[0])
    init = " ".join(parts[1:])
    return f"{sur} {init}".strip(), deg, (sur, init)


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
    if "змін" in t or "zmin" in t:
        return "changes"
    if ("розклад" in t and "курс" in t) or ("rozklad" in t and "kurs" in t):
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


def _strip_doubt(it):
    return {k: v for k, v in it.items() if k not in ("doubt", "choice")}


def _items_signature(data):
    if not isinstance(data, dict):
        return data
    return {k: ([_strip_doubt(it) for it in v] if k == "items" else v)
            for k, v in data.items() if k not in ("doubts", "parser")}


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
        if good is not None and old.get("sha256") == h and old.get("parser") == parse.VERSION:  # уже розібрано актуальним парсером
            if old.pop("failed", None):  # коледж повернув робочу версію файлу
                jsave(SRC / f"{key}.json", old)
            continue
        # сюди потрапляє: новий файл, змінений файл, зміна версії парсера, або файл, який минулого разу не розібрався (повтор)
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
        rec = {"file": fname, "title": title, "url": url, "kind": kind, "sha256": h, "parser": parse.VERSION,
               "first_seen": old.get("first_seen") or stamp(), "updated": stamp(), "data": data}
        jsave(SRC / f"{key}.json", rec)
        known[key] = rec
        if bootstrap:
            continue
        ekey = f"{kind}:{key}:{h[:16]}"
        if kind == "schedule":
            if good is not None and _items_signature(good) == _items_signature(data):  # без змін у парах (або лише doubts/version) — не турбуємо людей
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
        elif (seen and rec.get("kind") == "schedule" and "data" in rec and rec.get("parser") != parse.VERSION
              and (end or "") >= today().isoformat() and rec.get("url", "").startswith("https://nlu.edu.ua/")):
            try:
                pdf = http(rec["url"], tries=PDF_TRY)
                data = parse_pdf("schedule", pdf, rec.get("title", key), rec["data"])
                rec = {**rec, "sha256": hashlib.sha256(pdf).hexdigest(), "parser": parse.VERSION, "updated": stamp(), "data": data}
                jsave(SRC / f"{key}.json", rec)
                known[key] = rec
            except Exception:  # noqa: BLE001
                pass
    for k, rec in known.items():
        rec["on_page"] = k in on_page
    return known, events, problems, unchecked


# ---------------------------------------------------------------- збірка даних по групах і сумніви
def resolutions_path():
    return DATA / "resolutions.json"


def sync_resolutions():
    """Підтягує відповіді адміна з KV воркера в data/resolutions.json і дописує локальні рішення у KV."""
    res = jload(resolutions_path(), {})
    if not TOKEN:
        return res
    w = Worker()
    if not w.url:
        return res
    kv = w.get("resolutions")
    if not isinstance(kv, dict):
        return res
    changed, push_kv = False, False
    for did, val in kv.items():
        if did not in res or (val.get("at", "") > (res[did] or {}).get("at", "")):
            res[did] = val
            changed = True
    for did, val in res.items():
        if did not in kv or (val.get("at", "") > (kv[did] or {}).get("at", "")):
            push_kv = True
    if changed:
        jsave(resolutions_path(), res)
    if push_kv:
        w.put("resolutions", res, 180 * 86400)
    return res


def _apply_resolutions(items, resolutions):
    if not resolutions:
        return items
    out = []
    for it in items:
        did = it.get("doubt")
        if did and did in resolutions:
            keep = resolutions[did].get("keep")
            label = f"{it.get('type', '')} {it['subject']}".strip()
            if keep != "all" and (not isinstance(keep, list) or (label not in keep and it["subject"] not in keep)):
                continue
            it = {k: v for k, v in it.items() if k != "doubt"}
        out.append(it)
    return out


def collect_unresolved_doubts(sources, resolutions, sync_kv=False):
    w = Worker() if (sync_kv and TOKEN) else None
    seen, out = set(), []
    for rec in sorted(sources.values(), key=lambda s: str(s.get("title", ""))):
        if rec.get("kind") != "schedule" or "data" not in rec:
            continue
        d = rec["data"]
        if d.get("to", "") < today().isoformat() and not rec.get("on_page"):
            continue
        for db in d.get("doubts") or []:
            did = db["id"]
            if did in resolutions or did in seen:
                continue
            seen.add(did)
            meta = {"id": did, "course": d.get("course"), "pair": db["pair"], "start": db["start"],
                    "groups": db["groups"], "dates": db["dates"], "options": db["options"]}
            if w and w.url:
                w.put(f"doubt:{did}", meta, 14 * 86400)
            groups_s = ", ".join(db["groups"])
            out.append({"id": f"doubt:{did}", "text": f"{d.get('course')} курс · {groups_s} · {db['pair']} пара",
                        "url": rec.get("url", ""), "doubt": meta})
    return out


def build(sources, resolutions=None):
    resolutions = resolutions if resolutions is not None else jload(resolutions_path(), {})
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
            items = _apply_resolutions(items, resolutions)
            for it in items:
                it.pop("group"); it.pop("date")
                it.setdefault("room", "потік" if it.get("stream") else groups[n].get("room", ""))
            if date.fromisoformat(iso).weekday() == 5 and not items:
                continue
            out[n]["days"][iso] = {"items": items, "src": s["title"], "url": s.get("url", "")}
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
            was_subj = " / ".join(dict.fromkeys(it["subject"] for it in same)) if same else ""
            was_teachers = list(dict.fromkeys(it["teacher"] for it in same if it.get("teacher")))
            if was_subj:
                z["was"] = was_subj
            if was_teachers:
                z["was_teachers"] = was_teachers
            if z["subject"].lower().strip() == "вікно":
                for it in same:  # «вікно» там, де пари й так немає, — нічого не змінює
                    it["cancelled"] = True
            else:
                kind = (z.get("kind") or "").lower()
                new = {"pair": z["pair"], "start": st, "end": en, "subject": z["subject"], "teacher": z.get("teacher", ""),
                       "type": "Пр" if "практ" in kind else "Лк" if "лек" in kind else "", "changed": True,
                       "room": z.get("room") or (same[0].get("room", "") if same else g["room"])}
                if was_subj:
                    new["was"] = was_subj
                if was_teachers:
                    new["was_teachers"] = was_teachers
                day["items"] = [it for it in day["items"] if it["pair"] != z["pair"]] + [new]
            day["items"].sort(key=lambda it: (it["pair"], it.get("subgroup", 0)))
        g["days"] = dict(sorted(g["days"].items()))
    pdfs = [{"title": s["title"], "url": s["url"], "kind": s["kind"],
             "course": (s.get("data") or {}).get("course") if s["kind"] == "schedule" else None}
            for s in sorted(sources.values(), key=lambda s: s.get("title", "")) if s.get("on_page")]
    return out, pdfs


def _canonical_teacher_map(out):
    """Збирає відображення сирого ПІБ викладача → канонічне ім'я, ступінь, slug (із склеюванням одруків)."""
    raw_names = []
    for g in out.values():
        for d in (g.get("days") or {}).values():
            for it in d.get("items") or []:
                if it.get("teacher"):
                    raw_names.append(it["teacher"])
                for wt in it.get("was_teachers") or []:
                    raw_names.append(wt)
        for z in g.get("changes") or []:
            if z.get("teacher"):
                raw_names.append(z["teacher"])
            for wt in z.get("was_teachers") or []:
                raw_names.append(wt)
    parsed = {}
    counts = {}
    degrees = {}
    for r in raw_names:
        p = parse_teacher_raw(r)
        if not p:
            continue
        name, deg, (sur, init) = p
        parsed[r] = (name, deg, sur, init)
        key = (sur.upper(), init.upper())
        counts[key] = counts.get(key, 0) + 1
        if deg:
            degrees[key] = deg
    canon_sur = {}
    for (usur, uinit), cnt in counts.items():
        if cnt <= 2 and len(usur) >= 5:
            match = next((ms for (ms, mi), mc in sorted(counts.items(), key=lambda kv: -kv[1])
                          if mi == uinit and mc >= 5 and ms != usur and _lev1(usur, ms)), None)
            if match:
                orig = next(p[2] for p in parsed.values() if p[2].upper() == match and p[3].upper() == uinit)
                canon_sur[(usur, uinit)] = orig
    res = {}
    for r, (name, deg, sur, init) in parsed.items():
        uk = (sur.upper(), init.upper())
        target_sur = canon_sur.get(uk, sur)
        cname = f"{target_sur} {init}".strip()
        mkey = (target_sur.upper(), init.upper())
        cdeg = degrees.get(mkey) or degrees.get(uk) or deg
        res[r] = {"name": cname, "degree": cdeg, "slug": f"t-{slug(cname)}"}
    return res


def _format_groups_label(grps, out, course_sizes):
    courses = {out[n]["course"] for n in grps if n in out}
    if len(courses) == 1:
        c = next(iter(courses))
        if course_sizes.get(c, 0) >= 5 and len(grps) == course_sizes[c]:
            return f"{c} курс ({len(grps)} груп)"
    return ", ".join(grps)


def build_teachers(out):
    """Збирає розклади викладачів по всіх групах і замінах: t_slug → структура розкладу."""
    if not out:
        return {}
    cmap = _canonical_teacher_map(out)
    group_order = {n: i for i, n in enumerate(out)}
    course_sizes = {}
    for g in out.values():
        course_sizes[g["course"]] = course_sizes.get(g["course"], 0) + 1
    all_dates = sorted({iso for g in out.values() for iso in g.get("days", {})})
    weekday_isos = [iso for iso in all_dates if date.fromisoformat(iso).weekday() < 5]

    buckets = {}

    def ensure_t(info):
        ts = info["slug"]
        if ts not in buckets:
            buckets[ts] = {"info": info, "courses": set(), "subjects": set(), "slots": {}, "sat_dates": set(), "changes": {}}
        return buckets[ts]

    for gname, g in out.items():
        c = g["course"]
        for iso, day in (g.get("days") or {}).items():
            wd = date.fromisoformat(iso).weekday()
            for it in day.get("items") or []:
                t_info = cmap.get(it.get("teacher") or "")
                if t_info:
                    tb = ensure_t(t_info)
                    tb["courses"].add(c)
                    tb["subjects"].add(it["subject"])
                    if wd == 5:
                        tb["sat_dates"].add(iso)
                    skey = (iso, it["pair"], it.get("type", ""), it["subject"],
                            bool(it.get("cancelled")), bool(it.get("changed")))
                    slot = tb["slots"].setdefault(skey, {
                        "pair": it["pair"], "start": it["start"], "end": it["end"],
                        "type": it.get("type", ""), "subject": it["subject"],
                        "groups": [], "rooms": [], "online": False, "stream": False,
                        "choice": False, "changed": bool(it.get("changed")),
                        "cancelled": bool(it.get("cancelled")), "cancel_notes": set(),
                        "was": it.get("was", ""), "src": day.get("src", ""), "url": day.get("url", ""),
                    })
                    if gname not in slot["groups"]:
                        slot["groups"].append(gname)
                    rm = (it.get("room") or "").strip()
                    if rm and rm != "потік" and rm not in slot["rooms"]:
                        slot["rooms"].append(rm)
                    slot["online"] = slot["online"] or bool(it.get("online"))
                    slot["stream"] = slot["stream"] or bool(it.get("stream"))
                    slot["choice"] = slot["choice"] or bool(it.get("choice"))
                    if it.get("cancelled"):
                        slot["cancel_notes"].add("вікно")
                if it.get("changed") and it.get("was_teachers"):
                    for wt in it["was_teachers"]:
                        wt_info = cmap.get(wt)
                        if not wt_info or (t_info and wt_info["slug"] == t_info["slug"]):
                            continue
                        wb = ensure_t(wt_info)
                        wb["courses"].add(c)
                        subj_w = it.get("was") or it["subject"]
                        wb["subjects"].add(subj_w)
                        skey = (iso, it["pair"], "", subj_w, True, True)
                        slot = wb["slots"].setdefault(skey, {
                            "pair": it["pair"], "start": it["start"], "end": it["end"],
                            "type": "", "subject": subj_w,
                            "groups": [], "rooms": [], "online": False, "stream": False,
                            "choice": False, "changed": True, "cancelled": True,
                            "cancel_notes": {f"заміна → {it['subject']}"}, "was": "",
                            "src": day.get("src", ""), "url": day.get("url", ""),
                        })
                        if gname not in slot["groups"]:
                            slot["groups"].append(gname)

        for z in g.get("changes") or []:
            t_info = cmap.get(z.get("teacher") or "")
            if t_info:
                tb = ensure_t(t_info)
                ckey = (z["date"], z["pair"], z["subject"], "assigned")
                cent = tb["changes"].setdefault(ckey, {
                    "date": z["date"], "pair": z["pair"], "subject": z["subject"],
                    "teacher": t_info["name"], "kind": z.get("kind", ""), "room": z.get("room", ""),
                    "groups": [], "role": "assigned", "src": z.get("src", ""), "url": z.get("url", ""),
                })
                if gname not in cent["groups"]:
                    cent["groups"].append(gname)
            for wt in z.get("was_teachers") or []:
                wt_info = cmap.get(wt)
                if not wt_info or (t_info and wt_info["slug"] == t_info["slug"]):
                    continue
                wb = ensure_t(wt_info)
                wsubj = z.get("was") or z["subject"]
                ckey = (z["date"], z["pair"], wsubj, "removed")
                cent = wb["changes"].setdefault(ckey, {
                    "date": z["date"], "pair": z["pair"], "subject": wsubj,
                    "teacher": wt_info["name"], "kind": z.get("kind", ""), "room": z.get("room", ""),
                    "groups": [], "role": "removed",
                    "to": "вікно" if z["subject"].lower().strip() == "вікно" else z["subject"],
                    "src": z.get("src", ""), "url": z.get("url", ""),
                })
                if gname not in cent["groups"]:
                    cent["groups"].append(gname)

    teachers = {}
    for ts, tb in sorted(buckets.items(), key=lambda kv: uk_sort_key(kv[1]["info"]["name"])):
        info = tb["info"]
        by_date_pair = {}
        for skey, s in tb["slots"].items():
            iso = skey[0]
            by_date_pair.setdefault((iso, s["pair"]), []).append(s)
        days = {}
        target_dates = sorted(set(weekday_isos) | tb["sat_dates"])
        for iso in target_dates:
            day_items = []
            day_urls = []
            day_srcs = []
            for pair_num in sorted({p for d_i, p in by_date_pair if d_i == iso}):
                plist = by_date_pair[(iso, pair_num)]
                if any(not x["cancelled"] for x in plist):
                    plist = [x for x in plist if not x["cancelled"]]
                for x in plist:
                    grps = sorted(x["groups"], key=lambda n: group_order.get(n, 999))
                    glabel = _format_groups_label(grps, out, course_sizes)
                    room = ", ".join(x["rooms"]) if x["rooms"] else ("потік" if (x["stream"] or len(grps) > 1) else "")
                    obj = {
                        "pair": x["pair"], "start": x["start"], "end": x["end"],
                        "type": x["type"], "subject": x["subject"],
                        "groups": grps, "group_label": glabel, "room": room,
                    }
                    if x["online"]:
                        obj["online"] = True
                    if x["stream"] or len(grps) > 1:
                        obj["stream"] = True
                    if x["choice"]:
                        obj["choice"] = True
                    if x["changed"]:
                        obj["changed"] = True
                    if x["cancelled"]:
                        obj["cancelled"] = True
                        if x["cancel_notes"]:
                            obj["cancel_note"] = "; ".join(sorted(x["cancel_notes"]))
                    if x["was"]:
                        obj["was"] = x["was"]
                    day_items.append(obj)
                    if x["url"] and x["url"] not in day_urls:
                        day_urls.append(x["url"])
                    if x["src"] and x["src"] not in day_srcs:
                        day_srcs.append(x["src"])
            days[iso] = {
                "items": day_items,
                "src": day_srcs[0] if day_srcs else "",
                "url": day_urls[0] if day_urls else "",
            }
        chg_list = []
        for cent in sorted(tb["changes"].values(), key=lambda x: (x["date"], x["pair"], x["subject"])):
            grps = sorted(cent["groups"], key=lambda n: group_order.get(n, 999))
            chg_list.append({**cent, "groups": grps, "group": _format_groups_label(grps, out, course_sizes)})
        teachers[ts] = {
            "kind": "teacher",
            "group": info["name"],
            "teacher": info["name"],
            "degree": info["degree"],
            "slug": ts,
            "courses": sorted(tb["courses"]),
            "subjects": sorted(tb["subjects"], key=uk_sort_key),
            "days": days,
            "changes": chg_list,
        }
    return teachers


def write_site(out, pdfs, bot_name, status=None, teachers=None):
    """docs/data/* і docs/ics/*. Повертає хеш вмісту (щоб не деплоїти сайт без змін).
    status.source_down_since — з якого часу сайт коледжу недоступний (сайт покаже попередження)."""
    teachers = build_teachers(out) if teachers is None else teachers
    gdir, tdir, idir = DOCS / "data" / "g", DOCS / "data" / "t", DOCS / "ics"
    for d in (gdir, tdir, idir):
        d.mkdir(parents=True, exist_ok=True)
        for f in d.iterdir():
            f.unlink()
    courses = {}
    for n, g in sorted(out.items(), key=lambda kv: kv[1]["course"]):  # у межах курсу — порядок стовпців PDF
        courses.setdefault(str(g["course"]), []).append({"name": n, "slug": g["slug"], "room": g["room"]})
        jsave(gdir / f"{g['slug']}.json", g, compact=True)
        build_ics(g, idir / f"{g['slug']}.ics")
    teachers_idx = []
    for ts, t in teachers.items():
        teachers_idx.append({
            "name": t["teacher"], "slug": ts, "degree": t["degree"],
            "courses": t["courses"], "subjects": t["subjects"][:4],
        })
        jsave(tdir / f"{ts}.json", t, compact=True)
        build_ics(t, idir / f"{ts}.ics")
    index = {"college": CFG["college"], "short": CFG.get("short", ""), "bot": bot_name, "source_page": PAGE,
             "bells": CFG["bells"], "courses": courses, "teachers": teachers_idx, "pdfs": pdfs,
             "source_down_since": (status or {}).get("source_down_since")}
    h = hashlib.sha256(json.dumps([index, out, teachers], ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:16]
    index.update({"hash": h, "updated": now().isoformat(timespec="minutes")})
    jsave(DOCS / "data" / "index.json", index)
    return h


def build_ics(g, target):
    is_t = g.get("kind") == "teacher"
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
            uid_key = f"{g['group']}{iso}{it['pair']}{it.get('group_label', '')}{it.get('subgroup', '')}{it['subject']}"
            uid = hashlib.md5(uid_key.encode()).hexdigest()
            title = f"{(it.get('type', '') + ' ' + it['subject']).strip()}{' (заміна)' if it.get('changed') else ''}"
            if is_t and it.get("group_label"):
                title = f"{title} ({it['group_label']})"
            desc = " · ".join(x for x in [f"Групи: {it['group_label']}" if is_t and it.get("group_label") else it.get("teacher", ""), str(it["pair"]) + " пара"] if x)
            loc = "онлайн" if it.get("online") else (f"ауд. {it['room']}" if it.get("room") and str(it["room"]).isdigit() else it.get("room", ""))
            out += ["BEGIN:VEVENT", f"UID:{uid}@pfk-rozklad", f"DTSTAMP:{stamp}",
                    f"DTSTART;TZID=Europe/Kyiv:{d}T{it['start'].replace(':', '')}00",
                    f"DTEND;TZID=Europe/Kyiv:{d}T{it['end'].replace(':', '')}00",
                    f"SUMMARY:{esc(title)}", f"LOCATION:{esc(loc)}",
                    f"DESCRIPTION:{esc(desc)}", "END:VEVENT"]
    out.append("END:VCALENDAR")
    target.write_text("\r\n".join(out) + "\r\n", encoding="utf-8")


# ---------------------------------------------------------------- тексти повідомлень
def lesson_line(it, is_teacher=False):
    n = NUM[it["pair"] - 1] if 0 < it["pair"] <= len(NUM) else f"{it['pair']}."
    if it.get("cancelled"):
        note = it.get("cancel_note") or "вікно"
        grp = f" ({escape(it['group_label'])})" if is_teacher and it.get("group_label") else ""
        return f"{n} <s>{escape(it.get('subject', ''))}{grp}</s> — <b>{escape(note)}</b>"
    t = (it.get("type", "") + " ").lstrip()
    extra = []
    if it.get("subgroup"):
        extra.append(f"{it['subgroup']} підгр.")
    elif it.get("choice"):
        extra.append("за вибором")
    if is_teacher and it.get("group_label"):
        extra.append(f"👥 {escape(it['group_label'])}")
    elif it.get("teacher"):
        extra.append(escape(it["teacher"]))
    if it.get("room") and not it.get("online"):
        rm = f"ауд. {it['room']}" if str(it["room"]).isdigit() else it["room"]
        if rm != "потік":
            extra.append(escape(rm))
    if it.get("online"):
        extra.append("онлайн")
    mark = " 🔄" if it.get("changed") else ""
    return f"{n} {it['start']}–{it['end']}  <b>{escape(t + it['subject'])}</b>{mark}" + (f"\n      <i>{', '.join(extra)}</i>" if extra else "")


def mine(items, sg=0):
    """Підгрупа: спільні пари + пари своєї підгрупи (sg=0 — усі)."""
    return [i for i in items if not sg or not i.get("subgroup") or i["subgroup"] == sg]


def fmt_day(g, d, title, sg=0):
    is_t = g.get("kind") == "teacher"
    who = f"🎓 {escape(g['teacher'])}" if is_t else escape(g["group"])
    head = f"📅 <b>{title}: {DAYN[d.weekday()]}, {d:%d.%m}</b> · {who}"
    day = g["days"].get(d.isoformat())
    if day is not None:
        day = {**day, "items": day["items"] if is_t else mine(day["items"], sg)}
    if day is None:
        return f"{head}\n😴 Вихідний" if d.weekday() >= 5 else f"{head}\n⚠️ Розклад на цю дату ще не опубліковано"
    live = [i for i in day["items"] if not i.get("cancelled")]
    if not live and not day["items"]:
        return f"{head}\nПар немає 🎉"
    lines = [head, ""] + [lesson_line(it, is_teacher=is_t) for it in day["items"]]
    if any(i.get("changed") or i.get("cancelled") for i in day["items"]):
        lines.append("\n🔄 — заміна з «Змін до розкладу»")
    if live:
        npairs = len({i["pair"] for i in live})
        lines.append(f"\n🏁 {live[0]['start']}–{live[-1]['end']} · {npairs} {plural(npairs)}")
    return "\n".join(lines)


def plural(n):
    return "пара" if n % 10 == 1 and n % 100 != 11 else "пари" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "пар"


def fmt_week(g, monday, sg=0):
    is_t = g.get("kind") == "teacher"
    who = f"🎓 {escape(g['teacher'])}" if is_t else escape(g["group"])
    lines = [f"🗓 <b>Тиждень {monday:%d.%m}–{monday + timedelta(days=4):%d.%m}</b> · {who}"]
    for i in range(6):
        d = monday + timedelta(days=i)
        day = g["days"].get(d.isoformat())
        if day is None:
            if i < 5:
                lines.append(f"<b>{SHORTD[i]} {d:%d.%m}</b>: ⚠️ ще не опубліковано")
            continue
        items = day["items"] if is_t else mine(day["items"], sg)
        live = [x for x in items if not x.get("cancelled")]
        if not live:
            lines.append(f"<b>{SHORTD[i]} {d:%d.%m}</b>: пар немає 🎉"); continue
        subj = "\n".join(
            f"  {x['pair']}. {escape(x['subject'])}"
            + (f" ({escape(x['group_label'])})" if is_t and x.get("group_label") else "")
            + (" 🔄" if x.get("changed") else "")
            for x in live
        )
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
    q = f"t={urllib.parse.quote(g['teacher'])}" if g.get("kind") == "teacher" else f"g={urllib.parse.quote(g['group'])}"
    return {"inline_keyboard": [[{"text": "🌐 Сайт", "url": f"{SITE}?{q}"},
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
                if st.get("pending"):
                    return
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


DEFAULT_ANNOUNCE = (
    "✅ <b>Бот розкладу ПФК НЮУ відновлено!</b>\n\n"
    "Бот знову працює у штатному режимі: розклади усіх курсів та заміни "
    "автоматично оновлюються з офіційного порталу nlu.edu.ua.\n\n"
    "Натисни «📅 Сьогодні» або «🗓 Тиждень», щоб перевірити актуальні пари."
)


def notify(out, events, mode, only=None, stale_since=None, message="", teachers=None):
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
    teachers = build_teachers(out) if teachers is None else teachers
    by_slug = {g["slug"]: g for g in out.values()}
    t_by_name = {t["teacher"]: t for t in teachers.values()}
    cmap = _canonical_teacher_map(out) if out else {}
    t, h = today(), now().hour
    D = Delivery(w)

    def G(u):
        val = u.get("g") or ""
        return out.get(val) or by_slug.get(val) or teachers.get(val) or t_by_name.get(val)

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
        if g.get("kind") == "teacher" and not [x for x in g["days"][t.isoformat()]["items"] if not x.get("cancelled") or x.get("changed")]:
            return None
        return fmt_day(g, t, "Сьогодні", sg(u)) + note

    tm = t + timedelta(days=1)
    next_mon = t + timedelta(days=7 - t.weekday())

    def evening(u):
        g = G(u)
        if not g:
            return None
        is_t = g.get("kind") == "teacher"
        if t.weekday() == 6:  # неділя — огляд наступного тижня, якщо його вже опубліковано
            days_ok = [g["days"].get((next_mon + timedelta(days=i)).isoformat()) for i in range(6)]
            if on(u, "w") and (any((d or {}).get("items") for d in days_ok) if is_t else any(d is not None for d in days_ok)):
                return fmt_week(g, next_mon, sg(u)) + note
            if on(u, "w"):
                return None
        if t.weekday() != 5 and on(u, "e") and tm.isoformat() in g["days"]:
            if is_t and not [x for x in g["days"][tm.isoformat()]["items"] if not x.get("cancelled") or x.get("changed")]:
                return None
            return fmt_day(g, tm, "Завтра", sg(u)) + note
        return None

    scheduled("morning", "mh", DEFAULTS["mh"], MORNING_UNTIL, morning)
    scheduled("evening", "eh", DEFAULTS["eh"], EVENING_UNTIL, evening)
    if mode == "weekly":
        mon = t - timedelta(days=t.weekday()) if t.weekday() < 5 else next_mon
        D.run(f"weekly:{t}:force", test_users if only else users, lambda u: G(u) and fmt_week(G(u), mon, sg(u)) + note, kb, force=True)
    if mode == "announce":
        msg_text = (message or "").strip() or DEFAULT_ANNOUNCE
        D.run(f"announce:{t}:force", test_users if only else users, lambda u, m=msg_text: m, kb, force=True)
    for e in events:
        if e["type"] == "changes":
            targets = [(z, set(parse.change_targets(z["group"], glist))) for z in e["rows"]]

            def _t_rows(t_slug):
                out_rows = []
                for z, gs in targets:
                    assign_t = (cmap.get(z.get("teacher") or "") or {}).get("slug")
                    if assign_t == t_slug:
                        out_rows.append((z, "assigned", sorted(gs)))
                        continue
                    for gn in sorted(gs):
                        for gc in out.get(gn, {}).get("changes") or []:
                            if gc["date"] == z["date"] and gc["pair"] == z["pair"] and gc["subject"] == z["subject"]:
                                if any((cmap.get(wt) or {}).get("slug") == t_slug for wt in gc.get("was_teachers") or []):
                                    out_rows.append((gc, "removed", [gn]))
                return out_rows

            def txt(u, targets=targets):
                g = G(u)
                if not g or not on(u, "c"):
                    return None
                if g.get("kind") == "teacher":
                    t_rows = _t_rows(g["slug"])
                    if not t_rows:
                        return None
                    lines = []
                    for z, role, gs in t_rows:
                        d_s = f"{date.fromisoformat(z['date']):%d.%m} ({SHORTD[date.fromisoformat(z['date']).weekday()]})"
                        grp_s = ", ".join(gs) if gs else z["group"]
                        if role == "removed":
                            to_s = "<b>вікно</b>" if z["subject"].lower() == "вікно" else f"заміна → {escape(z['subject'])}"
                            lines.append(f"• {d_s}, {z['pair']} пара · {escape(grp_s)}: <s>{escape(z.get('was') or '')}</s> — {to_s}")
                        else:
                            lines.append(f"• {d_s}, {z['pair']} пара · {escape(grp_s)}: <b>{escape(z['subject'])}</b>")
                    return f"🔔 <b>Нові заміни</b> · 🎓 {escape(g['teacher'])}\n\n" + "\n".join(lines)
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

            def sched_txt(u, e=e, mon=mon, word=word):
                g = G(u)
                if not g or not on(u, "c"):
                    return None
                if g.get("kind") == "teacher":
                    if e["course"] not in g.get("courses", []):
                        return None
                    has_any = any(g["days"].get(iso, {}).get("items") for iso in g["days"] if e["from"] <= iso <= e["to"])
                    if not has_any:
                        return None
                    return f"🆕 <b>{word} ({e['course']} курс</b>, {date.fromisoformat(e['from']):%d.%m}–{date.fromisoformat(e['to']):%d.%m})\n\n" + fmt_week(g, mon, sg(u))
                if g["course"] != e["course"]:
                    return None
                return f"🆕 <b>{word}</b> ({date.fromisoformat(e['from']):%d.%m}–{date.fromisoformat(e['to']):%d.%m})\n\n" + fmt_week(g, mon, sg(u))

            D.run("ev:" + e["key"], users, sched_txt, kb, ttl=14 * 86400)
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
    """Порівнює поточні збої й сумніви з попереднім запуском: адміну — про новий збій/сумнів, нагадування раз на REMIND_HOURS
    і повідомлення про відновлення/закриття. Стан — data/health.json (комітиться). Не вдалося надіслати → повтор наступного разу."""
    old = jload(health_path(), {}).get("issues", {})
    cur, fresh, remind, fixed = {}, [], [], []
    fresh_doubts, remind_doubts = [], []
    for p in problems:
        o = old.get(p["id"]) or {}
        if o.get("resolved"):  # збій повернувся раніше, ніж ми встигли повідомити про відновлення
            o = {k: v for k, v in o.items() if k != "resolved"}
        # текст першої помилки не переписуємо щоразу (тайм-аут/обрив чергуються) — інакше коміт data/ щогодини
        st = {"text": o.get("text") or p["text"], "url": p.get("url", ""), "since": o.get("since") or stamp(), "alerted": o.get("alerted")}
        if o.get("msgs"):
            st["msgs"] = o["msgs"]
        if p["id"].startswith("doubt:"):
            if not st["alerted"]:
                fresh_doubts.append((st, p.get("doubt")))
            elif hours_since(st["alerted"]) >= REMIND_HOURS:
                remind_doubts.append((st, p.get("doubt")))
        else:
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
            if pid.startswith("doubt:"):
                return f"• Питання щодо розкладу закрито ({escape(o.get('text', ''))})"
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
        for pid, o in fixed:
            if pid.startswith("doubt:") and o.get("msgs"):
                for cid, mid in o["msgs"].items():
                    tg("editMessageReplyMarkup", {"chat_id": cid, "message_id": mid, "reply_markup": {"inline_keyboard": []}})
    elif tried:  # про відновлення не вдалося повідомити — лишаємо запис, спробуємо наступного разу
        for pid, o in fixed:
            cur[pid] = {**o, "resolved": True}
    for st, db in fresh_doubts + remind_doubts:
        if not db:
            continue
        days_s = ", ".join(f"{date.fromisoformat(x):%d.%m}" for x in db.get("dates") or [])
        opts_s = "\n".join(
            f"  {i + 1}. <b>{escape((o.get('type', '') + ' ' + o['subject']).strip())}</b>"
            + (f" ({escape(o['teacher'])})" if o.get("teacher") else "")
            for i, o in enumerate(db.get("options") or [])
        )
        q_text = (f"❓ <b>Сумнівне місце у PDF розкладу</b>\n"
                  f"{escape(str(db.get('course') or ''))} курс · {', '.join(escape(g) for g in db.get('groups') or [])}\n"
                  f"Дні: {days_s} · {db['pair']} пара ({db.get('start', '')})\n\n"
                  f"Варіанти:\n{opts_s}")
        did = db["id"]
        btns = [[{"text": f"{i + 1}. {(o.get('type', '') + ' ' + o['subject']).strip()}"[:40], "callback_data": f"rs:{did}:{i}"}]
                for i, o in enumerate(db.get("options") or [])]
        btns.append([{"text": "Обидва — підгрупи", "callback_data": f"rs:{did}:all"}])
        if send and TOKEN and ADMINS:
            d_ok, msgs = False, dict(st.get("msgs") or {})
            for c in ADMINS:
                r = tg("sendMessage", {"chat_id": c, "text": q_text, "parse_mode": "HTML",
                                       "reply_markup": {"inline_keyboard": btns},
                                       "link_preview_options": {"is_disabled": True}})
                if r.get("ok"):
                    d_ok = True
                    mid = (r.get("result") or {}).get("message_id")
                    if mid:
                        msgs[str(c)] = mid
            if d_ok:
                st["alerted"] = stamp()
            if msgs:
                st["msgs"] = msgs
        else:
            log("Сумнів у PDF (без надсилання):\n" + re.sub(r"<[^>]+>", "", q_text))
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


def sync_worker_site(index, out, teachers=None):
    """Дублює зібраний розклад (групи й викладачі) у KV воркера (1 ключ), щоб бот працював під час збоїв GitHub Pages."""
    if not index or not out:
        return
    teachers = build_teachers(out) if teachers is None else teachers
    w = Worker()
    if not w.url:
        return
    cur = w.get("site:bundle")
    if cur is MISSING:
        return
    if isinstance(cur, dict) and cur.get("hash") == index.get("hash"):
        return
    bundle = {"hash": index["hash"], "updated": index["updated"], "index": index,
              "groups": {**{g["slug"]: g for g in out.values()}, **teachers}}
    if w.put("site:bundle", bundle, 30 * 86400):
        log("Розклад синхронізовано з KV воркера")


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
        {"command": "teacher", "description": "Розклад викладача"},
        {"command": "settings", "description": "Сповіщення"},
        {"command": "help", "description": "Допомога"}]})
    tg("setMyDescription", {"description": f"Розклад усіх груп і викладачів {CFG.get('short', '')}: пари на сьогодні й завтра, тиждень, заміни. "
                                           "Натисни «Почати» й обери свою групу або викладача. Дані — з nlu.edu.ua."})
    tg("setMyShortDescription", {"short_description": f"Розклад і заміни (групи й викладачі) · {CFG.get('short', '')}"})
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
    resolutions = sync_resolutions()
    problems += collect_unresolved_doubts(sources, resolutions, sync_kv=True)
    out, pdfs = build(sources, resolutions)
    teachers = build_teachers(out) if out else {}
    status = {"source_down_since": issue_since("site", problems)}
    if not out:  # нічого не розібралося — не затираємо робочий сайт порожнім
        problems.append({"id": "empty", "text": "Після розбору не лишилося жодної групи — сайт не оновлено", "url": PAGE})
        h, changed = None, False
    else:
        if TOKEN:
            ensure_webhook()
        bot_name = setup_bot() if TOKEN else ""
        h = write_site(out, pdfs, bot_name, status, teachers)
        if TOKEN:
            sync_worker_site(jload(DOCS / "data" / "index.json", None), out, teachers)
        changed = h != deployed_hash()
    deploy = bool(out) and (changed or a.mode != "auto")
    tech_problems = [p for p in problems if not p["id"].startswith("doubt:")]
    state = {"events": events, "problems": problems, "unchecked": sorted(unchecked), "hash": h, "deploy": deploy,
             "stale_since": status["source_down_since"]}
    jsave(state_file(a), state)
    gh_output(deploy="true" if deploy else "false", degraded="true" if tech_problems else "false")
    log(f"Груп: {len(out)}, викл.: {len(teachers)}, PDF: {len(pdfs)}, подій: {len(events)}, проблем: {len(tech_problems)}, сумнівів: {len(problems) - len(tech_problems)}, сайт {'змінився' if changed else 'без змін'}")
    for p in tech_problems:
        log(f"::error::{p['text']}")
    return state


def cmd_notify(a, state=None):
    state = state or jload(state_file(a), None)
    if state is None:
        sys.exit("Немає файла стану від `update` — спершу запусти update")
    out, _ = build(load_sources())
    teachers = build_teachers(out) if out else {}
    if state.get("events") and state.get("deploy") and os.environ.get("PAGES_DEPLOYED") == "success" and state.get("hash"):
        wait_deployed(state["hash"])
    only = [c.strip() for c in (a.only or "").split(",") if c.strip()]
    msg = getattr(a, "message", "") or os.environ.get("ANNOUNCE_MESSAGE", "")
    problems = list(state.get("problems") or [])
    problems += notify(out, state.get("events") or [], a.mode, only=only, stale_since=state.get("stale_since"), message=msg, teachers=teachers)
    report(problems, state.get("unchecked") or [])
    tech_problems = [p for p in problems if not p["id"].startswith("doubt:")]
    heartbeat(tech_problems)
    gh_output(degraded="true" if tech_problems else "false")
    lines = ["### Розклад ПФК", f"- подій (новий розклад / заміни): {len(state.get('events') or [])}"]
    lines += [f"- ❌ {p['text']}" for p in tech_problems] or ["- ✅ збоїв немає"]
    summary(lines)
    if tech_problems:
        log("Перевірка завершилась зі збоями: " + "; ".join(p["text"] for p in tech_problems))
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
    teachers = build_teachers(out) if out else {}
    nq = parse._norm(a.group)
    g = (out.get(a.group)
         or next((v for k, v in out.items() if parse._norm(k) == nq), None)
         or teachers.get(a.group)
         or next((v for v in teachers.values() if parse._norm(v["teacher"]) == nq or nq in parse._norm(v["teacher"])), None))
    if not g:
        sys.exit(f"Не знайдено. Групи: {', '.join(sorted(out))}")
    d = date.fromisoformat(a.date) if a.date else today()
    print(re.sub(r"<[^>]+>", "", fmt_day(g, d, "День")))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8"); sys.stderr.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    modes = ["auto", "morning", "evening", "weekly", "announce", "none"]
    for name in ("run", "update", "notify"):
        r = sub.add_parser(name)
        r.add_argument("--mode", default="auto", choices=modes)
        r.add_argument("--state", help="файл стану між update і notify (типово $ROZKLAD_STATE)")
        r.add_argument("--only", default="", help="лише для morning/evening/weekly/announce: chat id тестового чату (через кому)")
        r.add_argument("--message", default="", help="текст для mode=announce")
        r.add_argument("--strict", action="store_true", help="run: код виходу 2, якщо були збої")
    s = sub.add_parser("show"); s.add_argument("group"); s.add_argument("--date")
    a = ap.parse_args()
    {"run": cmd_run, "update": cmd_update, "notify": cmd_notify, "show": cmd_show}[a.cmd](a)


if __name__ == "__main__":
    main()
