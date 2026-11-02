# NEXT_STEPS — передача работы следующей модели (ветка `multi-group`)

Цель пользователя (Ярослав): бот и сайт для ВСЕХ групп и курсов ПФК НЮУ с выбором группы, полностью автономно, лёгкое добавление пользователей, без лишнего.

## Уже сделано в этой ветке
- `scripts/pdfgrid.py` — сетка таблиц PDF по пикселям (текст убирается redaction'ом, растр 4x, порог `<190`, min_len 4pt, объединённые ячейки через union-find). `find_tables` на этих PDF не работает: линии — обрезанные заливки.
- `scripts/parse.py` — `parse_schedule` (группы, аудитории, дни, время пар, онлайн, потоки, подгруппы, пометки дат «до/з/список»), `parse_changes` («Зміни до розкладу», оба формата), `change_targets` («1 курс (9)», «ПК-31»→«ПК-31-А»). Проверено: 0 расхождений с ручным октябрьским расписанием СОБ-11; 21 группа на 3 курсах.
- `scripts/bot.py` — `run --mode auto|morning|evening|weekly|none` (update → build → write_site → notify) и `show <група> [--date]`.
  - Источники: `data/sources/*.json` (коммитятся). Сайт: `docs/data/index.json`, `docs/data/g/<slug>.json`, `docs/ics/<slug>.ics` (в .gitignore, публикуются на Pages).
  - Пишет `deploy=true|false` в `GITHUB_OUTPUT`.
  - Подписчики берутся из Worker; его URL — через `getWebhookInfo`. Секрет = `sha256("pfk-rozklad:"+token)[:48]`, защита от дублей — `/api/once`.
  - Без Worker — fallback: рассылка в `TELEGRAM_CHAT_ID` для `default_group`. Старые чаты переносятся в подписчики сами.
  - Ошибки разбора уходят админу (`TELEGRAM_CHAT_ID`). Первый запуск при пустом `data/sources` ничего не рассылает.
- `worker/worker.js` — выбор курс → группа кнопками, `/start <slug>`, ввод названия группы, `/group`, `/settings` (утро/вечер/замены), `/stop`, `my_chat_member`.
  - KV `USERS`: ключ `u:<chat>`, metadata `{g,m,e,c}`.
  - API с Bearer-секретом: `GET /api/users`, `POST /api/once|/api/add|/api/drop`.
- `docs/*` — выбор группы (`?g=`, localStorage), данные по группам, вкладка «Ще»: смена группы, бот, webcal/ics.
- `config.json`, `wrangler.json` (KV id вписывает CI), `scripts/requirements.txt`, `.gitignore`.
- Удалено: `data/rozklad.json`, `data/zminy.json`, `data/state.json`, `docs/site.json`, `docs/rozklad.ics`, `docs/novyi_rozklad.png`, `wrangler.jsonc`.

## Осталось сделать
> Статус: пункты 1–4 выполнены (workflows, README, проверки: 21 группа, `show СОБ-11 --date 2026-11-03` ок, actionlint ок, `wrangler deploy --dry-run` ок). Остались 5 (PR) и 6.

1. **`.github/workflows/rozklad.yml`** (сейчас старый — ПЕРЕПИСАТЬ, со старым bot.py он не совместим):
   - триггеры: cron `"20 3-19 * * *"`; `workflow_dispatch` с input `mode` (auto/morning/evening/weekly/none); push в main по paths `scripts/**`, `docs/**`, `config.json`, сам workflow.
   - permissions: `contents`, `pages`, `id-token`, `actions` — write. Concurrency без отмены.
   - job `update`:
     - python 3.12, `pip install -r scripts/requirements.txt`;
     - `python scripts/bot.py run --mode $MODE` (push → none, schedule → auto); env `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `SITE_URL=https://<owner>.github.io/<repo>/`;
     - коммит только `data/` и только при изменениях (retry с `pull --rebase`);
     - `upload-pages-artifact` (path `docs`), если `deploy=='true'`;
     - keepalive: `gh api -X PUT repos/$REPO/actions/workflows/rozklad.yml/enable` (continue-on-error).
   - job `pages`: `needs: update`, `if deploy=='true'`, environment `github-pages`, `deploy-pages`.
2. **`.github/workflows/worker.yml`** (триггеры: push по `worker/**`, `wrangler.json`, сам workflow; `workflow_dispatch`):
   - нет `CLOUDFLARE_API_TOKEN` или `TELEGRAM_BOT_TOKEN` → warning и выход;
   - account id: secret `CLOUDFLARE_ACCOUNT_ID` или `GET /client/v4/accounts`;
   - KV `pfk-rozklad-users`: найти или создать через REST, id вписать в `wrangler.json` через jq;
   - `npx wrangler@4 deploy --var SITE_URL:<url>`, из вывода взять `*.workers.dev`;
   - `wrangler secret bulk` с `BOT_TOKEN`;
   - `setWebhook(url, secret_token=<секрет выше>, allowed_updates=["message","callback_query","my_chat_member"])`.
3. **README.md** — переписать по-украински:
   - разовая настройка: секреты `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (админ), `CLOUDFLARE_API_TOKEN` (шаблон «Edit Cloudflare Workers»), при необходимости `CLOUDFLARE_ACCOUNT_ID`; затем Actions → workflow воркера → Run;
   - как присоединяются пользователи: бот → Почати → курс → група, `t.me/<bot>?start=<slug>`, групповые чаты;
   - убрать упоминание «Мудрий»/Antigravity.
4. Проверить: `SITE_URL=https://yaroslav28077.github.io/pfk-rozklad/ python3 scripts/bot.py run` (без токена — только сборка), `python3 scripts/bot.py show СОБ-11 --date 2026-11-03`.
5. Открыть PR `multi-group` → `main` с описанием. Пользователю: добавить `CLOUDFLARE_API_TOKEN`, смержить, запустить workflow воркера.
6. После мержа удалить этот файл.
