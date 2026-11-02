"""Перетворення PDF коледжу на структуровані дані.

parse_schedule(pdf) — «РОЗКЛАД ЗАНЯТЬ … курсу» → групи та пари на кожну дату.
parse_changes(pdf)  — «ЗМІНИ ДО РОЗКЛАДУ» → список замін.
"""
import re
from datetime import date, timedelta

import pymupdf

from pdfgrid import cells

GROUP_RE = re.compile(r"[А-ЯІЇЄҐ]{1,5}-\d{2}[А-ЯІЇЄҐа-яіїєґ\-]*")
TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})")
DAYS = {"ПОНЕДІЛОК": 0, "ВІВТОРОК": 1, "СЕРЕДА": 2, "ЧЕТВЕР": 3, "П'ЯТНИЦЯ": 4, "СУБОТА": 5}
MONTHS = {"січ": 1, "лют": 2, "бер": 3, "кві": 4, "тра": 5, "чер": 6, "лип": 7, "сер": 8, "вер": 9, "жов": 10, "лис": 11, "гру": 12}
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}
KINDS = {"лк": "Лк", "пр": "Пр", "лаб": "Лаб", "сем": "Сем"}
TEACHER_RE = re.compile(
    r"(?:,\s*|\s+)((?:(?:[а-яіїє]{1,3}\.){1,4}\s*)?[А-ЯІЇЄҐ][а-яіїєґ'’ʼ\-]+\s+[А-ЯІЇЄҐ]\.\s?[А-ЯІЇЄҐ]\.?)\s*$")
DM = r"(\d{1,2})\.(\d{2})(?!\.\d)"


def _apos(s):
    return s.replace("’", "'").replace("ʼ", "'").replace("`", "'")


def _norm(s):
    return re.sub(r"[\s\-–]", "", _apos(s or "").upper())


def _despace(t):
    """«К У Р А Т О Р С Ь К А  Г О Д И Н А» → «Кураторська година»."""
    if re.fullmatch(r"(?:\S\s+){3,}\S", t):
        s = t.replace(" ", "")
        if s.upper() == "КУРАТОРСЬКАГОДИНА":
            return "Кураторська година"
        return s.capitalize()
    return t


def _period(text):
    m = re.search(r"з\s*(\d{1,2})\.(\d{1,2})\.(\d{4})\s*(?:р\.?)?\s*(?:по|до|[-–])\s*(\d{1,2})\.(\d{1,2})\.(\d{4})", text)
    if not m:
        return None, None
    g = list(map(int, m.groups()))
    return date(g[2], g[1], g[0]), date(g[5], g[4], g[3])


def _course(text):
    m = re.search(r"(?<![А-ЯA-Z])(IV|III|II|I|\d)\s*[-–]?\s*(?:го\s*)?курс", text.replace("І", "I"))
    if not m:
        return None
    return ROMAN.get(m.group(1)) or int(m.group(1))


def parse_lesson(text, year):
    """'Пр. Математика, Харченко А.В. (05.10, 12.10)' → dict."""
    t = " ".join(_despace(_apos(text)).split())
    out = {}
    if re.search(r"\(?\s*онлайн\s*\)?", t, re.I):
        out["online"] = True
        t = re.sub(r"\(?\s*онлайн\s*\)?", "", t, flags=re.I).strip()

    def dm(d, m):
        return date(year, int(m), int(d)).isoformat()

    m = re.search(rf"\b(?:до|по)\s+{DM}", t)
    if m:
        out["to"] = dm(m.group(1), m.group(2)); t = (t[:m.start()] + t[m.end():]).strip()
    m = re.search(rf"\b(?:з|із|від)\s+{DM}", t)
    if m:
        out["from"] = dm(m.group(1), m.group(2)); t = (t[:m.start()] + t[m.end():]).strip()
    found = re.findall(DM, t)
    if found:
        out["dates"] = [dm(d, mm) for d, mm in found]
        t = re.sub(rf"\(?\s*{DM}(?:\s*[,;]\s*{DM})*\s*\)?", "", t)
    t = re.sub(r"\(\s*[,;\s]*\)", "", t)
    t = " ".join(t.split()).strip(" ,;/-")
    m = re.match(r"^(Лк|Пр|Лаб|Сем)\b\.?\s*", t, re.I)
    if m:
        out["type"] = KINDS[m.group(1).lower()]; t = t[m.end():]
    m = TEACHER_RE.search(t)
    if m and m.start() > 0:
        out["teacher"] = m.group(1).strip(); t = t[:m.start()]
    out["subject"] = t.strip(" ,;.") or text.strip()
    return out


def _alternatives(text):
    parts = [p.strip() for p in re.split(r"\s+/\s+|\s/(?=\s*(?:Лк|Пр|Лаб|Сем)\b)", text) if p.strip()]
    return parts or [text]


def parse_schedule(pdf_bytes, title=""):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    full = " ".join(p.get_text() for p in doc)
    d0, d1 = _period(full)
    if not d0:
        d0, d1 = _period(title.replace("-", " - "))
    if not d0:
        raise ValueError("не знайдено період дії розкладу («з … по …»)")
    course = _course(full) or _course(title)
    groups, lessons = {}, []
    for page in doc:
        cs = [c for c in cells(page)]
        if not cs:
            continue
        # рядок заголовка з назвами груп
        heads = {}
        for c in cs:
            if GROUP_RE.fullmatch(c.text.replace(" ", "")):
                heads.setdefault(c.r0, []).append(c)
        if not heads:
            continue
        hrow = max(heads, key=lambda r: len(heads[r]))
        gcols = sorted(heads[hrow], key=lambda c: c.c0)
        first_col = gcols[0].c0
        for g in gcols:
            name = g.text.replace(" ", "")
            room = next((c.text for c in cs if c.r0 == g.r1 + 1 and c.c0 <= g.c0 <= c.c1 and c.c1 <= g.c1 and ("ауд" in c.text or c.text.strip().isdigit())), "")
            groups[name] = {"name": name, "course": course, "room": room.replace("ауд.", "").strip()}
        # дні тижня та пари
        day_rows = []
        for c in cs:
            up = _apos(c.text).upper().replace(" ", "")
            for k, v in DAYS.items():
                if up.startswith(k.replace(" ", "")):
                    day_rows.append((c.r0, v, "онлайн" in c.text.lower()))
        day_rows.sort()
        bands = []
        for c in cs:
            if c.c1 >= first_col:
                continue
            m = TIME_RE.search(c.text.replace(" ", ""))
            if not m:
                continue
            num = next((x.text for x in cs if x.c1 < c.c0 and x.r0 <= c.r0 <= x.r1 and x.text.strip().isdigit()), "")
            day = [d for d in day_rows if d[0] < c.r0]
            if not day:
                continue
            bands.append({"r0": c.r0, "r1": c.r1, "day": day[-1][1], "online": day[-1][2],
                          "pair": int(num) if num else None,
                          "start": f"{int(m.group(1)):02d}:{m.group(2)}", "end": f"{int(m.group(3)):02d}:{m.group(4)}"})
        for b in bands:  # номер пари, якщо в PDF його немає
            if b["pair"] is None:
                same = sorted(x["r0"] for x in bands if x["day"] == b["day"])
                b["pair"] = same.index(b["r0"]) + 1
        for c in cs:
            if c.c1 < first_col or not c.text.strip() or c.r0 <= hrow + 1:
                continue
            if any(c.r0 == d[0] for d in day_rows):
                continue
            for b in bands:
                ov = min(c.r1, b["r1"]) - max(c.r0, b["r0"]) + 1
                if ov <= 0 or ov < (b["r1"] - b["r0"] + 1) / 2 and ov < (c.r1 - c.r0 + 1) / 2:
                    continue
                for g in gcols:
                    if c.c1 < g.c0 or c.c0 > g.c1:
                        continue
                    sub = None
                    if g.c1 > g.c0 and not (c.c0 <= g.c0 and c.c1 >= g.c1):
                        sub = max(c.c0, g.c0) - g.c0 + 1
                    for alt in _alternatives(c.text):
                        les = parse_lesson(alt, d0.year)
                        les.update({"group": g.text.replace(" ", ""), "day": b["day"], "pair": b["pair"],
                                    "start": b["start"], "end": b["end"], "row": c.r0 - b["r0"]})
                        if sub:
                            les["subgroup"] = sub
                        if b["online"]:
                            les["online"] = True
                        if c.c1 > g.c1 or c.c0 < g.c0:
                            les["stream"] = True  # спільна пара кількох груп (потік)
                        lessons.append(les)
    if not groups:
        raise ValueError("не знайдено заголовок із назвами груп")
    # розгортаємо на конкретні дати
    items = []
    d = d0
    while d <= d1:
        for les in lessons:
            if les["day"] != d.weekday():
                continue
            iso = d.isoformat()
            if les.get("dates") and iso not in les["dates"]:
                continue
            if les.get("from") and iso < les["from"] or les.get("to") and iso > les["to"]:
                continue
            it = {k: v for k, v in les.items() if k not in ("day", "dates", "from", "to", "row")}
            it["date"] = iso
            items.append(it)
        d += timedelta(days=1)
    items.sort(key=lambda x: (x["group"], x["date"], x["pair"], x.get("subgroup", 0)))
    return {"from": d0.isoformat(), "to": d1.isoformat(), "course": course,
            "groups": list(groups.values()), "items": items}


def _parse_day(s, year):
    s = _apos(s).strip().lower()
    m = re.search(r"(\d{1,2})\s*\.\s*(\d{1,2})(?:\.(\d{2,4}))?", s)
    if m:
        y = int(m.group(3)) if m.group(3) else year
        return date(y + 2000 if y < 100 else y, int(m.group(2)), int(m.group(1)))
    m = re.search(r"(\d{1,2})\s*[.\s]*\s*([а-яіїєґ]+)", s)
    if m and m.group(2)[:3] in MONTHS:
        return date(year, MONTHS[m.group(2)[:3]], int(m.group(1)))
    return None


def parse_changes(pdf_bytes):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    out = []
    for page in doc:
        txt = page.get_text()
        ym = re.search(r"\d{1,2}\.\d{1,2}\.(\d{4})", txt)
        year = int(ym.group(1)) if ym else date.today().year
        cs = cells(page)
        if not cs:
            continue
        ncols = max(c.c1 for c in cs) + 1
        at = {}
        for c in cs:
            for r in range(c.r0, c.r1 + 1):
                for k in range(c.c0, c.c1 + 1):
                    at[(r, k)] = c
        keys = {"дата": "date", "пара": "pair", "група": "group", "дисципліна": "subject",
                "викладач": "teacher", "вид": "kind", "зміна": "subject", "аудиторія": "room", "ауд": "room"}
        cols, hdr_end = {}, -1
        for c in cs:
            low = c.text.lower()
            for k, v in keys.items():
                if low.startswith(k):
                    hdr_end = max(hdr_end, c.r1)
        for k in range(ncols):  # найнижча клітинка заголовка в стовпці
            c = at.get((hdr_end, k))
            if not c:
                continue
            low = c.text.lower()
            for kk, v in keys.items():
                if low.startswith(kk):
                    cols[k] = v; break
        if "pair" not in cols.values() or "group" not in cols.values():
            continue
        nrows = max(c.r1 for c in cs) + 1
        cur = None
        for r in range(hdr_end + 1, nrows):
            row = {}
            for k, v in cols.items():
                c = at.get((r, k))
                row[v] = " ".join(c.text.split()) if c else ""
            if row.get("date"):
                cur = _parse_day(row["date"], year) or cur
            pair = re.sub(r"\D", "", row.get("pair", ""))
            if not cur or not pair or not row.get("group"):
                continue
            subj = row.get("subject") or "вікно"
            out.append({"date": cur.isoformat(), "pair": int(pair), "group": row["group"],
                        "subject": subj, "teacher": row.get("teacher", ""),
                        "kind": row.get("kind", ""), "room": row.get("room", "")})
    uniq = {(z["date"], z["pair"], z["group"], z["subject"]): z for z in out}
    return sorted(uniq.values(), key=lambda z: (z["date"], z["pair"], z["group"]))


def change_targets(group_text, groups):
    """До яких груп (зі списку назв) стосується рядок заміни: 'ПД-12', 'СОБ-11, СОІ-12', '1 курс (9)', 'ПК-31'."""
    gt = _apos(group_text or "")
    m = re.search(r"(\d)\s*курс", gt)
    if m:
        return [g["name"] for g in groups if g.get("course") == int(m.group(1))]
    res = []
    for part in re.split(r"[,;/]|\sта\s|\sі\s", gt):
        p = _norm(part)
        if not p:
            continue
        exact = [g["name"] for g in groups if _norm(g["name"]) == p]
        res += exact or [g["name"] for g in groups if _norm(g["name"]).startswith(p) and not _norm(g["name"])[len(p):][:1].isdigit()]
    return list(dict.fromkeys(res))
