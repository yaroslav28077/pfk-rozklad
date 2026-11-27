"""Справжній parse.py на синтетичних PDF + повний шлях PDF → data/sources → сайт → повідомлення."""
import json
from pathlib import Path

import pymupdf
import pytest

from test_bot import World, bot, kyiv, user  # noqa: I001 (test_bot додає scripts/ у sys.path)
from pdf_fixtures import FONT, changes_pdf, schedule_pdf, split_cell_schedule_pdf

GOLDEN = Path(__file__).resolve().parent / "golden"

REAL_PARSE = (bot.parse.parse_schedule, bot.parse.parse_changes)

pytestmark = pytest.mark.skipif(FONT is None, reason="немає шрифту з кирилицею для генерації PDF")


def test_parse_schedule_on_generated_pdf():
    d = bot.parse.parse_schedule(schedule_pdf(), "РОЗКЛАД 1 курс 09.11-13.11")
    assert (d["from"], d["to"], d["course"]) == ("2026-11-09", "2026-11-13", 1)
    assert [g["name"] for g in d["groups"]] == ["СОБ-11", "ПД-11"]
    mon = {(i["group"], i["pair"]): i for i in d["items"] if i["date"] == "2026-11-09"}
    assert mon[("СОБ-11", 1)]["subject"] == "Математика" and mon[("СОБ-11", 1)]["teacher"] == "Харченко А.В."
    assert mon[("ПД-11", 2)]["subject"] == "Філософія" and mon[("ПД-11", 2)].get("stream")
    assert any(i["date"] == "2026-11-10" and i["subject"] == "Англійська мова" for i in d["items"])


def test_parse_changes_on_generated_pdf():
    rows = bot.parse.parse_changes(changes_pdf())
    assert [(r["date"], r["pair"], r["group"], r["subject"]) for r in rows] == [
        ("2026-11-10", 2, "СОБ-11", "Логіка"), ("2026-11-10", 3, "ПД-11", "вікно")]


def test_full_pipeline_with_real_parser(tmp_path, monkeypatch):
    w = World(tmp_path, monkeypatch)
    monkeypatch.setattr(bot.parse, "parse_schedule", REAL_PARSE[0])
    monkeypatch.setattr(bot.parse, "parse_changes", REAL_PARSE[1])
    w.worker.users = {"1": user(1, "СОБ-11"), "2": user(2, "ПД-11", c=0)}
    w.clock = kyiv(2026, 11, 6, 15)
    # перший запуск (порожній data/) — лише збираємо базу, без розсилок
    w.pdfs["РОЗКЛАД-1-курс-09.11-13.11"] = schedule_pdf()
    w.titles["РОЗКЛАД-1-курс-09.11-13.11"] = "РОЗКЛАД 1 курс 09.11-13.11"
    st, problems = w.run()
    assert problems == [] and st["events"] == []
    g = json.loads((bot.DOCS / "data" / "g" / "sob-11.json").read_text(encoding="utf-8"))
    assert g["days"]["2026-11-09"]["items"][0]["subject"] == "Математика"
    # з'явились «Зміни до розкладу» → сповіщення СОБ-11, а ПД-11 (заміни вимкнено) — ні
    w.pdfs["Зміни-до-розкладу-10.11"] = changes_pdf()
    w.titles["Зміни-до-розкладу-10.11"] = "Зміни до розкладу 10.11"
    st, problems = w.run()
    assert problems == [] and [e["type"] for e in st["events"]] == ["changes"]
    assert any("Нові заміни" in t and "Логіка" in t for t in w.to(1)) and w.to(2) == []
    g = json.loads((bot.DOCS / "data" / "g" / "sob-11.json").read_text(encoding="utf-8"))
    assert any(it.get("changed") and it["subject"] == "Логіка" for it in g["days"]["2026-11-10"]["items"])
    # змінений PDF розкладу з тією ж назвою → «Оновлено розклад»
    w.pdfs["РОЗКЛАД-1-курс-09.11-13.11"] = schedule_pdf(subject="Алгебра")
    st, _ = w.run()
    assert [e.get("changed") for e in st["events"]] == [True]
    assert any("Оновлено розклад" in t for t in w.to(1))


def test_split_cell_without_horizontal_line():
    d = bot.parse.parse_schedule(split_cell_schedule_pdf(), "РОЗКЛАД 3 курс 09.11-13.11")
    mon = {(i["group"], i["pair"]): i for i in d["items"] if i["date"] == "2026-11-09"}
    assert mon[("ПК-31-А", 3)]["subject"] == "Основи національного спротиву"
    assert mon[("ПК-31-А", 3)]["teacher"] == "Бородай С.О."
    assert mon[("ПК-32-А", 3)]["subject"] == "Криміналістика"
    assert mon[("ПК-33-А", 3)]["subject"] == "Право"


def test_golden_real_pdfs_and_no_lost_table_text():
    specs = [
        ("rozklad-1-kurs-09_1.11-po-27-1", "schedule", "РОЗКЛАД 1 курс 09.11-27.11"),
        ("rozklad-2-kurs-09_1.11-po-27-1", "schedule", "РОЗКЛАД 2 курс 09.11-27.11"),
        ("rozklad-3-kurs-z-09_1.11-po-27-1", "schedule", "РОЗКЛАД 3 курс 09.11-27.11"),
        ("rozklad-2-kurs-30.11-25", "schedule", "РОЗКЛАД 2 курс 30.11-25.12"),
        ("zminy-do-rozkladu-z-23_1.11", "changes", "Зміни до розкладу з 23.11"),
    ]
    doubts_nov = []
    for stem, kind, title in specs:
        raw = (GOLDEN / f"{stem}.pdf").read_bytes()
        expected = json.loads((GOLDEN / f"{stem}.json").read_text(encoding="utf-8"))
        actual = bot.parse.parse_schedule(raw, title) if kind == "schedule" else bot.parse.parse_changes(raw)
        assert actual == expected
        if kind == "schedule" and "09_1.11" in stem:
            doubts_nov.extend(actual.get("doubts") or [])
        # Жоден текст таблиці не загублено: кожне слово таблиці в області сітки має непорожню клітинку
        doc = pymupdf.open(stream=raw, filetype="pdf")
        for page in doc:
            cs = bot.parse.cells(page)
            assert cs
            x0 = min(c.rect.x0 for c in cs)
            y0 = min(c.rect.y0 for c in cs)
            x1 = max(c.rect.x1 for c in cs)
            y1 = max(c.rect.y1 for c in cs)
            for w in page.get_text("words"):
                cx, cy = (w[0] + w[2]) / 2, (w[1] + w[3]) / 2
                if x0 <= cx <= x1 and y0 <= cy <= y1:
                    assert any(c.rect.x0 <= cx <= c.rect.x1 and c.rect.y0 <= cy <= c.rect.y1 and c.text for c in cs), w[4]

    # Ключові регресійні асерти з аудиту
    nov3 = json.loads((GOLDEN / "rozklad-3-kurs-z-09_1.11-po-27-1.json").read_text(encoding="utf-8"))
    assert any(i["date"] == "2026-11-27" and i["pair"] == 3 and i["group"] == "ПК-31-А"
               and i["subject"] == "Основи національного спротиву" and i["teacher"] == "Бородай С.О."
               for i in nov3["items"])
    nov1 = json.loads((GOLDEN / "rozklad-1-kurs-09_1.11-po-27-1.json").read_text(encoding="utf-8"))
    assert any(i["date"] == "2026-11-23" and i["pair"] == 2 and i["group"] == "ПД-14"
               and i["subject"] == "Іноземна мова" and i["teacher"] == "Гнідаш К.І."
               for i in nov1["items"])
    nov2 = json.loads((GOLDEN / "rozklad-2-kurs-09_1.11-po-27-1.json").read_text(encoding="utf-8"))
    assert any(i["date"] == "2026-11-23" and i["pair"] == 3 and i["group"] == "ПД-21"
               and i["subject"] == "Українська мова" for i in nov2["items"])
    assert len(doubts_nov) == 3
    doubt_groups = {tuple(d["groups"]) for d in doubts_nov}
    assert doubt_groups == {("ПК-32-А",), ("ПК-36-А",), ("СОІ-22", "СОБ-21")}
