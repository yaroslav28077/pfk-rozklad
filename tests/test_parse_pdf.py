"""Справжній parse.py на синтетичних PDF + повний шлях PDF → data/sources → сайт → повідомлення."""
import json
from pathlib import Path

import pymupdf
import pytest

from test_bot import World, bot, kyiv, user  # noqa: I001 (test_bot додає scripts/ у sys.path)
from pdf_fixtures import FONT, changes_pdf, schedule_pdf, split_cell_schedule_pdf
import consult  # noqa: E402

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
    # Коли і потік, і нижні клітинки мають явну дату, обидва конфлікти лишаються сумнівами
    assert len(d["doubts"]) == 2


def test_golden_real_pdfs_and_no_lost_table_text():
    specs = [
        ("rozklad-1-kurs-09_1.11-po-27-1", "schedule", "РОЗКЛАД 1 курс 09.11-27.11", 0),
        ("rozklad-2-kurs-09_1.11-po-27-1", "schedule", "РОЗКЛАД 2 курс 09.11-27.11", 0),
        ("rozklad-3-kurs-z-09_1.11-po-27-1", "schedule", "РОЗКЛАД 3 курс 09.11-27.11", 0),
        ("rozklad-1-kurs-30.11-25.12", "schedule", "РОЗКЛАД 1 курс 30.11-25.12", 0),
        ("rozklad-2-kurs-30.11-25", "schedule", "РОЗКЛАД 2 курс 30.11-25.12", 2),
        ("rozklad-3-kurs-30.11-25", "schedule", "РОЗКЛАД 3 курс 30.11-25.12", 1),
        ("zminy-do-rozkladu-z-23_1.11", "changes", "Зміни до розкладу з 23.11", 0),
    ]
    for stem, kind, title, exp_doubts in specs:
        raw = (GOLDEN / f"{stem}.pdf").read_bytes()
        expected = json.loads((GOLDEN / f"{stem}.json").read_text(encoding="utf-8"))
        actual = bot.parse.parse_schedule(raw, title) if kind == "schedule" else bot.parse.parse_changes(raw)
        assert actual == expected
        if kind == "schedule":
            assert len(actual.get("doubts") or []) == exp_doubts
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

    # Ключові регресійні асерти з аудиту та усунення хибних сумнівів
    nov3 = json.loads((GOLDEN / "rozklad-3-kurs-z-09_1.11-po-27-1.json").read_text(encoding="utf-8"))
    assert any(i["date"] == "2026-11-27" and i["pair"] == 3 and i["group"] == "ПК-31-А"
               and i["subject"] == "Основи національного спротиву" and i["teacher"] == "Бородай С.О."
               for i in nov3["items"])
    # 13.11 ПК-32-А ще на потоковій лекції, а з 20.11 — лише практика Мороз С.Г. (без дублювання лекції)
    assert [i["subject"] for i in nov3["items"] if i["date"] == "2026-11-13" and i["group"] == "ПК-32-А" and i["pair"] == 3] == ["Основи національного спротиву"]
    for iso in ("2026-11-20", "2026-11-27"):
        pk32 = [i for i in nov3["items"] if i["date"] == iso and i["group"] == "ПК-32-А" and i["pair"] == 3]
        assert len(pk32) == 1 and pk32[0]["subject"] == "Криміналістичні експертизи" and pk32[0]["type"] == "Пр"
        pk36 = [i for i in nov3["items"] if i["date"] == iso and i["group"] == "ПК-36-А" and i["pair"] == 3]
        assert len(pk36) == 1 and pk36[0]["subject"] == "Основи національного спротиву" and pk36[0]["type"] == "Пр"

    nov1 = json.loads((GOLDEN / "rozklad-1-kurs-09_1.11-po-27-1.json").read_text(encoding="utf-8"))
    assert any(i["date"] == "2026-11-23" and i["pair"] == 2 and i["group"] == "ПД-14"
               and i["subject"] == "Іноземна мова" and i["teacher"] == "Гнідаш К.І."
               for i in nov1["items"])
    nov2 = json.loads((GOLDEN / "rozklad-2-kurs-09_1.11-po-27-1.json").read_text(encoding="utf-8"))
    assert any(i["date"] == "2026-11-23" and i["pair"] == 3 and i["group"] == "ПД-21"
               and i["subject"] == "Українська мова" for i in nov2["items"])
    sob_choices = [i for i in nov2["items"] if i["date"] == "2026-11-17" and i["group"] == "СОБ-21" and i["pair"] == 4]
    assert len(sob_choices) == 2 and all(i.get("choice") is True and "doubt" not in i for i in sob_choices)


def _mini_schedule(cell_text):
    from pdf_fixtures import FONT, _table
    doc = pymupdf.open()
    page = doc.new_page(width=595, height=400)
    page.insert_font(fontname="dv", fontfile=FONT)
    page.insert_text((40, 40), "РОЗКЛАД ЗАНЯТЬ 2 курсу з 16.11.2026 по 20.11.2026", fontname="dv", fontsize=10)
    _table(page, [40, 70, 145, 580], [
        (["№", "Час", "СОБ-21"], set()),
        (["", "", "ауд. 101"], set()),
        (["ПОНЕДІЛОК"], {1, 2}),
        (["1", "08:00-09:20", cell_text], set()),
    ], top=60, h=28)
    return doc.tobytes()


def test_doubt_rules_distinguish_electives_from_real_conflicts():
    # 1) Паралельні предмети за вибором: через "/", різні предмети та різні викладачі, однакові дати → без сумніву
    ok = bot.parse.parse_schedule(_mini_schedule("Пр. Етика, Поволоцька М.О. з 16.11 / Пр. Право, Садовська Т.І. з 16.11"))
    assert ok["doubts"] == []
    assert len(ok["items"]) == 2 and all(i.get("choice") is True for i in ok["items"])

    # 2) Той самий викладач не може вести дві пари одночасно → справжній conflict
    same_t = bot.parse.parse_schedule(_mini_schedule("Лк. Спротив, Бородай С.О. / Пр. Спротив, Бородай С.О."))
    assert len(same_t["doubts"]) == 1 and same_t["doubts"][0]["kind"] == "conflict"

    # 3) Різні/невказані дати на двох частинах → справжній conflict
    mismatch = bot.parse.parse_schedule(_mini_schedule("Пр. Етика, Поволоцька М.О. / Пр. Право, Садовська Т.І. з 16.11"))
    assert len(mismatch["doubts"]) == 1 and mismatch["doubts"][0]["kind"] == "conflict"

    # 4) Два маркери "Лк./Пр." без "/" (склеєний текст) → merged сумнів
    merged = bot.parse.parse_schedule(_mini_schedule("Лк. Право, Іванов І.І. Пр. Історія, Петров П.П."))
    assert len(merged["doubts"]) == 1 and merged["doubts"][0]["kind"] == "merged"


def test_date_doubts_on_real_december_pdfs():
    dec3 = json.loads((GOLDEN / "rozklad-3-kurs-30.11-25.json").read_text(encoding="utf-8"))
    assert len(dec3["doubts"]) == 1
    d3 = dec3["doubts"][0]
    assert d3["kind"] == "date" and d3["reason"] == "out_of_period"
    assert d3["groups"] == ["ПК-36-А"] and d3["pair"] == 2
    assert d3["bad_date"] == "2026-11-10" and d3["suggested"] == "2026-12-10"
    assert "10.12" in d3["hint"]

    dec2 = json.loads((GOLDEN / "rozklad-2-kurs-30.11-25.json").read_text(encoding="utf-8"))
    reasons = {db["reason"]: db for db in dec2["doubts"]}
    assert set(reasons) == {"inverted_period", "weekday_mismatch"}
    assert reasons["inverted_period"]["suggested"] == "2026-12-25"
    wm = reasons["weekday_mismatch"]
    assert set(wm["groups"]) == {"СОБ-21", "СОІ-22"} and wm["bad_date"] == "2026-12-17" and wm["suggested"] == "2026-12-18"


def test_golden_consultations_pdfs_and_merge():
    r1 = consult.parse_consultations_pdf(
        (GOLDEN / "grafik-konsultaczij-czk-zo-2026-2027.pdf").read_bytes(),
        "https://nlu.edu.ua/wp-content/uploads/2026/11/grafik-konsultaczij-czk-zagalnoosvitnih-ta-soczialno-gumanitarnyh-dysczyplin-2026-2027-2.pdf",
        "ЦК загальноосвітніх та соціально-гуманітарних дисциплін",
    )
    r2 = consult.parse_consultations_pdf(
        (GOLDEN / "grafik-konsultaczij-czk-pravo-2026-2027.pdf").read_bytes(),
        "https://nlu.edu.ua/wp-content/uploads/2026/11/grafik-konsultaczij-czk-prava-ta-pravoohoronnoyi-diyalnosti-2026-2027.pdf",
        "ЦК права та правоохоронної діяльності",
    )
    assert len(r1) == 17 and len(r2) == 17
    merged = consult.merge_consultations(r1 + r2)
    expected = json.loads((GOLDEN / "consultations.json").read_text(encoding="utf-8"))
    assert merged == expected
    assert len(merged) == 31 and len({x["slug"] for x in merged}) == 30
    tue1420 = [x for x in merged if x["day_num"] == 2 and x["start"] <= "14:20" < x["end"]]
    assert len(tue1420) == 8
    trot = [x for x in merged if x["teacher"] == "Троцька В.В."]
    assert [(x["start"], x["end"]) for x in trot] == [("16:30", "17:00"), ("17:00", "18:00")]
    hned = next(x for x in merged if x["teacher"] == "Гнедик Ю.О.")
    assert "Стешенко Ю.О." in hned["aliases"]
    theory = [x["teacher"] for x in merged if any("теорія права" in s.lower() for s in x["subjects"])]
    assert set(theory) == {"Нога П.П.", "Кришталь Ю.В."}
    deyn = next(x for x in merged if x["teacher"] == "Дейнека В.С.")
    assert "Практикум зі складання процесуальних документів (ПД-24п)" in deyn["subjects"]
