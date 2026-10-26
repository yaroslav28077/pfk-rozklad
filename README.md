# 📅 Розклад СОБ-11 · ПФК НЮУ

Неофіційний безкоштовний бот і сайт розкладу для групи **СОБ-11** Полтавського фахового коледжу НЮУ імені Ярослава Мудрого.

- 🌐 **Сайт:** https://yaroslav28077.github.io/pfk-rozklad/ — «Сьогодні / Завтра / Тиждень / Заміни / PDF», кнопка «У календар».
- 🤖 **Telegram:** щоранку пари на сьогодні, о 18:00 — на завтра, удень — повідомлення про нові заміни чи новий розклад.
- 🔄 **Автоматично:** GitHub Actions кілька разів на день перевіряє сторінку [«Розклад занять»](http://plc.nlu.edu.ua/навчальна-робота/розклад-занять/) на plc.nlu.edu.ua, завантажує PDF, розбирає «Зміни до розкладу» і накладає їх на розклад групи.

Усе працює на безкоштовних сервісах: GitHub Actions + GitHub Pages + Telegram (+ за бажанням Cloudflare Workers для команд).

---

## Налаштування (≈10 хвилин, один раз)

### 1. Увімкнути сайт
**Settings → Pages → Build and deployment → Source: «GitHub Actions».**

### 2. Створити Telegram-бота
1. У Telegram відкрий **@BotFather** → `/newbot` → назва (напр. «Розклад СОБ-11») → username (напр. `sob11_rozklad_bot`).
2. Скопіюй **токен** (виглядає як `1234567890:AA...`). Нікому його не показуй.
3. Напиши своєму боту будь-яке повідомлення (напр. «привіт»).
4. Відкрий у браузері `https://api.telegram.org/bot<ТОКЕН>/getUpdates` і знайди `"chat":{"id":123456789` — це твій **chat id**.
   - Хочеш, щоб бот писав у чат групи? Додай бота в груповий чат, напиши там повідомлення і знову відкрий `getUpdates` — id групи від'ємний (напр. `-1001234567890`). Кілька чатів — через кому.

### 3. Додати секрети в репозиторій
**Settings → Secrets and variables → Actions → New repository secret:**

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен від BotFather |
| `TELEGRAM_CHAT_ID` | chat id (або кілька через кому) |

### 4. Перевірити
**Actions → «Розклад ПФК» → Run workflow** (mode: `morning`) → за ~1 хв має прийти повідомлення з парами на сьогодні, а сайт — відкритися.

> Якщо вкладки Actions немає або workflow вимкнений — натисни «I understand my workflows, go ahead and enable them».

---

## Команди в Telegram (за бажанням): /today /tomorrow /week /changes

Розсилки працюють і без цього. Щоб бот **відповідав на команди миттєво**, потрібен безкоштовний Cloudflare Worker:

1. Зареєструйся на https://dash.cloudflare.com → **Workers & Pages → Create → Worker** → «Hello World» → **Deploy**.
2. **Edit code** → встав вміст `worker/worker.js` → **Deploy**.
3. Worker → **Settings → Variables and Secrets**:
   - `BOT_TOKEN` (Secret) — токен бота;
   - `SITE_URL` (Text) — `https://yaroslav28077.github.io/pfk-rozklad/`;
   - `WEBHOOK_SECRET` (Secret) — будь-який довгий рядок, напр. `sob11-xyz-2026`.
4. Відкрий у браузері (підстав свої значення):
   `https://api.telegram.org/bot<ТОКЕН>/setWebhook?url=https://<назва-воркера>.<акаунт>.workers.dev&secret_token=<WEBHOOK_SECRET>`
   → має бути `"ok":true`.
5. Напиши боту `/start` — він покаже меню і ID чату.
6. (Необов'язково) у @BotFather → `/setcommands` → вибери бота → встав:
   ```
   today - Пари сьогодні
   tomorrow - Пари завтра
   week - Розклад на тиждень
   changes - Заміни
   ```

---

## Коли виходить новий розклад (≈раз на місяць)

Бот сам помітить новий PDF і напише «🆕 На сайті новий розклад». Основна сітка в PDF складна (об'єднані клітинки, вертикальний текст), тому її **звіряє людина**:

- **через агента «Мудрий» в Antigravity:** «Онови розклад СОБ-11 з сайту коледжу і онови data/rozklad.json у репозиторії pfk-rozklad»;
- **або вручну:** відредагуй `data/rozklad.json` прямо на GitHub (олівець ✏️) — формат нижче. Після збереження сайт оновиться сам.

Заміни (PDF «Зміни до розкладу») розбираються **автоматично**.

### Формат `data/rozklad.json`
```json
{
  "from": "2026-10-05", "to": "2026-10-30",
  "items": [
    {"day": 2, "pair": 1, "subject": "Математика", "type": "Пр", "teacher": "Харченко А.В.", "room": "202"},
    {"day": 1, "pair": 3, "subject": "Теорія права", "type": "Лк", "teacher": "Кришталь Ю.В.", "to": "2026-10-19"},
    {"day": 1, "pair": 1, "subject": "Теорія права", "type": "Лк", "dates": ["2026-10-26"]},
    {"day": 3, "pair": 2, "subject": "…", "weeks": "odd"}
  ]
}
```
`day`: 1 = Пн … 5 = Пт · `pair`: номер пари · необов'язкові: `from`/`to` (з/по дату), `dates` (лише ці дати), `weeks`: `odd`/`even` (чисельник/знаменник), `start`/`end` (інший час).

---

## Для інших груп
Зроби **Fork**, зміни `config.json` (`group`, `course`, `site_url`) і `data/rozklad.json`, виконай кроки 1–4.

## Як це влаштовано
```
config.json              група, курс, адреса сайту
data/rozklad.json        основний розклад (звірений людиною)
data/zminy.json          заміни (автоматично з PDF)
data/state.json          що вже бачили на сайті коледжу (автоматично)
scripts/bot.py           перевірка сайту, розбір PDF, Telegram, генерація сайту
docs/                    сайт (index.html, app.js, style.css) + site.json, rozklad.ics
worker/worker.js         Cloudflare Worker для команд бота
.github/workflows/       розклад запусків GitHub Actions
```
Час запусків задано в UTC (`rozklad.yml`): ранкова розсилка ≈06:15 взимку / ≈07:15 влітку за Києвом.

_Неофіційний проєкт студента. Джерело даних — офіційні PDF на plc.nlu.edu.ua; при розбіжностях пріоритет за оголошеннями коледжу._
