"""Розбір таблиць із PDF коледжу.

Лінії таблиць у PDF коледжу намальовані не як «лінії», а як обрізані заливки, тому
pymupdf.find_tables() їх часто не бачить. Тут ми прибираємо текст, растеризуємо сторінку,
знаходимо лінії сітки за пікселями й будуємо клітинки (з урахуванням об'єднаних).
"""
import numpy as np
import pymupdf

ZOOM = 4  # 288 dpi: лінії товщиною 0.5pt дають ≥2 px
PARTIAL = (0.2, 0.85)
MIN_SPLIT_ROW = 16


class Cell:
    __slots__ = ("r0", "r1", "c0", "c1", "rect", "text")

    def __init__(self, r0, r1, c0, c1, rect):
        self.r0, self.r1, self.c0, self.c1, self.rect, self.text = r0, r1, c0, c1, rect, ""

    def __repr__(self):
        return f"Cell(r{self.r0}-{self.r1} c{self.c0}-{self.c1} {self.text[:30]!r})"


def _clusters(idx, gap=3):
    out = []
    for i in idx:
        if out and i - out[-1][-1] <= gap:
            out[-1].append(i)
        else:
            out.append([i])
    return [(c[0], c[-1]) for c in out]


def _long_runs(mask, axis, min_len):
    """Індекси рядків (axis=1) або стовпців (axis=0), де є суцільний темний відрізок ≥ min_len."""
    m = mask if axis == 1 else mask.T
    res = []
    for i, row in enumerate(m):
        if row.sum() < min_len:
            continue
        d = np.diff(np.concatenate(([0], row.view(np.int8), [0])))
        starts, ends = np.where(d == 1)[0], np.where(d == -1)[0]
        if len(starts) and (ends - starts).max() >= min_len:
            res.append(i)
    return res


def _mask(page):
    """Чорно-біла маска ліній сторінки (без тексту)."""
    doc = pymupdf.open()
    doc.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
    p = doc[0]
    for w in p.get_text("words"):
        p.add_redact_annot(pymupdf.Rect(w[:4]))
    p.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE, graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                       text=pymupdf.PDF_REDACT_TEXT_REMOVE)
    pix = p.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM), colorspace=pymupdf.csGRAY)
    a = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.h, pix.w)
    return a < 190  # тонкі лінії світліші (~120–160), сіра заливка (~220) — не лінія


def _vprofile(m, x, y0, y1):
    """Вертикальний профіль лінії в межах рядка [y0, y1] на позиції x."""
    xi = int(round(x))
    ya, yb = int(round(y0)) + 2, int(round(y1)) - 1
    if yb <= ya:
        return np.ones(0, dtype=bool)
    return m[ya:yb, max(0, xi - 3):xi + 4].any(axis=1)


def _implicit_splits(m, xs, ys):
    """Шукає неповні вертикальні лінії (20–85% висоти від краю) і ділить рядок уявною межею."""
    extra = []
    min_gap = 4 * ZOOM
    for i in range(len(ys) - 1):
        y0, y1 = ys[i], ys[i + 1]
        if (y1 - y0) / ZOOM <= MIN_SPLIT_ROW:
            continue
        ya = int(round(y0)) + 2
        yb = int(round(y1)) - 1
        h = yb - ya
        if h <= min_gap:
            continue
        row_pts = []
        for x in xs[1:-1]:
            seg = _vprofile(m, x, y0, y1)
            if not len(seg):
                continue
            mean = float(seg.mean())
            if not (PARTIAL[0] <= mean <= PARTIAL[1]):
                continue
            idx = np.where(seg)[0]
            first, last = int(idx[0]), int(idx[-1])
            edge_tol = max(4, int(0.06 * h))
            if first <= edge_tol and (last + 1) / h <= PARTIAL[1] and seg[:last + 1].mean() >= 0.8 and seg[last + 1:].mean() <= 0.05:
                if h - 1 - last >= min_gap:
                    row_pts.append(ya + last + 1)
            elif h - 1 - last <= edge_tol and (h - first) / h <= PARTIAL[1] and seg[first:].mean() >= 0.8 and seg[:first].mean() <= 0.05:
                if first >= min_gap:
                    row_pts.append(ya + first)
        if row_pts:
            row_pts.sort()
            for c0, c1 in _clusters(row_pts, gap=3 * ZOOM):
                extra.append((c0 + c1) / 2)
    return sorted(ys + extra)


def cells(page):
    """Список клітинок таблиці на сторінці (лише прямокутні клітинки)."""
    m = _mask(page)
    min_len = 4 * ZOOM
    ys = [(a + b) / 2 for a, b in _clusters(_long_runs(m, 1, min_len))]
    xs = [(a + b) / 2 for a, b in _clusters(_long_runs(m, 0, min_len))]
    if len(xs) < 3 or len(ys) < 3:
        return []
    ys = _implicit_splits(m, xs, ys)
    nr, nc = len(ys) - 1, len(xs) - 1

    def has_v(j, i):  # вертикальна лінія x=xs[j] в рядку i
        seg = _vprofile(m, xs[j], ys[i], ys[i + 1])
        return True if not len(seg) else seg.mean() > 0.6

    def has_h(i, j):  # горизонтальна лінія y=ys[i] між стовпцями j
        y = int(round(ys[i])); x0, x1 = int(xs[j]) + 2, int(xs[j + 1]) - 1
        if x1 <= x0:
            return True
        seg = m[max(0, y - 3):y + 4, x0:x1].any(axis=0)
        return seg.mean() > 0.6

    out = []
    prev_row = {}
    for i in range(nr):
        cur_row = {}
        c0 = 0
        for j in range(nc):
            if j + 1 < nc and not has_v(j + 1, i):
                continue
            c1 = j
            above = prev_row.get((c0, c1))
            if above is not None and not any(has_h(i, k) for k in range(c0, c1 + 1)):
                above.r1 = i
                above.rect = pymupdf.Rect(xs[c0] / ZOOM, ys[above.r0] / ZOOM, xs[c1 + 1] / ZOOM, ys[i + 1] / ZOOM)
                cur_row[(c0, c1)] = above
            else:
                rect = pymupdf.Rect(xs[c0] / ZOOM, ys[i] / ZOOM, xs[c1 + 1] / ZOOM, ys[i + 1] / ZOOM)
                c = Cell(i, i, c0, c1, rect)
                out.append(c)
                cur_row[(c0, c1)] = c
            c0 = j + 1
        prev_row = cur_row
    # текст: кожне слово — у клітинку, де його центр
    words = page.get_text("words")
    buckets = {id(c): [] for c in out}
    for w in words:
        cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
        for c in out:
            if c.rect.x0 <= cx <= c.rect.x1 and c.rect.y0 <= cy <= c.rect.y1:
                buckets[id(c)].append(w); break
    for c in out:
        ws = buckets[id(c)]
        lines = []
        for w in sorted(ws, key=lambda w: (w[1] + w[3]) / 2):
            cy = (w[1] + w[3]) / 2
            if lines and abs(lines[-1][0] - cy) < 2.5:
                lines[-1][1].append(w)
            else:
                lines.append([cy, [w]])
        c.text = " ".join(" ".join(x[4] for x in sorted(ln[1], key=lambda w: w[0])) for ln in lines).strip()
    out.sort(key=lambda c: (c.r0, c.c0))
    return out
