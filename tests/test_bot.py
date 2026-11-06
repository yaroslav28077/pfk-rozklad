"""Сценарні тести конвеєра: сайт коледжу → PDF → data/sources → сайт → розсилки → адмін.

Мережа, Telegram, воркер і сам розбір PDF підмінені фейками, тож тести перевіряють логіку бота,
а не стан зовнішніх сервісів. Запуск: pip install -r scripts/requirements.txt pytest && pytest -q
"""
import copy
import hashlib
import json
import sys
import urllib.error
import urllib.parse
from argparse import Namespace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import bot  # noqa: E402

WORKER = "https://worker.test"
REAL = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in (ROOT / "data" / "sources").glob("*.json")}
C1 = next(k for k, v in REAL.items() if v["data"]["course"] == 1)
C2 = next(k for k, v in REAL.items() if v["data"]["course"] == 2)


def shift(data, days, course=None):
    """Копія розкладу, зсунута на `days` днів (наприклад, «наступний тиждень»)."""
    d = copy.deepcopy(data)
    mv = lambda iso: (date.fromisoformat(iso) + timedelta(days=days)).isoformat()  # noqa: E731
    d["from"], d["to"] = mv(d["from"]), mv(d["to"])
    for it in d["items"]:
        it["date"] = mv(it["date"])
    if course:
        d["course"] = course
    return d


def kyiv(y, m, d, hh, mm=5):
    return datetime(y, m, d, hh, mm, tzinfo=bot.TZ)


class FakeWorker:
    def __init__(self, users=()):
        self.users = {str(u["id"]): dict(u) for u in users}
        self.kv, self.once_keys, self.calls = {}, set(), []
        self.mode = "ok"  # ok | legacy (немає /api/state) | down

    def handle(self, path, body):
        self.calls.append((path, body))
        if self.mode == "down":
            raise urllib.error.URLError(TimeoutError("timed out"))
        if path == "/api/users":
            return {"users": [{"id": k, **bot.DEFAULTS, **v} for k, v in self.users.items()]}
        if path == "/api/once":
            first = body["key"] not in self.once_keys
            self.once_keys.add(body["key"])
            return {"first": first}
        if path.startswith("/api/state") and self.mode == "legacy":
            raise urllib.error.HTTPError(WORKER + path, 404, "not found", {}, None)
        if path == "/api/state/get":
            return {"values": {k: self.kv.get(k) for k in body["keys"]}}
        if path == "/api/state/set":
            self.kv[body["key"]] = body["value"]
            return {"ok": True}
        if path == "/api/drop":
            for i in body["ids"]:
                self.users.pop(str(i), None)
            return {"ok": True}
        if path == "/api/move":
            u = self.users.pop(str(body["from"]), None)
            if u:
                self.users[str(body["to"])] = u
            return {"ok": True}
        if path == "/api/heartbeat":
            self.kv["hb"] = body
            return {"ok": True}
        raise urllib.error.HTTPError(WORKER + path, 404, "not found", {}, None)


class World:
    """Фейковий світ: сайт коледжу, PDF, розбір, Telegram, воркер, годинник."""

    def __init__(self, tmp, monkeypatch):
        self.tmp = tmp
        self.page_ok = True
        self.pdfs = {}          # key → bytes (що лежить на сайті коледжу)
        self.titles = {}
        self.meaning = {}       # bytes → розібрані дані або Exception
        self.parse_calls = []
        self.pdf_errors = {}    # key → Exception при завантаженні
        self.sent = []          # (chat_id, text, disable_notification)
        self.tg_fail = {}       # chat_id → [відповіді, які повернути замість ok]
        self.worker = FakeWorker()
        self.clock = kyiv(2026, 11, 3, 8)
        self.page_html = None
        mp = monkeypatch
        mp.setattr(bot, "DATA", tmp / "data")
        mp.setattr(bot, "SRC", tmp / "data" / "sources")
        mp.setattr(bot, "DOCS", tmp / "docs")
        mp.setattr(bot, "TOKEN", "123:TEST")
        mp.setattr(bot, "ADMINS", ["900"])
        mp.setattr(bot, "WORKER_URL", WORKER)
        mp.setattr(bot, "SITE", "https://site.test/")
        mp.setattr(bot, "SLEEP", lambda s: None)
        mp.setattr(bot, "now", lambda: self.clock)
        mp.setattr(bot, "http", self.http)
        mp.setattr(bot, "tg", self.tg)
        mp.setattr(bot.parse, "parse_schedule", self.parse)
        mp.setattr(bot.parse, "parse_changes", self.parse)
        mp.setattr(bot, "deployed_hash", lambda: None)
        (tmp / "data" / "sources").mkdir(parents=True)

    # --- сайт коледжу ---
    def url_of(self, key):
        return "http://plc.nlu.edu.ua/wp-content/uploads/2016/09/" + urllib.parse.quote(key) + ".pdf"

    def publish(self, key, data, raw=None, title=None):
        raw = raw or f"PDF:{key}:{json.dumps(data, sort_keys=True)}".encode()
        self.pdfs[key], self.titles[key] = raw, title or key.replace("-", " ")
        self.meaning[raw] = data
        return raw

    def seed_real(self):
        """data/sources як зараз у репозиторії + ті самі PDF на сайті коледжу (sha збігається)."""
        for k, rec in REAL.items():
            raw = self.publish(k, rec["data"], title=rec["title"])
            rec = {**copy.deepcopy(rec), "sha256": hashlib.sha256(raw).hexdigest(), "url": self.url_of(k)}
            bot.jsave(bot.SRC / f"{k}.json", rec)

    def http(self, url, data=None, headers=None, timeout=60, tries=None):
        if url.startswith(WORKER):
            body = json.loads(data) if data else None
            return json.dumps(self.worker.handle(url[len(WORKER):], body)).encode()
        if "uploads" in url:
            key = Path(urllib.parse.unquote(urllib.parse.urlsplit(url).path)).stem
            if key in self.pdf_errors:
                raise self.pdf_errors[key]
            return self.pdfs[key]
        if not self.page_ok:
            raise urllib.error.URLError(TimeoutError("timed out"))
        links = "".join(f'<a href="{self.url_of(k)}">{self.titles[k]}</a>' for k in self.pdfs)
        return (self.page_html or f"<html><body>{links}</body></html>").encode()

    def parse(self, raw, title=""):
        self.parse_calls.append(raw)
        v = self.meaning.get(raw, ValueError("невідомий PDF"))
        if isinstance(v, Exception):
            raise v
        return copy.deepcopy(v)

    # --- Telegram ---
    def tg(self, method, payload):
        if method != "sendMessage":
            return {"ok": True, "result": {"username": "pfkrozkladBot"}} if method == "getMe" else {"ok": True}
        cid = str(payload["chat_id"])
        fails = self.tg_fail.get(cid)
        if fails:
            return fails.pop(0)
        self.sent.append((cid, payload["text"], payload.get("disable_notification")))
        return {"ok": True}

    def to(self, cid):
        return [t for c, t, _ in self.sent if c == str(cid)]

    # --- запуск як у GitHub Actions ---
    def run(self, mode="auto", only=""):
        a = Namespace(mode=mode, only=only, strict=False, state=str(self.tmp / "state.json"))
        st = bot.cmd_update(a)
        problems = bot.cmd_notify(a)
        return st, problems


@pytest.fixture
def w(tmp_path, monkeypatch):
    return World(tmp_path, monkeypatch)


def user(cid, g, **kw):
    return {"id": str(cid), "g": g, **kw}


def src(key):
    return json.loads((bot.SRC / f"{key}.json").read_text(encoding="utf-8"))


# ======================================================================== сайт недоступний → відновився
def test_site_down_keeps_last_schedule_alerts_once_and_reports_recovery(w):
    w.seed_real()
    before = {p.name: p.read_bytes() for p in bot.SRC.glob("*.json")}
    w.worker.users = {"1": user(1, "СОБ-11", mh=6)}
    w.page_ok = False
    w.clock = kyiv(2026, 11, 4, 2, 1)  # як у реальному логі: 04.11 02:01
    st, problems = w.run()
    assert [p["id"] for p in problems] == ["site"]
    assert {p.name: p.read_bytes() for p in bot.SRC.glob("*.json")} == before  # дані не зачеплено
    idx = json.loads((bot.DOCS / "data" / "index.json").read_text(encoding="utf-8"))
    assert sum(len(v) for v in idx["courses"].values()) == 21  # сайт зібрано з останніх робочих даних
    assert idx["source_down_since"].startswith("2026-11-04T02:01")
    admin = w.to(900)
    assert len(admin) == 1 and "Сайт коледжу недоступний" in admin[0] and "тайм-аут" in admin[0]

    # наступні запуски протягом доби — без повторних повідомлень, ранкова розсилка йде з поміткою
    w.clock = kyiv(2026, 11, 4, 9, 30)
    st, problems = w.run()
    assert len(w.to(900)) == 1
    assert any("Сайт коледжу зараз недоступний" in t for t in w.to(1))
    w.clock = kyiv(2026, 11, 4, 20)
    w.run()
    assert len(w.to(900)) == 1

    # через добу — одне нагадування
    w.clock = kyiv(2026, 11, 5, 3)
    w.run()
    assert len(w.to(900)) == 2 and "Досі не вирішено" in w.to(900)[1]

    # сайт відновився (PDF ті самі) — повідомлення про відновлення, без розсилки «новий розклад»
    w.page_ok = True
    w.clock = kyiv(2026, 11, 5, 14)
    sent_before = len(w.sent)
    st, problems = w.run()
    assert problems == []
    assert "знову доступний" in w.to(900)[-1]
    assert len(w.sent) == sent_before + 1  # лише адміну
    assert json.loads((bot.DATA / "health.json").read_text())["issues"] == {}
    idx = json.loads((bot.DOCS / "data" / "index.json").read_text(encoding="utf-8"))
    assert idx["source_down_since"] is None
    assert w.parse_calls == []  # sha не змінились — повторно нічого не розбирали


def test_http_retries_are_bounded_and_only_for_transient_errors(monkeypatch):
    calls = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        if len(calls) <= behaviour["fail"]:
            raise behaviour["exc"]

        class R:
            def __enter__(s): return s
            def __exit__(s, *a): return False
            def read(s): return b"ok"
        return R()

    monkeypatch.setattr(bot.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(bot, "SLEEP", lambda s: None)
    behaviour = {"fail": 2, "exc": urllib.error.URLError(TimeoutError("timed out"))}
    assert bot.http("http://x/", tries=(3, 1, (1,))) == b"ok" and len(calls) == 3
    calls.clear(); behaviour.update(fail=99)
    with pytest.raises(urllib.error.URLError):
        bot.http("http://x/", tries=(3, 1, (1,)))
    assert len(calls) == 3  # не більше 3 спроб
    calls.clear(); behaviour.update(fail=99, exc=urllib.error.HTTPError("http://x/", 404, "nf", {}, None))
    with pytest.raises(urllib.error.HTTPError):
        bot.http("http://x/", tries=(3, 1, (1,)))
    assert len(calls) == 1  # 404 не повторюємо
    calls.clear(); behaviour.update(fail=1, exc=urllib.error.HTTPError("http://x/", 503, "busy", {}, None))
    assert bot.http("http://x/", tries=(3, 1, (1,))) == b"ok" and len(calls) == 2


# ======================================================================== новий PDF / змінений PDF
def test_new_pdf_is_published_to_site_and_sent_only_to_its_course(w):
    w.seed_real()
    w.worker.users = {"1": user(1, "СОБ-11"), "2": user(2, "СОБ-21"), "3": user(3, "ПД-11", c=0), "4": user(4, "ПД-12", s=1)}
    w.clock = kyiv(2026, 11, 6, 15)
    nxt = shift(REAL[C1]["data"], 7)
    w.publish("РОЗКЛАД-1-курс-09.11-13.11", nxt, title="РОЗКЛАД 1 курс 09.11-13.11")
    st, problems = w.run()
    assert problems == []
    assert [e["type"] for e in st["events"]] == ["schedule"] and st["deploy"] is True
    g = json.loads((bot.DOCS / "data" / "g" / "sob-11.json").read_text(encoding="utf-8"))
    assert "2026-11-09" in g["days"]  # нові дані потрапили в дані сайту (і, отже, у кнопки бота)
    assert any("Новий розклад" in t for t in w.to(1))
    assert w.to(2) == [] and w.to(3) == []  # інший курс / вимкнені заміни
    assert [s for c, _, s in w.sent if c == "4"] == [True]  # беззвучний режим

    n = len(w.sent)
    w.clock += timedelta(hours=1)
    st, _ = w.run()  # повторний запуск: файл уже відомий
    assert st["events"] == [] and len(w.sent) == n


def test_stale_checkout_or_rerun_does_not_duplicate_notifications(w):
    """Перезапуск старого run бачить data/ з минулого — подія виявиться вдруге, але дубля не буде."""
    w.seed_real()
    snapshot = {p.name: p.read_bytes() for p in bot.SRC.glob("*.json")}
    w.worker.users = {"1": user(1, "СОБ-11")}
    w.clock = kyiv(2026, 11, 6, 15)
    w.publish("РОЗКЛАД-1-курс-09.11-13.11", shift(REAL[C1]["data"], 7))
    w.run()
    assert len(w.to(1)) == 1
    for p in bot.SRC.glob("*.json"):  # «checkout старого коміту»
        p.unlink()
    for name, b in snapshot.items():
        (bot.SRC / name).write_bytes(b)
    st, _ = w.run()
    assert len(st["events"]) == 1 and len(w.to(1)) == 1


def test_changed_pdf_with_same_name(w):
    w.seed_real()
    w.worker.users = {"1": user(1, "СОБ-11")}
    w.clock = kyiv(2026, 11, 3, 15)
    # той самий розклад, але файл перезалили (інші байти) — без повідомлення людям
    w.publish(C1, REAL[C1]["data"], raw=b"same-schedule-new-bytes")
    st, problems = w.run()
    assert st["events"] == [] and w.to(1) == [] and problems == []
    assert src(C1)["sha256"] == hashlib.sha256(b"same-schedule-new-bytes").hexdigest()
    # реально змінений розклад у файлі з тією ж назвою — «Оновлено розклад»
    changed = copy.deepcopy(REAL[C1]["data"])
    for it in changed["items"]:
        if it["group"] == "СОБ-11" and it["date"] == "2026-11-04":
            it["subject"] = "Нова дисципліна"
    w.publish(C1, changed, raw=b"really-changed")
    st, _ = w.run()
    assert [e.get("changed") for e in st["events"]] == [True]
    assert len(w.to(1)) == 1 and "Оновлено розклад" in w.to(1)[0] and "Нова дисципліна" in w.to(1)[0]


# ======================================================================== помилка розбору → повтор
def test_parse_failure_keeps_old_data_and_is_retried_until_success(w):
    w.seed_real()
    w.worker.users = {"1": user(1, "СОБ-11")}
    w.clock = kyiv(2026, 11, 3, 15)
    w.pdfs[C1] = b"BROKEN"
    w.meaning[b"BROKEN"] = ValueError("не знайдено заголовок із назвами груп")
    st, problems = w.run()
    rec = src(C1)
    assert rec["data"] == REAL[C1]["data"]  # робоча версія збережена
    assert rec["failed"]["sha256"] == hashlib.sha256(b"BROKEN").hexdigest() and rec["failed"]["attempts"] == 1
    assert [p["id"] for p in problems] == [f"parse:{C1}"]
    g = json.loads((bot.DOCS / "data" / "g" / "sob-11.json").read_text(encoding="utf-8"))
    assert "2026-11-03" in g["days"]  # сайт і далі показує 1 курс
    assert len(w.to(900)) == 1 and "Не вдалося розібрати PDF" in w.to(900)[0]

    # той самий файл (не змінився) — розбір повторюється, адміну не пишемо вдруге
    w.clock += timedelta(hours=1)
    w.parse_calls.clear()
    st, problems = w.run()
    assert w.parse_calls == [b"BROKEN"] and src(C1)["failed"]["attempts"] == 2
    assert len(w.to(900)) == 1 and w.to(1) == []

    # парсер виправили (або файл став читатися) — той самий файл тепер розбирається
    fixed = copy.deepcopy(REAL[C1]["data"])
    fixed["items"][0]["subject"] = "Виправлено"
    w.meaning[b"BROKEN"] = fixed
    w.clock += timedelta(hours=1)
    st, problems = w.run()
    assert problems == [] and "failed" not in src(C1) and src(C1)["data"] == fixed
    assert "PDF розібрано" in w.to(900)[-1]
    assert [e["type"] for e in st["events"]] == ["schedule"]


def test_new_file_parse_failure_and_legacy_error_record_are_retried(w):
    w.seed_real()
    w.clock = kyiv(2026, 11, 6, 15)
    w.publish("РОЗКЛАД-2-курс-09.11-13.11", None, raw=b"NEW-BROKEN")
    w.meaning[b"NEW-BROKEN"] = ValueError("не знайдено жодної пари")
    w.run()
    rec = src("РОЗКЛАД-2-курс-09.11-13.11")
    assert "data" not in rec and rec["failed"]["attempts"] == 1
    # старий формат запису з полем "error" (так зберігав попередній код) — теж розбирається повторно
    legacy = {k: v for k, v in rec.items() if k != "failed"}
    legacy.update(sha256=hashlib.sha256(b"NEW-BROKEN").hexdigest(), error="ValueError: старе")
    bot.jsave(bot.SRC / "РОЗКЛАД-2-курс-09.11-13.11.json", legacy)
    w.meaning[b"NEW-BROKEN"] = shift(REAL[C2]["data"], 7)
    w.parse_calls.clear()
    st, problems = w.run()
    assert w.parse_calls == [b"NEW-BROKEN"] and "data" in src("РОЗКЛАД-2-курс-09.11-13.11")
    assert problems == [] and [e["type"] for e in st["events"]] == ["schedule"]


def test_suspicious_parse_result_does_not_replace_good_data(w):
    w.seed_real()
    w.clock = kyiv(2026, 11, 3, 15)
    broken = copy.deepcopy(REAL[C1]["data"])
    broken["items"] = broken["items"][:5]  # 5 пар замість 160 — схоже на зламаний розбір
    w.publish(C1, broken, raw=b"tiny")
    st, problems = w.run()
    assert src(C1)["data"] == REAL[C1]["data"] and [p["id"] for p in problems] == [f"parse:{C1}"]


def test_pdf_download_error_is_reported_not_hidden(w):
    w.seed_real()
    w.clock = kyiv(2026, 11, 3, 15)
    w.pdf_errors[C2] = urllib.error.HTTPError("x", 503, "busy", {}, None)
    st, problems = w.run()
    assert [p["id"] for p in problems] == [f"pdf:{C2}"] and src(C2)["data"] == REAL[C2]["data"]
    out = Path(bot.os.environ.get("GITHUB_OUTPUT", "/nonexistent"))
    assert not out.exists() or "degraded=true" in out.read_text()


def test_empty_build_does_not_wipe_site(w):
    w.clock = kyiv(2026, 11, 3, 15)
    w.publish("РОЗКЛАД-1-курс-x", None, raw=b"B")
    w.meaning[b"B"] = ValueError("зламано")
    (bot.DOCS / "data").mkdir(parents=True)
    (bot.DOCS / "data" / "index.json").write_text('{"hash": "old"}')
    st, problems = w.run()
    assert "empty" in [p["id"] for p in problems] and st["deploy"] is False
    assert json.loads((bot.DOCS / "data" / "index.json").read_text())["hash"] == "old"


# ======================================================================== розсилки за розкладом
def test_morning_is_sent_even_if_github_starts_the_run_hours_late(w):
    w.seed_real()
    w.worker.users = {"a": user("a", "СОБ-11", mh=6), "b": user("b", "СОБ-11", mh=7, m=0),
                      "c": user("c", "ПД-21", mh=9), "d": user("d", "ПК-31-А", mh=8)}
    w.clock = kyiv(2026, 11, 3, 11, 2)  # реальний запуск 03.11 13:02 був би вже поза старим вікном (h, h-1)
    w.run()
    assert len(w.to("a")) == 1 and "Сьогодні" in w.to("a")[0]
    assert w.to("b") == []  # ранок вимкнено
    assert len(w.to("c")) == 1 and len(w.to("d")) == 1
    w.clock = kyiv(2026, 11, 3, 12, 30)
    w.run()  # повторний запуск — без дублів
    assert len(w.to("a")) == 1 and len(w.to("c")) == 1
    w.clock = kyiv(2026, 11, 4, 13, 2)  # наступний день, але запуск лише о 13:02 — ранкова вже недоречна
    w.run()
    assert len(w.to("a")) == 1


def test_morning_not_sent_before_users_hour(w):
    w.seed_real()
    w.worker.users = {"a": user("a", "СОБ-11", mh=9)}
    w.clock = kyiv(2026, 11, 3, 7)
    w.run()
    assert w.to("a") == []
    w.clock = kyiv(2026, 11, 3, 9, 20)
    w.run()
    assert len(w.to("a")) == 1


def test_evening_and_sunday_week(w):
    w.seed_real()
    w.worker.users = {"e": user("e", "СОБ-11", eh=18), "x": user("x", "СОБ-11", eh=21, e=0, w=0)}
    w.clock = kyiv(2026, 11, 3, 22, 40)  # вівторок, запуск запізнився до 22:40
    w.run()
    assert len(w.to("e")) == 1 and "Завтра" in w.to("e")[0] and w.to("x") == []
    w.clock = kyiv(2026, 11, 3, 23, 10)
    w.run()
    assert len(w.to("e")) == 1
    # неділя: огляд наступного тижня — лише якщо він уже є; інакше не шлемо таблицю «не опубліковано»
    w.clock = kyiv(2026, 11, 8, 18, 30)
    w.run()
    assert len(w.to("e")) == 1
    w.publish("РОЗКЛАД-1-курс-09.11-13.11", shift(REAL[C1]["data"], 7))
    w.clock = kyiv(2026, 11, 8, 19, 30)
    w.run()
    assert any("Тиждень 09.11–13.11" in t for t in w.to("e"))


def test_failed_send_is_not_marked_done_and_is_retried_only_for_failed_chats(w):
    w.seed_real()
    w.worker.users = {"a": user("a", "СОБ-11"), "b": user("b", "СОБ-11")}
    w.tg_fail["b"] = [{"ok": False, "error_code": 502, "description": "HTTP 502"}]
    w.clock = kyiv(2026, 11, 3, 7)
    w.run()
    assert len(w.to("a")) == 1 and w.to("b") == []
    assert w.worker.kv["morning:2026-11-03:6"]["failed"] == ["b"]
    w.clock = kyiv(2026, 11, 3, 8)
    w.run()
    assert len(w.to("a")) == 1 and len(w.to("b")) == 1
    w.clock = kyiv(2026, 11, 3, 9)
    w.run()
    assert len(w.to("a")) == 1 and len(w.to("b")) == 1


def test_worker_down_postpones_broadcasts_and_alerts_admin(w):
    w.seed_real()
    w.worker.users = {"a": user("a", "СОБ-11")}
    w.worker.mode = "down"
    w.clock = kyiv(2026, 11, 3, 7)
    st, problems = w.run()
    assert w.to("a") == [] and "worker" in [p["id"] for p in problems]
    assert "Cloudflare Worker" in w.to(900)[0]
    w.worker.mode = "ok"
    w.clock = kyiv(2026, 11, 3, 8)
    st, problems = w.run()
    assert len(w.to("a")) == 1 and problems == [] and "Воркер знову відповідає" in w.to(900)[-1]


def test_old_worker_without_state_api_still_dedupes(w):
    w.seed_real()
    w.worker.users = {"a": user("a", "СОБ-11")}
    w.worker.mode = "legacy"
    w.clock = kyiv(2026, 11, 3, 7)
    w.run()
    w.clock = kyiv(2026, 11, 3, 8)
    w.run()
    assert len(w.to("a")) == 1


def test_forced_test_mode_goes_only_to_test_chat(w):
    w.seed_real()
    w.worker.users = {"a": user("a", "СОБ-11"), "b": user("b", "ПД-21")}
    w.clock = kyiv(2026, 11, 3, 15)
    w.run(mode="morning", only="777")
    assert w.to("a") == [] and w.to("b") == [] and len(w.to("777")) == 1


@pytest.mark.parametrize("utc,due", [
    (datetime(2026, 10, 25, 4, 30, tzinfo=timezone.utc), {6}),     # після переходу на зимовий: 06:30 EET
    (datetime(2027, 3, 28, 4, 30, tzinfo=timezone.utc), {6, 7}),   # після переходу на літній: 07:30 EEST
    (datetime(2027, 3, 28, 3, 30, tzinfo=timezone.utc), {6}),      # 06:30 EEST
])
def test_kyiv_time_with_dst(w, utc, due):
    d = utc.astimezone(bot.TZ).date().isoformat()
    out = {"G-11": {"group": "G-11", "course": 1, "slug": "g-11", "room": "", "changes": [],
                    "days": {d: {"items": [{"pair": 1, "start": "08:00", "end": "09:20", "subject": "Предмет"}]}}}}
    w.clock = utc.astimezone(bot.TZ)
    w.worker.users = {str(h): user(h, "G-11", mh=h) for h in (6, 7, 8, 9)}
    bot.notify(out, [], "auto")
    assert {int(c) for c, _, _ in w.sent} == due


# ======================================================================== заміни: правильні групи й підгрупи
def test_changes_go_to_exact_group_not_prefix_sibling_and_subgroups_filter(w):
    day = {"items": [{"pair": 1, "start": "08:00", "end": "09:20", "subject": "Спільна"},
                     {"pair": 2, "start": "09:30", "end": "10:50", "subject": "Тільки 1", "subgroup": 1},
                     {"pair": 2, "start": "09:30", "end": "10:50", "subject": "Тільки 2", "subgroup": 2}]}
    mk = lambda n: {"group": n, "course": 1, "slug": bot.slug(n), "room": "", "changes": [], "days": {"2026-11-03": day}}  # noqa: E731
    out = {"СОБ-13": mk("СОБ-13"), "СОБ-13п": mk("СОБ-13п")}
    w.worker.users = {"p": user("p", "СОБ-13"), "q": user("q", "СОБ-13п"), "r": user("r", "СОБ-13", sg=2, mh=6)}
    w.clock = kyiv(2026, 11, 3, 6, 30)
    ev = {"type": "changes", "key": "changes:x:1", "title": "Зміни", "url": "u",
          "rows": [{"date": "2026-11-04", "pair": 3, "group": "СОБ-13", "subject": "Історія", "teacher": ""}]}
    bot.notify(out, [ev], "auto")
    assert any("Нові заміни" in t for t in w.to("p")) and any("Нові заміни" in t for t in w.to("r"))
    assert not any("Нові заміни" in t for t in w.to("q"))  # раніше сюди теж приходило (збіг за префіксом)
    morning_r = next(t for t in w.to("r") if "Сьогодні" in t)
    assert "Тільки 2" in morning_r and "Тільки 1" not in morning_r and "Спільна" in morning_r
    n = len(w.sent)
    bot.notify(out, [ev], "auto")  # та сама подія вдруге — без дублів
    assert len(w.sent) == n


def test_blocked_users_dropped_and_migrated_group_moved(w):
    out = {"G-11": {"group": "G-11", "course": 1, "slug": "g-11", "room": "", "changes": [],
                    "days": {"2026-11-03": {"items": [{"pair": 1, "start": "08:00", "end": "09:20", "subject": "П"}]}}}}
    w.worker.users = {"-1": user(-1, "G-11", sg=1, mh=7, s=1), "5": user(5, "G-11")}
    w.tg_fail["-1"] = [{"ok": False, "error_code": 400, "description": "Bad Request: group chat was upgraded to a supergroup chat",
                        "parameters": {"migrate_to_chat_id": -1001}}]
    w.tg_fail["5"] = [{"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}]
    w.clock = kyiv(2026, 11, 3, 8)
    bot.notify(out, [], "auto")
    assert len(w.to(-1001)) == 1
    assert "5" not in w.worker.users
    assert w.worker.users["-1001"] == {"id": "-1", "g": "G-11", "sg": 1, "mh": 7, "s": 1}  # налаштування збережено


def test_admin_alert_retried_if_telegram_failed(w):
    w.seed_real()
    w.page_ok = False
    w.tg_fail["900"] = [{"ok": False, "error_code": 502, "description": "HTTP 502"}]
    w.clock = kyiv(2026, 11, 4, 10)
    w.run()
    assert w.to(900) == []
    w.clock = kyiv(2026, 11, 4, 11)
    w.run()
    assert len(w.to(900)) == 1


def test_heartbeat_sent_each_run(w):
    w.seed_real()
    w.clock = kyiv(2026, 11, 3, 15)
    w.run()
    assert w.worker.kv["hb"]["admins"] == ["900"] and w.worker.kv["hb"]["degraded"] is False
