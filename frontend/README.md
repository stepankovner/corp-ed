# Kronto — фронтенд

Интерфейс MVP: вход, вопросы по документам со ссылками на источники,
«Мои источники» (подключение личных аккаунтов Битрикс24 и Яндекс 360) и
управление для администратора — документы, подключения, сотрудники,
пробелы в документах, глоссарий, лимит вопросов, журнал действий.

Дизайн — лендинга krontoai.ru (репозиторий demo-kronto): токены
`src/styles/tokens.css`, шрифты Onest и JetBrains Mono (OFL, лицензии
рядом), паттерны окна чата, источников и форм.

## Стек

React 19, TypeScript (strict), Vite 8, React Router 7, TanStack Query 5,
`openapi-fetch` с типами из схемы бэкенда, CSS Modules, `react-markdown`.
Тесты — Vitest + Testing Library + MSW; сквозные — Playwright. Почему
так — `docs/DECISIONS.md`, запись 2026-09-28.

## Запуск для разработки

Нужны Node 22 и работающий бэкенд. Ключи Yandex Cloud не нужны:
`LLM_PROVIDER=fake` отвечает первой найденной выдержкой.

```bash
# бэкенд (из корня репозитория; .env — по .env.example)
LLM_PROVIDER=fake RAG_FAQ_MAX_DISTANCE=0.8 uv run uvicorn corp_ed.main:app --reload
LLM_PROVIDER=fake uv run python -m corp_ed.worker
uv run python -m corp_ed.cli create-tenant --code acme --name "ACME" \
    --seats 20 --admin-email admin@acme.ru     # печатает временный пароль

# фронт
cd frontend
npm ci
npm run dev          # http://localhost:5173, /api проксируется на :8000
```

Адрес API для прокси разработки — `KRONTO_BACKEND` (по умолчанию
`http://127.0.0.1:8000`).

## Проверки

```bash
npm run lint && npm run format:check && npm run typecheck && npm test && npm run build
npm run check:api    # типы клиента совпадают со схемой бэкенда
```

После изменения API на бэкенде — `npm run gen:api` (пишет `openapi.json`
и `src/api/schema.d.ts`) и коммит обоих файлов.

Сквозные проверки — против живого стенда со свежей компанией:

```bash
E2E_COMPANY=acme E2E_ADMIN_EMAIL=admin@acme.ru E2E_ADMIN_PASSWORD=<временный> \
    npm run e2e
```

`PW_CHROMIUM_PATH` — путь к Chromium, если браузер Playwright не
установлен (`npx playwright install chromium`).

## Устройство

| Каталог     | Что внутри                                                                      |
| ----------- | ------------------------------------------------------------------------------- |
| `src/api`   | клиент: токены (`session.ts`), обновление под межвкладочной блокировкой, ошибки |
| `src/auth`  | вход, смена временного пароля, охрана маршрутов                                 |
| `src/chat`  | вопросы и ответы, маркеры `[n]` → кнопки, панель фрагмента                      |
| `src/admin` | разделы управления                                                              |
| `src/pages` | вход, смена пароля, «Мои источники», 404                                        |
| `src/ui`    | кнопки, поля, бейджи, модальные окна — по лендингу                              |

Выкладка — `Dockerfile` (nginx без root, только статика) и
`docs/DEPLOY.md` §5: `/api` внешний прокси ведёт прямо в API.
