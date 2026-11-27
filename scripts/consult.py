"""Розбір і об'єднання графіків консультацій (відпрацювань) викладачів ПФК НЮУ.

parse_consultations_pdf(pdf_bytes, url, title) → list[dict]
merge_consultations(records) → list[dict]
"""
import re
from html.parser import HTMLParser
import urllib.parse

import pymupdf

import parse
from pdfgrid import cells

DAYS = {
    "ПОНЕДІЛОК": (1, "Понеділок"),
    "ВІВТОРОК": (2, "Вівторок"),
    "СЕРЕДА": (3, "Середа"),
    "ЧЕТВЕР": (4, "Четвер"),
    "П'ЯТНИЦЯ": (5, "П'ятниця"),
    "ПЯТНИЦЯ": (5, "П'ятниця"),
}
TIME_RE = re.compile(r"(\d{1,2})[:.](\d{2})\s*[-–—]\s*(\d{1,2})[:.](\d{2})")
SURNAME_ALIASES = {"ХАРЧНКО": "Харченко", "СТЕШЕНКО": "Гнедик"}
TR = dict(zip(
    "абвгґдеєжзиіїйклмнопрстуфхцчшщьюя",
    "a b v h g d e ie zh z y i i i k l m n o p r s t u f kh ts ch sh shch  iu ia".split(" "),
))


def _slug(name):
    s = "".join(TR.get(ch, ch) for ch in name.lower().replace("'", ""))
    return re.sub(r"[^a-z0-9]+", "-", s).strip("-")


def _cell_lines(page, rect):
    """Візуальні рядки всередині прямокутника клітинки за координатами слів."""
    words = [
        w for w in page.get_text("words")
        if rect.x0 - 1 <= (w[0] + w[2]) / 2 <= rect.x1 + 1
        and rect.y0 - 1 <= (w[1] + w[3]) / 2 <= rect.y1 + 1
    ]
    lines = []
    for w in sorted(words, key=lambda x: ((x[1] + x[3]) / 2, x[0])):
        cy = (w[1] + w[3]) / 2
        if lines and abs(lines[-1][0] - cy) < 3.0:
            lines[-1][1].append(w)
        else:
            lines.append([cy, [w]])
    return [" ".join(x[4] for x in sorted(ln[1], key=lambda w: w[0])).strip() for ln in lines if ln[1]]


def _split_subjects(lines):
    """Акуратно розбиває перелік дисциплін клітинки на окремі назви."""
    merged = []
    for raw in lines:
        ln = " ".join(parse._apos(raw).split()).strip()
        if not ln:
            continue
        if merged:
            prev = merged[-1]
            unclosed_paren = prev.count("(") > prev.count(")")
            ends_sep = prev.endswith((";", "."))
            starts_lower = bool(re.match(r"^[а-яіїєґ(]", ln))
            ends_joiner = bool(re.search(r"(?:\b(?:та|і|й|зі|з|у|в|на|за|до|при)|-)$", prev, re.I))
            if unclosed_paren or starts_lower or (not ends_sep and ends_joiner):
                clean_prev = prev.rstrip(";") if starts_lower and prev.endswith(";") else prev
                if clean_prev.endswith("-") and not clean_prev.endswith(" -"):
                    merged[-1] = clean_prev + ln
                else:
                    merged[-1] = f"{clean_prev} {ln}"
                continue
        merged.append(ln)
    out = []
    for chunk in merged:
        # Розбиваємо по ';' лише коли далі починається нова назва з великої літери (ігноруємо описки типу 'складання; процесуальних')
        parts = re.split(r";\s*(?=[А-ЯІЇЄҐA-Z])", chunk)
        for p in parts:
            s = re.sub(r";\s*(?=[а-яіїєґ])", " ", p)
            s = " ".join(s.split()).strip(" .;,-")
            if s:
                out.append(s)
    return list(dict.fromkeys(out))


def canon_full_name(full_name):
    """'Стешенко Юлія Олександрівна' → {teacher, full_name, slug, aliases}."""
    s = " ".join(parse._apos(full_name).split()).strip(" ,.;")
    parts = s.split()
    if len(parts) >= 3:
        sur, fn, mn = parts[0], parts[1], parts[2]
        orig_short = f"{sur} {fn[0].upper()}.{mn[0].upper()}."
        target_sur = SURNAME_ALIASES.get(sur.upper(), sur)
        short = f"{target_sur} {fn[0].upper()}.{mn[0].upper()}."
        aliases = [orig_short] if target_sur != sur else []
    elif len(parts) == 2:
        sur, fn = parts[0], parts[1]
        orig_short = f"{sur} {fn}"
        target_sur = SURNAME_ALIASES.get(sur.upper(), sur)
        short = f"{target_sur} {fn}"
        aliases = [orig_short] if target_sur != sur else []
    else:
        short = s
        aliases = []
    return {
        "teacher": short,
        "full_name": s,
        "slug": f"t-{_slug(short)}",
        "aliases": aliases,
    }


def _detect_commission(text, title=""):
    t = f"{text} {title}".lower()
    if "загальноосвіт" in t or "гуманітар" in t or "czk-zo" in t:
        return "ЦК загальноосвітніх та соціально-гуманітарних дисциплін"
    if "права та правоохорон" in t or "pravo" in t:
        return "ЦК права та правоохоронної діяльності"
    return title or "ПФК НЮУ"


def parse_consultations_pdf(pdf_bytes, url="", title=""):
    doc = pymupdf.open(stream=pdf_bytes, filetype="pdf")
    full_txt = " ".join(p.get_text() for p in doc)
    commission = _detect_commission(full_txt, title)
    records = []
    for page in doc:
        cs = cells(page)
        if not cs:
            continue
        rows = {}
        for c in cs:
            rows.setdefault(c.r0, []).append(c)
        hdr_row = None
        cols = {}
        for r in sorted(rows):
            rcells = sorted(rows[r], key=lambda c: c.c0)
            row_map = {}
            for c in rcells:
                low = " ".join(parse._apos(c.text).lower().split())
                if low == "№" or low.startswith("№ "):
                    row_map["num"] = (c.c0, c.c1)
                elif "прізвищ" in low:
                    row_map["teacher"] = (c.c0, c.c1)
                elif "дисципл" in low:
                    row_map["subjects"] = (c.c0, c.c1)
                elif "день" in low and ("час" in low or r >= 0):
                    row_map["time"] = (c.c0, c.c1)
            if {"teacher", "subjects", "time"}.issubset(row_map):
                hdr_row = r
                cols = row_map
                break
        if hdr_row is None:
            continue
        for r in sorted(rows):
            if r <= hdr_row:
                continue
            rcells = sorted(rows[r], key=lambda c: c.c0)
            by_col = {}
            for key, (c0, c1) in cols.items():
                matched = [c for c in rcells if not (c.c1 < c0 or c.c0 > c1)]
                if matched:
                    by_col[key] = matched[0]
            if not {"teacher", "subjects", "time"}.issubset(by_col):
                continue
            t_raw = " ".join(parse._apos(by_col["teacher"].text).split()).strip()
            if not t_raw or re.search(r"голова\s+циклової", t_raw, re.I):
                continue
            dt_raw = " ".join(parse._apos(by_col["time"].text).split())
            dt_up = dt_raw.upper()
            day_num, day_name = next(((dn, dname) for k, (dn, dname) in DAYS.items() if k in dt_up), (None, ""))
            tm = TIME_RE.search(dt_raw)
            if not day_num or not tm:
                continue
            start = f"{int(tm.group(1)):02d}:{tm.group(2)}"
            end = f"{int(tm.group(3)):02d}:{tm.group(4)}"
            subj_lines = _cell_lines(page, by_col["subjects"].rect)
            subjects = _split_subjects(subj_lines)
            info = canon_full_name(t_raw)
            records.append({
                **info,
                "day": day_name,
                "day_num": day_num,
                "start": start,
                "end": end,
                "subjects": subjects,
                "commissions": [commission],
                "sources": [url] if url else [],
            })
    return records


def merge_consultations(records):
    """Об'єднує записи одного викладача з однаковим часом; різний час лишає окремими слотами."""
    buckets = {}
    for rec in records:
        key = (rec["slug"], rec["day_num"], rec["start"], rec["end"])
        b = buckets.setdefault(key, {
            "id": f"{rec['slug']}:{rec['day_num']}:{rec['start'].replace(':', '')}",
            "teacher": rec["teacher"],
            "full_name": rec["full_name"],
            "slug": rec["slug"],
            "aliases": list(rec.get("aliases") or []),
            "day": rec["day"],
            "day_num": rec["day_num"],
            "start": rec["start"],
            "end": rec["end"],
            "subjects": [],
            "commissions": [],
            "sources": [],
        })
        for al in rec.get("aliases") or []:
            if al not in b["aliases"]:
                b["aliases"].append(al)
        for s in rec.get("subjects") or []:
            if s not in b["subjects"]:
                b["subjects"].append(s)
        for c in rec.get("commissions") or []:
            if c not in b["commissions"]:
                b["commissions"].append(c)
        for u in rec.get("sources") or []:
            if u not in b["sources"]:
                b["sources"].append(u)
    out = list(buckets.values())
    out.sort(key=lambda x: (x["day_num"], x["start"], x["end"], x["teacher"]))
    return out


class _Links(HTMLParser):
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


def find_consultation_pdfs(html, base_url):
    p = _Links(); p.feed(html)
    seen, out = set(), []
    for text, href in p.links:
        unq = urllib.parse.unquote(href).lower()
        if not unq.split("?")[0].endswith(".pdf"):
            continue
        t = f"{text} {unq}".lower()
        if not any(k in t for k in ("консульт", "чергув", "відпрац", "konsult", "cherguv")):
            continue
        url = urllib.parse.quote(urllib.parse.urljoin(base_url, urllib.parse.unquote(href)), safe=":/?=&%#~+")
        if url in seen:
            continue
        seen.add(url)
        out.append({"title": text or unq.rsplit("/", 1)[-1], "url": url})
    return out
