"""Справжній parse.py на синтетичних PDF + повний шлях PDF → data/sources → сайт → повідомлення."""
import json

import pytest

from test_bot import World, bot, kyiv, user  # noqa: I001 (test_bot додає scripts/ у sys.path)
from pdf_fixtures import FONT, changes_pdf, schedule_pdf

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
