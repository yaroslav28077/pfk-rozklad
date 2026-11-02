"""Розбір таблиць із PDF коледжу.

Лінії таблиць у PDF коледжу намальовані не як «лінії», а як обрізані заливки, тому
pymupdf.find_tables() їх часто не бачить. Тут ми прибираємо текст, растеризуємо сторінку,
знаходимо лінії сітки за пікселями й будуємо клітинки (з урахуванням об'єднаних).
"""
import numpy as np
import pymupdf

ZOOM = 4  # 288 dpi: лінії товщиною 0.5pt дають ≥2 px


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


def cells(page):
    """Список клітинок таблиці на сторінці (об'єднані клітинки — одна Cell)."""
    m = _mask(page)
    min_len = 4 * ZOOM
    ys = [(a + b) / 2 for a, b in _clusters(_long_runs(m, 1, min_len))]
    xs = [(a + b) / 2 for a, b in _clusters(_long_runs(m, 0, min_len))]
    if len(xs) < 3 or len(ys) < 3:
        return []
    nr, nc = len(ys) - 1, len(xs) - 1

    def has_v(j, i):  # вертикальна лінія x=xs[j] між рядками i
        x = int(round(xs[j])); y0, y1 = int(ys[i]) + 2, int(ys[i + 1]) - 1
        if y1 <= y0:
            return True
        seg = m[y0:y1, max(0, x - 3):x + 4].any(axis=1)
        return seg.mean() > 0.6

    def has_h(i, j):  # горизонтальна лінія y=ys[i] між стовпцями j
        y = int(round(ys[i])); x0, x1 = int(xs[j]) + 2, int(xs[j + 1]) - 1
        if x1 <= x0:
            return True
        seg = m[max(0, y - 3):y + 4, x0:x1].any(axis=0)
        return seg.mean() > 0.6

    parent = list(range(nr * nc))

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]; a = parent[a]
        return a

    def union(a, b):
        parent[find(a)] = find(b)

    for i in range(nr):
        for j in range(nc):
            if j + 1 < nc and not has_v(j + 1, i):
                union(i * nc + j, i * nc + j + 1)
            if i + 1 < nr and not has_h(i + 1, j):
                union(i * nc + j, (i + 1) * nc + j)
    groups = {}
    for i in range(nr):
        for j in range(nc):
            groups.setdefault(find(i * nc + j), []).append((i, j))
    out = []
    for atoms in groups.values():
        r0, r1 = min(a[0] for a in atoms), max(a[0] for a in atoms)
        c0, c1 = min(a[1] for a in atoms), max(a[1] for a in atoms)
        rect = pymupdf.Rect(xs[c0] / ZOOM, ys[r0] / ZOOM, xs[c1 + 1] / ZOOM, ys[r1 + 1] / ZOOM)
        out.append(Cell(r0, r1, c0, c1, rect))
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
