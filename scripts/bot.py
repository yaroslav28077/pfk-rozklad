#!/usr/bin/env python3
"""Бот розкладу ПФК НЮУ: перевіряє plc.nlu.edu.ua, розбирає зміни, будує сайт і шле повідомлення в Telegram.

  python scripts/bot.py update                 # перевірити сайт, оновити data/ та docs/
  python scripts/bot.py notify --mode morning  # розклад на сьогодні в Telegram
  python scripts/bot.py notify --mode evening  # розклад на завтра
  python scripts/bot.py notify --mode watch    # лише нові заміни / новий розклад (якщо є)
  python scripts/bot.py show --date 2026-10-27 # показати день у консолі

Змінні середовища (GitHub Secrets): TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID (кілька — через кому).
"""
import argparse
import hashlib
import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parent.parent
DATA, DOCS = ROOT / "data", ROOT / "docs"
CFG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
TZ = ZoneInfo(CFG.get("timezone", "Europe/Kyiv"))
PAGE = "http://plc.nlu.edu.ua/%d0%bd%d0%b0%d0%b2%d1%87%d0%b0%d0%bb%d1%8c%d0%bd%d0%b0-%d1%80%d0%be%d0%b1%d0%be%d1%82%d0%b0/%d1%80%d0%be%d0%b7%d0%ba%d0%bb%d0%b0%d0%b4-%d0%b7%d0%b0%d0%bd%d1%8f%d1%82%d1%8c/"
UA = {"User-Agent": "Mozilla/5.0 (pfk-rozklad bot; +github)"}
TIMES = {1: ("08:00", "09:20"), 2: ("09:30", "10:50"), 3: ("11:00", "12:15"), 4: ("12:45", "14:00"), 5: ("14:10", "15:30"), 6: ("15:40", "17:00")}
DAYN = ["Понеділок", "Вівторок", "Середа", "Четвер", "П'ятниця", "Субота", "Неділя"]
MONTHS = {"січ": 1, "лют": 2, "бер": 3, "кві": 4, "тра": 5, "чер": 6, "лип": 7, "сер": 8, "вер": 9, "жов": 10, "лис": 11, "гру": 12}
NUM = "1️⃣ 2️⃣ 3️⃣ 4️⃣ 5️⃣ 6️⃣".split()


def jload(p, default):
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def jsave(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def today():
    return datetime.now(TZ).date()


def norm(s):
    return re.sub(r"[\s\-–]", "", (s or "").upper())


# ---------------------------------------------------------------- сайт коледжу
def http_get(url, timeout=60):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
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
    t = f"{text} {name}".lower()
    if not name.endswith(".pdf"):
        return None
    if "змін" in t:
        return "zminy"
    if "розклад" in t:
        m = re.search(r"(\d)\s*[-_ ]?\s*курс", t)
        return f"kurs{m.group(1)}" if m else None
    return None


def parse_date(s, year):
    s = s.strip().lower()
    m = re.match(r"(\d{1,2})\s*[.\s]\s*(\d{1,2})(?:\.(\d{2,4}))?$", s)
    if m:
        y = int(m.group(3)) if m.group(3) else year
        return date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1)))
    m = re.match(r"(\d{1,2})\s*[.\s]*\s*([а-яіїєґ']+)", s)
    if m and m.group(2)[:3] in MONTHS:
        return date(year, MONTHS[m.group(2)[:3]], int(m.group(1)))
    return None


def parse_zminy(pdf_bytes, fname):
    import pymupdf

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out = []
    for page in doc:
        txt = page.get_text()
        ym = re.search(r"\d{2}\.\d{2}\.(\d{4})", txt)
        year = int(ym.group(1)) if ym else today().year
        for t in page.find_tables().tables:
            cols, cur = None, None
            for r in t.rows:
                row = [" ".join(page.get_text("text", clip=cb).split()) if cb else "" for cb in r.cells]
                low = [c.lower() for c in row]
                if "пара" in low and "група" in low:
                    cols = {k: low.index(k) for k in low if k}; continue
                if cols is None or not any(row):
                    continue
                g = lambda k: row[cols[k]] if k in cols and cols[k] < len(row) else ""  # noqa: E731
                if g("дата"):
                    cur = parse_date(g("дата"), year) or cur
                if not cur:
                    continue
                subj = g("дисципліна") or g("зміна")
                if g("зміна") and g("зміна").lower() == "вікно":
                    subj = "вікно"
                pair = re.sub(r"\D", "", g("пара"))
                if not pair:
                    continue
                out.append({"date": cur.isoformat(), "pair": int(pair), "group": g("група"),
                            "subject": subj or "вікно", "teacher": g("викладач"),
                            "kind": g("вид заняття") or g("вид"), "source": fname})
    return out


def concerns_group(groups_text):
    g = norm(CFG["group"])
    gt = norm(groups_text)
    if g and g in re.split(r"[,;]", gt) or gt == g:
        return True
    return bool(re.search(rf"\b{CFG['course']}\s*курс", groups_text or ""))


def crop_group_png(pdf_bytes, out_png):
    """PNG колонки групи з нового PDF розкладу — для швидкої звірки людиною."""
    import pymupdf

    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    g = norm(CFG["group"])
    for page in doc:
        words = page.get_text("words")
        heads = sorted([w for w in words if re.fullmatch(r"[А-ЯІЇЄҐ]{2,4}-\d{2}[А-ЯІЇЄҐа-яіїєґ\-]*", w[4].strip())], key=lambda w: w[0])
        mine = [w for w in heads if norm(w[4]) == g]
        if not mine:
            continue
        band = sorted([w for w in heads if abs(w[1] - mine[0][1]) < 6], key=lambda w: w[0])
        i = band.index(mine[0]); cx = lambda w: (w[0] + w[2]) / 2  # noqa: E731
        left = (cx(band[i - 1]) + cx(band[i])) / 2 if i else 0
        right = (cx(band[i]) + cx(band[i + 1])) / 2 if i + 1 < len(band) else page.rect.x1
        right = max(right, left + page.rect.width * 0.35)  # захопити об'єднані клітинки
        page.get_pixmap(dpi=150, clip=pymupdf.Rect(0, 0, min(right, page.rect.x1), page.rect.y1)).save(out_png)
        return True
    page = doc[0]; page.get_pixmap(dpi=110).save(out_png)
    return False


def cmd_update(a=None):
    state = jload(DATA / "state.json", {"files": {}, "events": []})
    html = http_get(PAGE).decode("utf-8", "replace")
    p = Links(); p.feed(html)
    current, zminy, seen = [], [], set()
    my_kurs = f"kurs{CFG['course']}"
    events = []
    for text, href in p.links:
        kind = classify(text, href)
        if not kind or href in seen:
            continue
        seen.add(href)
        url = urllib.parse.quote(urllib.parse.urljoin(PAGE, href), safe=":/?=&%#~+")
        fname = Path(urllib.parse.unquote(urllib.parse.urlparse(url).path)).name
        try:
            data = http_get(url)
        except Exception as e:  # noqa: BLE001
            print(f"ERR {fname}: {e}", file=sys.stderr); continue
        h = hashlib.sha256(data).hexdigest()
        old = state["files"].get(fname, {})
        is_new = old.get("sha256") != h
        current.append(fname)
        state["files"][fname] = {"kind": kind, "title": text, "url": url, "sha256": h,
                                 "first_seen": old.get("first_seen") or datetime.now(TZ).isoformat(timespec="minutes")}
        if kind == "zminy":
            rows = [z for z in parse_zminy(data, fname) if concerns_group(z["group"])]
            zminy += rows
            future = [z for z in rows if z["date"] >= today().isoformat()]
            if is_new and state.get("bootstrapped") and future:
                events.append({"type": "zminy", "title": text, "url": url, "count": len(future)})
        elif kind == my_kurs and is_new:
            DOCS.mkdir(exist_ok=True)
            crop_group_png(data, DOCS / "novyi_rozklad.png")
            if old.get("sha256") is not None or state.get("bootstrapped"):
                events.append({"type": "rozklad", "title": text, "url": url})
                state["new_schedule"] = {"title": text, "url": url, "seen": datetime.now(TZ).isoformat(timespec="minutes")}
    if not current:
        events.append({"type": "error", "title": "На сторінці розкладу не знайдено PDF — можливо, змінився сайт", "url": PAGE})
    # унікальні зміни, сортування
    uniq = {(z["date"], z["pair"], z["group"], z["subject"]): z for z in zminy}
    zminy = sorted(uniq.values(), key=lambda z: (z["date"], z["pair"]))
    jsave(DATA / "zminy.json", zminy)
    state["current"] = current
    state["bootstrapped"] = True
    state["last_check"] = datetime.now(TZ).isoformat(timespec="minutes")
    state["events"] = (state.get("events", []) + [dict(e, at=state["last_check"], sent=False) for e in events])[-30:]
    jsave(DATA / "state.json", state)
    build_site()
    print(f"Перевірено: {len(current)} PDF, змін для групи: {len(zminy)}, подій: {len(events)}")


# ---------------------------------------------------------------- логіка дня
def base_items(d):
    sch = jload(DATA / "rozklad.json", {})
    if not sch:
        return None, "Немає data/rozklad.json"
    d0, d1 = date.fromisoformat(sch["from"]), date.fromisoformat(sch["to"])
    if not d0 <= d <= d1:
        return None, f"Розклад діє {d0:%d.%m}–{d1:%d.%m}; на {d:%d.%m} його ще не оновлено"
    week0 = d0 - timedelta(days=d0.weekday())
    wk = (d - week0).days // 7 + 1
    res = []
    for it in sch["items"]:
        if it["day"] != d.isoweekday():
            continue
        if it.get("dates") and d.isoformat() not in it["dates"]:
            continue
        if it.get("from") and d < date.fromisoformat(it["from"]):
            continue
        if it.get("to") and d > date.fromisoformat(it["to"]):
            continue
        if it.get("weeks") in ("odd", "even") and (it["weeks"] == "odd") != (wk % 2 == 1):
            continue
        res.append(dict(it))
    return res, None


def day_plan(d):
    items, err = base_items(d)
    zm = [z for z in jload(DATA / "zminy.json", []) if z["date"] == d.isoformat()]
    if items is None:
        return {"date": d.isoformat(), "error": err, "items": [], "changes": zm}
    by = {it["pair"]: it for it in items}
    for z in zm:
        if z["subject"].lower() == "вікно":
            if z["pair"] in by:
                by[z["pair"]]["cancelled"] = True
            else:
                by[z["pair"]] = {"pair": z["pair"], "subject": "вікно", "cancelled": True}
        else:
            prev = by.get(z["pair"])
            by[z["pair"]] = {"pair": z["pair"], "subject": z["subject"], "teacher": z["teacher"],
                             "type": "Пр" if "практ" in (z["kind"] or "").lower() else ("Лк" if "лек" in (z["kind"] or "").lower() else ""),
                             "room": (prev or {}).get("room", ""), "changed": True,
                             "was": (prev or {}).get("subject")}
    out = []
    for p in sorted(by):
        it = by[p]
        st, en = TIMES.get(p, ("", ""))
        it["start"], it["end"] = it.get("start") or st, it.get("end") or en
        out.append(it)
    return {"date": d.isoformat(), "items": out, "changes": zm}


def fmt_day(plan, title):
    d = date.fromisoformat(plan["date"])
    head = f"📅 <b>{title}: {DAYN[d.weekday()]}, {d:%d.%m}</b> — {escape(CFG['group'])}"
    if plan.get("error"):
        return f"{head}\n⚠️ {escape(plan['error'])}"
    if d.weekday() >= 5:
        return f"{head}\n😴 Вихідний"
    lines = [head]
    live = [i for i in plan["items"] if not i.get("cancelled")]
    if not live:
        lines.append("Пар немає 🎉")
    for it in plan["items"]:
        n = NUM[it["pair"] - 1] if 0 < it["pair"] <= 6 else str(it["pair"])
        if it.get("cancelled"):
            lines.append(f"{n} <s>{escape(it.get('subject', ''))}</s> — <b>вікно</b>"); continue
        t = f"{it.get('type', '')} ".lstrip()
        extra = f" ({escape(it['teacher'])})" if it.get("teacher") else ""
        mark = " 🔄" if it.get("changed") else ""
        lines.append(f"{n} {it['start']}–{it['end']}  {escape(t + it['subject'])}{extra}{mark}")
    if any(i.get("changed") or i.get("cancelled") for i in plan["items"]):
        lines.append("\n🔄 — заміна за PDF «Зміни до розкладу»")
    if live:
        lines.append(f"🏁 Перша пара о {live[0]['start']}, остання закінчується о {live[-1]['end']}")
    return "\n".join(lines)


# ---------------------------------------------------------------- Telegram
def tg_send(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chats = [c.strip() for c in os.environ.get("TELEGRAM_CHAT_ID", "").split(",") if c.strip()]
    if not token or not chats:
        print("[Telegram не налаштовано — повідомлення лише в лог]\n" + re.sub(r"<[^>]+>", "", text)); return
    for chat in chats:
        body = urllib.parse.urlencode({"chat_id": chat, "text": text, "parse_mode": "HTML",
                                       "disable_web_page_preview": "true"}).encode()
        try:
            urllib.request.urlopen(f"https://api.telegram.org/bot{token}/sendMessage", body, timeout=30).read()
            print(f"Надіслано в чат …{chat[-4:]}")
        except Exception as e:  # noqa: BLE001
            print(f"Telegram помилка для …{chat[-4:]}: {e}", file=sys.stderr)


def cmd_notify(a):
    state = jload(DATA / "state.json", {"events": []})
    site = CFG.get("site_url", "")
    msgs = []
    if a.mode == "morning" and today().weekday() < 5:
        msgs.append(fmt_day(day_plan(today()), "Сьогодні"))
    if a.mode == "evening":
        tm = today() + timedelta(days=1)
        if tm.weekday() < 5:
            msgs.append(fmt_day(day_plan(tm), "Завтра"))
    for e in state.get("events", []):
        if e.get("sent"):
            continue
        if e["type"] == "zminy" and e.get("count"):
            msgs.append(f"🔔 <b>Нові зміни до розкладу</b> ({escape(e['title'])}) — є записи для {escape(CFG['group'])}: {e['count']}.\n{e['url']}")
        elif e["type"] == "rozklad":
            msgs.append(f"🆕 <b>На сайті новий розклад</b>: {escape(e['title'])}.\nПотрібна звірка й оновлення data/rozklad.json (поки бот показує старий).\n{e['url']}")
        elif e["type"] == "error":
            msgs.append(f"⚠️ {escape(e['title'])}\n{e['url']}")
        e["sent"] = True
    jsave(DATA / "state.json", state)
    if msgs:
        tg_send("\n\n".join(msgs) + (f"\n\n🌐 {site}" if site else ""))
    else:
        print("Нічого надсилати.")


# ---------------------------------------------------------------- сайт + ics
def build_site():
    DOCS.mkdir(exist_ok=True)
    sch = jload(DATA / "rozklad.json", {})
    state = jload(DATA / "state.json", {})
    t0 = today()
    days = [day_plan(t0 + timedelta(days=i)) for i in range(0, 14)]
    files = state.get("files", {})
    site = {
        "group": CFG["group"], "course": CFG["course"], "college": CFG.get("college", ""),
        "generated": datetime.now(TZ).isoformat(timespec="minutes"),
        "last_check": state.get("last_check"),
        "schedule": {"from": sch.get("from"), "to": sch.get("to"), "source": sch.get("source"), "items": sch.get("items", [])},
        "days": days, "changes": jload(DATA / "zminy.json", []),
        "new_schedule": state.get("new_schedule"),
        "pdfs": [{"title": files[f]["title"], "url": files[f]["url"], "kind": files[f]["kind"]} for f in state.get("current", []) if f in files],
        "times": {str(k): v for k, v in TIMES.items()},
        "source_page": PAGE,
    }
    jsave(DOCS / "site.json", site)
    build_ics(sch, DOCS / "rozklad.ics")
    build_icons()


def build_icons():
    """PNG-іконки для PWA з docs/icon.svg (потрібні Android/iOS)."""
    src = DOCS / "icon.svg"
    if not src.exists():
        return
    try:
        import pymupdf
        for size in (192, 512):
            out = DOCS / f"icon-{size}.png"
            if out.exists() and out.stat().st_mtime >= src.stat().st_mtime:
                continue
            doc = pymupdf.open(src)
            page = doc[0]
            zoom = size / max(page.rect.width, page.rect.height)
            page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False).save(out)
    except Exception as e:  # noqa: BLE001
        print(f"Іконки не створено: {e}", file=sys.stderr)


def build_ics(sch, target):
    if not sch:
        return
    d0, d1 = date.fromisoformat(sch["from"]), date.fromisoformat(sch["to"])
    stamp = datetime.now(TZ).strftime("%Y%m%dT%H%M%S")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//pfk-rozklad//UK", "CALSCALE:GREGORIAN", "METHOD:PUBLISH",
           f"X-WR-CALNAME:Розклад {CFG['group']}", "X-WR-TIMEZONE:Europe/Kyiv",
           "BEGIN:VTIMEZONE", "TZID:Europe/Kyiv",
           "BEGIN:STANDARD", "DTSTART:19701025T040000", "RRULE:FREQ=YEARLY;BYMONTH=10;BYDAY=-1SU", "TZOFFSETFROM:+0300", "TZOFFSETTO:+0200", "TZNAME:EET", "END:STANDARD",
           "BEGIN:DAYLIGHT", "DTSTART:19700329T030000", "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=-1SU", "TZOFFSETFROM:+0200", "TZOFFSETTO:+0300", "TZNAME:EEST", "END:DAYLIGHT",
           "END:VTIMEZONE"]
    d = max(d0, today() - timedelta(days=7))
    while d <= d1:
        for it in day_plan(d)["items"]:
            if it.get("cancelled"):
                continue
            uid = hashlib.md5(f"{d}{it['pair']}".encode()).hexdigest()
            out += ["BEGIN:VEVENT", f"UID:{uid}@pfk-rozklad", f"DTSTAMP:{stamp}",
                    f"DTSTART;TZID=Europe/Kyiv:{d:%Y%m%d}T{it['start'].replace(':', '')}00",
                    f"DTEND;TZID=Europe/Kyiv:{d:%Y%m%d}T{it['end'].replace(':', '')}00",
                    f"SUMMARY:{(it.get('type', '') + ' ' + it['subject']).strip()}{' (заміна)' if it.get('changed') else ''}",
                    f"LOCATION:{it.get('room', '')}", f"DESCRIPTION:{it.get('teacher', '')} · {it['pair']} пара", "END:VEVENT"]
        d += timedelta(days=1)
    out.append("END:VCALENDAR")
    target.write_text("\r\n".join(out) + "\r\n", encoding="utf-8")


def cmd_show(a):
    d = date.fromisoformat(a.date) if a.date else today()
    print(re.sub(r"<[^>]+>", "", fmt_day(day_plan(d), "День")))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("update"); sub.add_parser("site")
    n = sub.add_parser("notify"); n.add_argument("--mode", choices=["morning", "evening", "watch"], default="watch")
    s = sub.add_parser("show"); s.add_argument("--date")
    a = ap.parse_args()
    {"update": cmd_update, "notify": cmd_notify, "show": cmd_show, "site": lambda _a: build_site()}[a.cmd](a)


if __name__ == "__main__":
    main()
