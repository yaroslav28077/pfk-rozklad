"""Синтетичні PDF у форматі коледжу (сітка таблиці + кириличний текст) для перевірки справжнього парсера.

Це НЕ заміна перевірки на реальному новому PDF коледжу — лише гарантія, що конвеєр
«PDF → parse.py → data/sources → сайт» працює на файлі з таблицею, а не на підмінених даних.
"""
from pathlib import Path

import pymupdf

FONT = next((p for p in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                         "/usr/share/fonts/dejavu/DejaVuSans.ttf",
                         "/Library/Fonts/Arial Unicode.ttf", "C:/Windows/Fonts/arial.ttf") if Path(p).exists()), None)


def _table(page, xs, rows, top, h=26):
    """rows: [(cells, merges)] — cells: список текстів по стовпцях; merges: множина індексів вертикальних ліній,
    яких у цьому рядку немає (об'єднані клітинки)."""
    page.insert_font(fontname="dv", fontfile=FONT)
    y = top
    page.draw_line((xs[0], y), (xs[-1], y), width=0.8)
    for cells, merged in rows:
        for j, x in enumerate(xs):
            if j not in merged:
                page.draw_line((x, y), (x, y + h), width=0.8)
        page.draw_line((xs[0], y + h), (xs[-1], y + h), width=0.8)
        col = 0
        for text in cells:
            while col in merged:
                col += 1
            if text:
                page.insert_text((xs[col] + 3, y + 16), text, fontname="dv", fontsize=7)
            col += 1
        y += h


def schedule_pdf(course=1, period=("09.11.2026", "13.11.2026"), subject="Математика"):
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="dv", fontfile=FONT)
    page.insert_text((40, 40), f"РОЗКЛАД ЗАНЯТЬ {course} курсу з {period[0]} по {period[1]}", fontname="dv", fontsize=10)
    xs = [40, 70, 150, 330, 520]
    _table(page, xs, [
        (["№", "Час", "СОБ-11", "ПД-11"], set()),
        (["", "", "ауд. 202", "ауд. 105"], set()),
        (["ПОНЕДІЛОК"], {1, 2, 3}),
        (["1", "08:00-09:20", f"Пр. {subject}, Харченко А.В.", "Лк Історія України, Петренко І.І."], set()),
        (["2", "09:30-10:50", "Лк. Філософія, Іваненко О.О."], {3}),
        (["ВІВТОРОК"], {1, 2, 3}),
        (["1", "08:00-09:20", "Пр. Англійська мова, Коваль Н.М.", ""], set()),
    ], top=60)
    return doc.tobytes()


def changes_pdf():
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=842)
    page.insert_font(fontname="dv", fontfile=FONT)
    page.insert_text((40, 40), "ЗМІНИ ДО РОЗКЛАДУ на 10.11.2026", fontname="dv", fontsize=10)
    xs = [40, 110, 150, 230, 420, 560]
    _table(page, xs, [
        (["Дата", "Пара", "Група", "Дисципліна", "Викладач"], set()),
        (["10.11.2026", "2", "СОБ-11", "Логіка", "Сидоренко В.В."], set()),
        (["", "3", "ПД-11", "вікно", ""], set()),
    ], top=60)
    return doc.tobytes()
