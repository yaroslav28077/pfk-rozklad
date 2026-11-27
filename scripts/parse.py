"""Перетворення PDF коледжу на структуровані дані.

parse_schedule(pdf) — «РОЗКЛАД ЗАНЯТЬ … курсу» → групи та пари на кожну дату.
parse_changes(pdf)  — «ЗМІНИ ДО РОЗКЛАДУ» → список замін.
"""
import hashlib
import json
import re
from datetime import date, timedelta

import pymupdf

from pdfgrid import cells

VERSION = 4
GROUP_RE = re.compile(r"[А-ЯІЇЄҐ]{1,5}-\d{2}[А-ЯІЇЄҐа-яіїєґ\-]*")
TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})")
DAYS = {"ПОНЕДІЛОК": 0, "ВІВТОРОК": 1, "СЕРЕДА": 2, "ЧЕТВЕР": 3, "П'ЯТНИЦЯ": 4, "СУБОТА": 5}
DAY_NAMES = ["понеділок", "вівторок", "середа", "четвер", "п'ятниця", "субота", "неділя"]
MONTHS = {"січ": 1, "лют": 2, "бер": 3, "кві": 4, "тра": 5, "чер": 6, "лип": 7, "сер": 8, "вер": 9, "жов": 10, "лис": 11, "гру": 12}
ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4}
KINDS = {"лк": "Лк", "пр": "Пр", "лаб": "Лаб", "сем": "Сем"}
LESSON_MARK_RE = re.compile(r"(?:^|(?<=\s))(?:Лк|Пр|Лаб|Сем)\.(?=\s|[А-ЯІЇЄҐ])")
TEACHER_RE = re.compile(
    r"(?:,\s*|\s+)((?:(?:[а-яіїє]{1,3}\.){1,4}\s*)?[А-ЯІЇЄҐ][а-яіїєґ'’ʼ\-]+,?\s+[А-ЯІЇЄҐ]\.\s?[А-ЯІЇЄҐ]\.?)\s*$")
DM = r"(\d{1,2})\.(\d{2})(?!\.\d)"


def _apos(s):
    return s.replace("’", "'").replace("ʼ", "'").replace("`", "'").replace("\xad", "-")


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


def _resolve_period_info(full, title):
    d0, d1 = _period(full)
    if not d0:
        d0, d1 = _period(title.replace("-", " - "))
    if not d0:
        return None, None, None
    if d1 < d0:
        # У шапці бувають описки (напр. «з 30.11.2026 по 25.11.2026» замість 25.12): звіряємо із заголовком або зсуваємо на місяць уперед.
        fixed = None
        tm = re.search(r"(\d{1,2})\.(\d{2})\s*[-–—]\s*(\d{1,2})\.(\d{2})", title or "")
        if tm:
            td0, tm0, td1, tm1 = map(int, tm.groups())
            if (td0, tm0) == (d0.day, d0.month):
                y1 = d0.year + (1 if tm1 < tm0 else 0)
                cand = date(y1, tm1, td1)
                if cand >= d0:
                    fixed = cand
        if not fixed and d1.month == d0.month and d1.day < d0.day:
            y1, m1 = (d0.year + 1, 1) if d0.month == 12 else (d0.year, d0.month + 1)
            fixed = date(y1, m1, d1.day)
        if fixed:
            info = {"raw_from": d0.isoformat(), "raw_to": d1.isoformat(), "fixed_to": fixed.isoformat(),
                    "raw_text": f"з {d0:%d.%m.%Y} по {d1:%d.%m.%Y}"}
            return d0, fixed, info
    return d0, d1, None


def _resolve_period(full, title):
    d0, d1, _ = _resolve_period_info(full, title)
    return d0, d1


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
    t = re.sub(r"\b(Лк|Пр|Лаб|Сем)\.?\s+\1\.?\b", r"\1.", t, flags=re.I)
    m = re.match(r"^(Лк|Пр|Лаб|Сем)\b\.?\s*", t, re.I)
    if m:
        out["type"] = KINDS[m.group(1).lower()]; t = t[m.end():]
    m = TEACHER_RE.search(t)
    if m and m.start() > 0:
        teacher = re.sub(r"([А-ЯІЇЄҐа-яіїєґ'’ʼ\-]),\s*([А-ЯІЇЄҐ]\.)", r"\1 \2", m.group(1).strip())
        out["teacher"] = " ".join(teacher.split())
        t = t[:m.start()]
    out["subject"] = t.strip(" ,;.") or text.strip()
    return out


def _suggest_in_period(bad_iso, d0, d1):
    """Шукає той самий день місяця у межах [d0, d1] (напр. 10.11 → 10.12)."""
    bd = date.fromisoformat(bad_iso)
    cur = date(d0.year, d0.month, 1)
    while cur <= d1:
        try:
            cand = date(cur.year, cur.month, bd.day)
            if d0 <= cand <= d1:
                return cand.isoformat()
        except ValueError:
            pass
        cur = date(cur.year + 1, 1, 1) if cur.month == 12 else date(cur.year, cur.month + 1, 1)
    return None


def _suggest_weekday(bad_iso, target_wd, d0, d1):
    """Шукає сусідню дату (±1..3 дні) у періоді [d0, d1], що відповідає дню тижня рядка."""
    bd = date.fromisoformat(bad_iso)
    for delta in (1, -1, 2, -2, 3, -3):
        cand = bd + timedelta(days=delta)
        if d0 <= cand <= d1 and cand.weekday() == target_wd:
            return cand.isoformat()
    return None


def find_doubts(items, lessons=None, period=None, inverted_period=None):
    """Виявляє конфлікти, нерозділені клітинки (`merged`) та помилкові дати (`date`), позначає `doubt`."""
    slots = {}
    for it in items:
        key = (it["group"], it["date"], it["pair"], it.get("subgroup", 0))
        slots.setdefault(key, []).append(it)
    grouped = {}
    for key, slot_items in slots.items():
        grp, iso, pair, _ = key
        is_merged = any(it.get("_merged") for it in slot_items)
        opts_map = {}
        for it in slot_items:
            opt = (it.get("type", ""), it["subject"], it.get("teacher", ""))
            opts_map[opt] = {"type": opt[0], "subject": opt[1], "teacher": opt[2]}
        if not is_merged and len(opts_map) < 2:
            continue
        if (not is_merged and all(it.get("choice") for it in slot_items)
                and len({it["subject"] for it in slot_items}) == len(slot_items)
                and all(it.get("teacher") for it in slot_items)
                and len({it["teacher"] for it in slot_items}) == len(slot_items)):
            continue
        kind = "merged" if is_merged else "conflict"
        opts = sorted(opts_map.values(), key=lambda o: (o["type"], o["subject"], o["teacher"]))
        sig = (kind, pair, tuple((o["type"], o["subject"], o["teacher"]) for o in opts))
        if sig not in grouped:
            did = hashlib.sha1(json.dumps({"kind": kind, "pair": pair, "options": opts}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]
            grouped[sig] = {"id": did, "kind": kind, "pair": pair, "start": slot_items[0]["start"],
                            "options": opts, "groups": set(), "dates": set(), "items": []}
        grouped[sig]["groups"].add(grp)
        grouped[sig]["dates"].add(iso)
        grouped[sig]["items"].extend(slot_items)
    for it in items:
        it.pop("_merged", None)
    doubts = []
    for g in grouped.values():
        for it in g["items"]:
            it["doubt"] = g["id"]
        doubts.append({"id": g["id"], "kind": g["kind"], "pair": g["pair"], "start": g["start"],
                       "options": g["options"], "groups": sorted(g["groups"]), "dates": sorted(g["dates"])})

    # Перевірка дат («по 10.11» до початку періоду, невідповідність дню тижня, перевернутий період у шапці)
    date_doubts = {}
    if inverted_period:
        rf = date.fromisoformat(inverted_period["raw_from"])
        rt = date.fromisoformat(inverted_period["raw_to"])
        ft = date.fromisoformat(inverted_period["fixed_to"])
        hint = f"У шапці вказано «{inverted_period['raw_text']}» ({rt:%d.%m} раніше за {rf:%d.%m}). Можливо, {ft:%d.%m.%Y}?"
        did = hashlib.sha1(json.dumps({"kind": "date", "reason": "inverted_period", "raw": inverted_period}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]
        doubts.append({
            "id": did, "kind": "date", "reason": "inverted_period", "pair": 0, "start": "",
            "groups": [], "dates": [inverted_period["raw_from"]], "options": [],
            "bad_date": inverted_period["raw_to"], "suggested": inverted_period["fixed_to"],
            "raw_text": inverted_period["raw_text"], "hint": hint,
        })
    if lessons and period:
        d0, d1 = date.fromisoformat(period[0]), date.fromisoformat(period[1])
        d0_iso, d1_iso = period[0], period[1]

        def add_date_doubt(les, reason, field, bad_iso, sug, hint):
            sig = ("date", reason, les["pair"], les["day"], field, bad_iso, sug or "", les["subject"], les.get("teacher", ""))
            if sig not in date_doubts:
                did = hashlib.sha1(json.dumps({"sig": sig}, ensure_ascii=False, sort_keys=True).encode()).hexdigest()[:10]
                lpayload = {k: les[k] for k in ("pair", "start", "end", "day", "type", "subject", "teacher",
                                                 "online", "stream", "subgroup", "choice", "from", "to", "dates") if k in les}
                date_doubts[sig] = {
                    "id": did, "kind": "date", "reason": reason, "pair": les["pair"], "start": les["start"],
                    "day": les["day"], "groups": set(), "dates": [bad_iso], "options": [],
                    "field": field, "bad_date": bad_iso, "suggested": sug,
                    "raw_text": les.get("_raw", les["subject"]), "hint": hint, "lesson": lpayload,
                }
            date_doubts[sig]["groups"].add(les["group"])

        for les in lessons:
            if les.get("to") and les["to"] < d0_iso:
                bd = date.fromisoformat(les["to"])
                sug = _suggest_in_period(les["to"], d0, d1)
                hint = f"Дата «по {bd:%d.%m}» раніше за початок періоду ({d0:%d.%m}–{d1:%d.%m})" + (
                    f". Можливо, {date.fromisoformat(sug):%d.%m}?" if sug else "")
                add_date_doubt(les, "out_of_period", "to", les["to"], sug, hint)
            if les.get("from") and les["from"] > d1_iso:
                bd = date.fromisoformat(les["from"])
                sug = _suggest_in_period(les["from"], d0, d1)
                hint = f"Дата «з {bd:%d.%m}» пізніше за кінець періоду ({d0:%d.%m}–{d1:%d.%m})" + (
                    f". Можливо, {date.fromisoformat(sug):%d.%m}?" if sug else "")
                add_date_doubt(les, "out_of_period", "from", les["from"], sug, hint)
            for dt_iso in les.get("dates") or []:
                dt = date.fromisoformat(dt_iso)
                if dt < d0 or dt > d1:
                    sug = _suggest_in_period(dt_iso, d0, d1)
                    hint = f"Дата {dt:%d.%m} поза періодом ({d0:%d.%m}–{d1:%d.%m})" + (
                        f". Можливо, {date.fromisoformat(sug):%d.%m}?" if sug else "")
                    add_date_doubt(les, "out_of_period", "dates", dt_iso, sug, hint)
                elif dt.weekday() != les["day"]:
                    sug = _suggest_weekday(dt_iso, les["day"], d0, d1)
                    hint = f"{dt:%d.%m} — {DAY_NAMES[dt.weekday()]}, а клітинка у рядку «{DAY_NAMES[les['day']]}»" + (
                        f". Можливо, {date.fromisoformat(sug):%d.%m}?" if sug else "")
                    add_date_doubt(les, "weekday_mismatch", "dates", dt_iso, sug, hint)
        for dd in date_doubts.values():
            dd["groups"] = sorted(dd["groups"])
            doubts.append(dd)

    doubts.sort(key=lambda d: (d["pair"], (d["groups"] or [""])[0], d["dates"][0], d["id"]))
    return doubts


def _alternatives(text):
    parts = [p.strip() for p in re.split(r"\s+/\s+|\s/(?=\s*(?:Лк|Пр|Лаб|Сем)\b)", text) if p.strip()]
    return parts or [text]


def parse_schedule(pdf_bytes, title=""):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    full = " ".join(p.get_text() for p in doc)
    d0, d1, inverted_info = _resolve_period_info(full, title)
    if not d0:
        raise ValueError("не знайдено період дії розкладу («з … по …»)")
    course = _course(full) or _course(title)
    groups, lessons = {}, []
    prev_gcols, prev_first_x, last_day = None, None, None
    for page in doc:
        cs = [c for c in cells(page)]
        if not cs:
            continue
        for c in cs:
            c.text = c.text.replace("\xad", "-")
        # рядок заголовка з назвами груп
        heads = {}
        for c in cs:
            if GROUP_RE.fullmatch(c.text.replace(" ", "")):
                heads.setdefault(c.r0, []).append(c)
        if heads:
            hrow = max(heads, key=lambda r: len(heads[r]))
            gcols = sorted(heads[hrow], key=lambda c: c.c0)
            first_col = gcols[0].c0
            prev_gcols = gcols
            prev_first_x = gcols[0].rect.x0
            is_continuation = False
            min_data_row = hrow + 2
            for g in gcols:
                name = g.text.replace(" ", "")
                room = next((c.text for c in cs if c.r0 == g.r1 + 1 and c.c0 <= g.c0 <= c.c1 and c.c1 <= g.c1 and ("ауд" in c.text or c.text.strip().isdigit())), "")
                groups[name] = {"name": name, "course": course, "room": room.replace("ауд.", "").strip()}
        elif prev_gcols:
            gcols = prev_gcols
            first_col = None
            is_continuation = True
            min_data_row = 0
        else:
            continue
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
            if not is_continuation:
                if c.c1 >= first_col:
                    continue
            else:
                if (c.rect.x0 + c.rect.x1) / 2 >= prev_first_x - 3:
                    continue
            m = TIME_RE.search(c.text.replace(" ", ""))
            if not m:
                continue
            num = next((x.text for x in cs if x.c1 < c.c0 and x.r0 <= c.r0 <= x.r1 and x.text.strip().isdigit()), "")
            day = [d for d in day_rows if d[0] < c.r0]
            if day:
                day_info = day[-1][1:]
            elif last_day is not None:
                day_info = last_day
            else:
                continue
            bands.append({"r0": c.r0, "r1": c.r1, "day": day_info[0], "online": day_info[1],
                          "pair": int(num) if num else None,
                          "start": f"{int(m.group(1)):02d}:{m.group(2)}", "end": f"{int(m.group(3)):02d}:{m.group(4)}"})
        if day_rows:
            last_day = day_rows[-1][1:]
        for b in bands:  # номер пари, якщо в PDF його немає
            if b["pair"] is None:
                same = sorted(x["r0"] for x in bands if x["day"] == b["day"])
                b["pair"] = same.index(b["r0"]) + 1
        for c in cs:
            if not c.text.strip() or c.r0 < min_data_row:
                continue
            if not is_continuation:
                if c.c1 < first_col:
                    continue
            else:
                if (c.rect.x0 + c.rect.x1) / 2 < prev_first_x - 3:
                    continue
            if any(c.r0 == d[0] for d in day_rows):
                continue
            for b in bands:
                ov = min(c.r1, b["r1"]) - max(c.r0, b["r0"]) + 1
                if ov <= 0 or ov < (b["r1"] - b["r0"] + 1) / 2 and ov < (c.r1 - c.r0 + 1) / 2:
                    continue
                if not is_continuation:
                    matched_groups = [g for g in gcols if not (c.c1 < g.c0 or c.c0 > g.c1)]
                else:
                    matched_groups = [
                        g for g in gcols
                        if (min(c.rect.x1, g.rect.x1) - max(c.rect.x0, g.rect.x0))
                        > 0.35 * min(max(1.0, c.rect.width), max(1.0, g.rect.width))
                    ]
                for g in matched_groups:
                    sub = None
                    if not is_continuation:
                        if g.c1 > g.c0 and not (c.c0 <= g.c0 and c.c1 >= g.c1):
                            sub = max(c.c0, g.c0) - g.c0 + 1
                        is_stream = c.c1 > g.c1 or c.c0 < g.c0
                    else:
                        if c.rect.width < g.rect.width * 0.72:
                            cx = (c.rect.x0 + c.rect.x1) / 2
                            gx = (g.rect.x0 + g.rect.x1) / 2
                            sub = 1 if cx < gx else 2
                        is_stream = len(matched_groups) > 1 or c.rect.x0 < g.rect.x0 - 6 or c.rect.x1 > g.rect.x1 + 6
                    cell_les = []
                    span = (c.c1 - c.c0 + 1) if not is_continuation else round(c.rect.width, 1)
                    for alt in _alternatives(c.text):
                        norm_alt = re.sub(r"\b(Лк|Пр|Лаб|Сем)\.?\s+\1\.?\b", r"\1.", _apos(alt), flags=re.I)
                        les = parse_lesson(norm_alt, d0.year)
                        if len(LESSON_MARK_RE.findall(norm_alt)) >= 2:
                            les["_merged"] = True
                        has_dates = bool(les.get("dates") or les.get("from") or les.get("to"))
                        les.update({"group": g.text.replace(" ", ""), "day": b["day"], "pair": b["pair"],
                                    "start": b["start"], "end": b["end"], "row": c.r0 - b["r0"],
                                    "_span": span, "_has_dates": has_dates, "_raw": " ".join(_apos(alt).split())})
                        if sub:
                            les["subgroup"] = sub
                        if b["online"]:
                            les["online"] = True
                        if is_stream:
                            les["stream"] = True  # спільна пара кількох груп (потік)
                        cell_les.append(les)
                    if len(cell_les) >= 2 and not any(x.get("_merged") for x in cell_les):
                        by_dates = {}
                        for x in cell_les:
                            dk = (x.get("from"), x.get("to"), tuple(x.get("dates") or ()))
                            by_dates.setdefault(dk, []).append(x)
                        for grp_les in by_dates.values():
                            if (len(grp_les) >= 2
                                    and len({x["subject"] for x in grp_les}) == len(grp_les)
                                    and all(x.get("teacher") for x in grp_les)
                                    and len({x["teacher"] for x in grp_les}) == len(grp_les)):
                                for x in grp_les:
                                    x["choice"] = True
                    lessons.extend(cell_les)
    if not groups:
        raise ValueError("не знайдено заголовок із назвами груп")
    # розгортаємо на конкретні дати
    items = []
    d = d0
    while d <= d1:
        iso = d.isoformat()
        active = []
        for les in lessons:
            if les["day"] != d.weekday():
                continue
            if les.get("dates") and iso not in les["dates"]:
                continue
            if les.get("from") and iso < les["from"] or les.get("to") and iso > les["to"]:
                continue
            active.append(les)
        by_gp = {}
        for les in active:
            by_gp.setdefault((les["group"], les["pair"]), []).append(les)
        for gp_les in by_gp.values():
            dated = [x for x in gp_les if x.get("_has_dates")]
            for les in gp_les:
                # У поділеній парі вузька клітинка з явними датами уточнює ширшу потокову без дат
                if (les.get("stream") and not les.get("_has_dates")
                        and any(s["row"] != les["row"] and les["_span"] > s["_span"] for s in dated)):
                    continue
                it = {k: v for k, v in les.items()
                      if k not in ("day", "dates", "from", "to", "row", "_span", "_has_dates", "_raw")}
                it["date"] = iso
                items.append(it)
        d += timedelta(days=1)
    uniq_items = {}
    for it in items:
        uk = (it["group"], it["date"], it["pair"], it.get("subgroup", 0), it.get("type", ""), it["subject"], it.get("teacher", ""))
        if uk not in uniq_items:
            uniq_items[uk] = it
        elif it.get("_merged"):
            uniq_items[uk]["_merged"] = True
    items = sorted(uniq_items.values(), key=lambda x: (x["group"], x["date"], x["pair"], x.get("subgroup", 0)))
    doubts = find_doubts(items, lessons, period=(d0.isoformat(), d1.isoformat()), inverted_period=inverted_info)
    return {"from": d0.isoformat(), "to": d1.isoformat(), "course": course,
            "groups": list(groups.values()), "items": items, "doubts": doubts}


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
        for c in cs:
            c.text = c.text.replace("\xad", "-")
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
    for part in re.split(r"[,;/+]|\sта\s|\sі\s", gt):
        p = _norm(part)
        if not p:
            continue
        exact = [g["name"] for g in groups if _norm(g["name"]) == p]
        res += exact or [g["name"] for g in groups if _norm(g["name"]).startswith(p) and not _norm(g["name"])[len(p):][:1].isdigit()]
    return list(dict.fromkeys(res))
