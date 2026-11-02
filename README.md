# 📅 Розклад ПФК НЮУ — усі групи й курси

Неофіційний безкоштовний бот і сайт розкладу для **всіх груп** Полтавського фахового коледжу НЮУ імені Ярослава Мудрого.

- 🌐 **Сайт:** https://yaroslav28077.github.io/pfk-rozklad/ — вибір групи, «Сьогодні / Завтра / Тиждень / Заміни», PDF, підписка на календар (webcal/ics). Посилання на свою групу: `…/pfk-rozklad/?g=СОБ-11`.
- 🤖 **Telegram-бот:** кожен сам обирає курс і групу. Зранку бот надсилає пари на сьогодні, о 18:00 — на завтра, у неділю — огляд тижня. Нові заміни й новий розклад приходять одразу.
- 🔄 **Повністю автоматично:** GitHub Actions щогодини перевіряє сторінку [«Розклад занять»](http://plc.nlu.edu.ua/навчальна-робота/розклад-занять/), завантажує нові PDF і **сам їх розбирає**: розклад усіх курсів і «Зміни до розкладу». Нічого вручну переписувати не треба.

Усе працює на безкоштовних сервісах: GitHub Actions + GitHub Pages + Telegram + Cloudflare Workers.

---

## Разове налаштування (≈10 хвилин)

### 1. Увімкнути сайт
**Settings → Pages → Build and deployment → Source: «GitHub Actions».**

### 2. Створити Telegram-бота
1. У Telegram відкрий **@BotFather** → `/newbot` → назва (напр. «Розклад ПФК») → username (напр. `pfk_rozklad_bot`).
2. Скопіюй **токен** (виглядає як `1234567890:AA...`). Нікому його не показуй.
3. Напиши своєму боту будь-яке повідомлення, потім відкрий `https://api.telegram.org/bot<ТОКЕН>/getUpdates` і знайди `"chat":{"id":123456789` — це твій **chat id** (адміна).

### 3. Додати секрети в репозиторій
**Settings → Secrets and variables → Actions → New repository secret:**

| Name | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | токен від BotFather |
| `TELEGRAM_CHAT_ID` | chat id адміна — сюди приходять технічні помилки (кілька — через кому) |

### 4. Підключити воркер у Cloudflare (кнопки й команди бота)
1. https://dash.cloudflare.com → **Workers & Pages → Create → Import a repository** → обрати цей репозиторій (`main`). Назва воркера — `pfk-rozklad`. Тепер Cloudflare сам розгортає воркер після кожного пушу в `main`.
2. Воркер → **Settings → Variables and Secrets → Add** → тип **Secret**, ім'я `BOT_TOKEN`, значення — токен бота.
3. Якщо адреса воркера відрізняється від `https://pfk-rozklad.yaroslav28077.workers.dev`, впиши свою в `worker_url` у `config.json`.

Сховище підписників (KV) Cloudflare створює сам за `wrangler.json`, а вебхук бота на воркер ставить `bot.py` під час кожного запуску. Токен Cloudflare у GitHub не потрібен.

> Порада: воркер → **Settings → Build → Build watch paths** → `worker/*` і `wrangler.json`, щоб Cloudflare не перезбирав воркер після кожного автокоміту розкладу.

### 5. Запустити
**Actions → «Розклад ПФК» → Run workflow** (mode: `none`) — перша збірка сайту й підключення вебхука. Далі все працює щогодини само.

> Якщо вкладки Actions немає або workflow вимкнений — натисни «I understand my workflows, go ahead and enable them».
>
> Без воркера все теж працює, але спрощено: сайт оновлюється, а розсилка йде лише в `TELEGRAM_CHAT_ID` для групи `default_group` з `config.json`.

---

## Як приєднуються користувачі

Нічого вручну додавати не треба: кожен підписується сам.

- **Особисто:** відкрити бота → **«Почати»** → обрати **курс** → **групу**. Далі бот сам надсилає розклад цієї групи. Кнопки внизу: 📅 Сьогодні · ➡️ Завтра · 🗓 Тиждень · 🔄 Заміни · ⚙️ Налаштування.
- **Посилання одразу на групу:** `https://t.me/<бот>?start=<slug>`, напр. `https://t.me/pfk_rozklad_bot?start=sob-11` (slug видно в адресі календаря на сайті: `ics/sob-11.ics`). Можна просто написати боту назву групи, напр. `СОБ-11`.
- **Груповий чат:** додати бота в чат (або посилання `https://t.me/<бот>?startgroup=sob-11`) і надіслати `/group@<бот>` → обрати групу. Розклад приходитиме в чат.
- **Команди:** `/today` `/tomorrow` `/week` `/changes` `/group` (змінити групу) `/settings` (ранок / вечір / заміни) `/stop` (відписатися). Меню команд і опис бот виставляє сам, BotFather не потрібен.

Якщо користувач заблокував бота або видалив його з чату, підписка зникає автоматично.

---

## Як це влаштовано
```
config.json              коледж, сторінка розкладу, дзвінки, години розсилок, default_group, worker_url
scripts/bot.py           run: перевірка сайту → розбір PDF → збірка сайту → розсилка; show: розклад групи в консоль
scripts/parse.py         розбір PDF розкладу (усі групи, підгрупи, потоки, онлайн, дати) і «Змін до розкладу»
scripts/pdfgrid.py       пошук сітки таблиці в PDF
data/sources/*.json      розібрані PDF (комітить GitHub Actions)
docs/                    сайт (index.html, app.js, style.css, sw.js)
docs/data/, docs/ics/    дані груп і календарі — генеруються в Actions і публікуються на Pages
worker/worker.js         Cloudflare Worker: кнопки й команди бота, підписники (KV), API для Actions
wrangler.json            налаштування воркера для Cloudflare Workers Builds (KV, SITE_URL)
.github/workflows/rozklad.yml   щогодини: оновлення, сайт, розсилки, вебхук бота
```
Бот запускається щогодини (cron задано в UTC) і сам вирішує, що надсилати: о `morning_hour` — пари на сьогодні, о `evening_hour` — на завтра, нові заміни й розклад — одразу. Дублі відсікає воркер.

### Перевірити локально
```bash
pip install -r scripts/requirements.txt
SITE_URL=https://yaroslav28077.github.io/pfk-rozklad/ python3 scripts/bot.py run   # без токена — лише збірка сайту
python3 scripts/bot.py show СОБ-11 --date 2026-11-03
```

### Якщо PDF не розібрався
Бот напише адміну в `TELEGRAM_CHAT_ID`, а для цього курсу сайт показуватиме попередній розклад. Найчастіше причина — коледж змінив оформлення таблиці; тоді треба поправити `scripts/parse.py`.

## Інший коледж чи інша сторінка
Зроби **Fork**, зміни `config.json` (`college`, `short`, `source_page`, `bells`, `default_group`) і виконай кроки 1–5 (і зміни `worker_url`, `SITE_URL` у `wrangler.json`).

_Неофіційний проєкт студента. Джерело даних — офіційні PDF на plc.nlu.edu.ua; при розбіжностях пріоритет за оголошеннями коледжу._
