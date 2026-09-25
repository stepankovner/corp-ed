# Handoff для бэкенда: что встроить из ML

**От:** ML (Артём) · **Кому:** бэкенд-агент / Братанчик (менторский режим)
**Версия:** v1.2 · 25.09.2026 (вечер) — сверка с веткой бэкенда: что
встроено и как, отличия от handoff, новые BH-25 и BH-26. v1.1 (25.09,
утро) — решение по задаче 1: Alice AI LLM Flash + text-embeddings-v2, 768.
v2 — к 06.10, финал — к 13.10.
**Основа:** ТЗ ML-части (23.09), `docs/ml-report.md`, `docs/ml-plan.md`.

ML пишет только чистые функции (без БД, сети, файлов), промпты и eval.
Всё, что ходит в БД и во внешние API, — ваше. Здесь — что встроить, в каком
порядке и где у вас есть выбор. ML-код в бэкенд-файлы не лезет.

## Как читать пункт

- **Зачем / Блокирует / Приоритет / Срок.** P0 — без этого не работает ответ
  или eval; P1 — нужно к pre-MVP 14.10; P2 — MVP.
- **Что сделать** — модель и миграция (SQL), какие ML-функции вызывать и в
  каком порядке, API (метод, путь, схемы, роль).
- **Где выбор** — альтернативы и цена каждой. Решение ваше, если не сказано
  иначе.
- **Изоляция тенантов.** ORM-хуки (`_apply_tenant_filter`,
  `_check_tenant_on_write`) не покрывают колоночные `select`, bulk
  `DELETE/UPDATE` и сырой SQL — там фильтр `tenant_id` пишется явно.
- **Приёмка** — тесты, всегда с тестом «чужой тенант».
- **Ловушки** — что уже ломалось у нас или у компаний из ТЗ.

## Код приходит PR-стеком (вливать по порядку)

| Ветка | PR | Что | Пункты |
|---|---|---|---|
| `ml/preprocess-split` | #3 | `preprocess`, `split_document`, `split_sections` | BH-2, BH-3 |
| `ml/retrieval-utils` | #4 | `rrf_merge`, `to_fulltext_query`, `expand_query` | BH-12, BH-14 |
| `ml/eval` | #5 | eval-стенд, клиент Яндекса для eval | BH-5 |
| `ml/prompt-v2` | #6 | промпт faq-v2.1, `select_context`, `normalize_citations` | BH-1, BH-7 — **только вместе с BH-1** |
| `ml/prep` | #7 | этот документ, `docs/ml-plan.md` | — |
| `ml/flash-v2` | #8 | задача 1: замеры, промпт faq-v2.3, решение Flash + v2-768 | BH-15…BH-19 |
| `ml/golden-tools` | #9 | инструменты золотого набора (только eval) | — |
| `ml/gaps` | #10 | `corp_ed.domain.gaps`, промпт gaps-v1 | BH-20…BH-23 |
| `ml/judge-kappa` | #11 | калибровка судьи (только eval) | — |
| `ml/alice-compare` | #12 | протокол сравнения с Алисой (только eval) | — |
| `ml/r1-general` | #13 | Р1: общий ответ с пометкой, промпт `faq-v2.4` | BH-24 |
| `ml/docs` | #14 | `docs/ml-summary.md`, `docs/ml-code-guide.md`, этот документ v1.2 | BH-25, BH-26 |
| `ml/m2-sections` | #16 | M2: `Section`, `merge_chunks`, `select_sections` с окном; стенд `--context`, `--dry-run` | BH-13 |
| `ml/golden-runs` | #17 | золотой dev: решения A7/A8/M1/M2, `eval.judge --api`, состав ТЗ — минимум | — (менять нечего) |

#7–#17 вливаются после #6 — CI у них падает на той же ожидаемой ошибке.
#15 — PR бэкенда (`claude/gallant-pascal-xtb35t` → `main`, 62 коммита):
ML-стек до `ml/docs` внутри, CI зелёный.

### Статус у бэкенда (ветка `claude/gallant-pascal-xtb35t`, вечер 25.09, 10b7857)

Сверка ML 25.09 по коду ветки, без правок в ней. Ruff и format чистые.
`pytest`: 473 passed (в том числе все тесты ML), 279 требуют Postgres.
8 ошибок mypy и 1 упавший тест — только на Windows (`resource.setrlimit`
в `ingest/extract_worker.py`); в CI на Linux их нет.

- ML-стек влит до `ml/docs` 4b50b76 (слияние df70bdc): промпт `faq-v2.4`,
  документы для команды.
- **Сделано:** BH-1…BH-12, BH-14…BH-20, BH-24. Сверено с тем, как ML
  мерил: Flash через `/v1/chat/completions` (`/latest`, `max_tokens`
  1000); `text-embeddings-v2` с `dim=768` и проверкой размерности на
  старте (ловушка 256 закрыта); `RAG_FAQ_MAX_DISTANCE=0.51`, температура
  0, `RAG_FAQ_LIMIT=5`, `RAG_CONTEXT_MAX_TOKENS=3000`, 400/50; гибрид —
  глубина 50, RRF k=60, веса 1.0/0.5, порог на лучшем векторном
  кандидате; словарь — только в поиск; извлечение docx/pdf — как в
  стенде ML (mammoth + markdownify ATX, pymupdf4llm постранично, `\f`);
  `qa_log` — все поля для `classify_miss`, включая `miss_kind`.
- **Не сделано:** BH-13 (M2, контракт черновой), BH-21…BH-23 (ночная
  задача пробелов, таблицы кластеров, `GET /gaps`). `qa_log.miss_kind`
  пока никто не заполняет.
- **Отличия от handoff — принимаю, handoff подстроен:** режим Р1 — не
  `RAG_NOT_FOUND_MODE`, а поле компании `tenants.not_found_mode`
  (general | strict); поле ответа — `origin` (documents |
  general_knowledge | none), а не `answer_source`; `/faq/search` отдаёт
  `material_title` и `fulltext_rank`, принимает `retriever`.
- **Найдено при сверке:** BH-25 (`content_filter` уходит клиенту как 502),
  BH-26 (версия модели не пишется в `qa_log`). Оба ниже.
- **Решения ML по золотому dev (25.09, вечер; `docs/ml-report.md`,
  раздел «Золотой dev»):** текущие значения остаются —
  `RAG_CHUNK_TOKENS=400/50`, `RAG_FAQ_LIMIT=5`, `RAG_FAQ_MAX_DISTANCE=0.51`,
  `RAG_RETRIEVER=vector`, крошки с названием документа; M2 (BH-13) —
  после MVP. Менять ничего не нужно. Финал — на holdout к 12.10.
- **Роли после разворота продукта — ADMIN и EMPLOYEE.** Где в пунктах
  написано MANAGER, читать ADMIN; где INTERN — EMPLOYEE.
- ML-ветки обновляются только новыми коммитами и слияниями, без
  переписывания истории. Чтобы забрать изменения, влейте `ml/docs`
  ещё раз.

### Ожидаемые падения на `ml/prompt-v2` (не чинить в ML-ветках)

- `mypy`: `services/faq_service.py:61` — `arg-type`: `ChunkMatch` без `title`
  и `heading_path` не подходит под `SourceChunk`. Уходит с BH-1.
- `tests/test_faq_service.py::test_found_chunks_reach_the_prompt` ищет в
  промпте `"Выдержка 1"`, а в v2 выдержка — `"[1] …"`. Правка — BH-10.
- Локально без Postgres падают 41 тест с БД — это не ML, в CI они с базой.

---

## BH-1. `ChunkMatch` с `title` и `heading_path`, `content` = `llm_text`

- **Зачем:** промпт v2 подписывает каждую выдержку источником
  «[1] Положение > Раздел 3 > 3.2 …»; без этого модель не знает, из какого
  раздела фрагмент, а фронт — что показывать в ссылке.
- **Блокирует:** вливание PR #6 (промпт v2), BH-7, BH-10.
- **Приоритет / Срок:** P0 · 28.09 (задача бэкенда 1.5).
- **Что сделать:**
  1. `domain/types.py`:
     ```python
     @dataclass(frozen=True)
     class ChunkMatch:
         id: UUID
         content: str              # llm_text: крошки + Markdown
         material_id: UUID
         position: int
         distance: float
         title: str                # materials.title
         heading_path: list[str]   # chunks.heading_path (BH-3)
     ```
  2. `ChunkRepository.search`: `JOIN materials ON materials.id =
     chunks.material_id`, в `select` — `Material.title`, `Chunk.heading_path`.
  3. Промпт принимает любой объект с `content`, `title`, `heading_path`
     (Protocol `SourceChunk` в `prompts/faq.py`) — наследовать ничего не надо.
- **Где выбор:**
  - поле `title` в `ChunkMatch` (просто, одно имя с промптом) **или** поле
    `material_title` (как в вашем PR #2) + свойство `title` → тогда ML меняет
    Protocol. Скажите, что выбрали, — подстроюсь за 10 минут;
  - `title` из JOIN при поиске (всегда свежий) **или** копия в `chunks`
    (быстрее, но устаревает при переименовании материала). Рекомендую JOIN.
- **Изоляция тенантов:** `search` — колоночный `select`, хук не сработает;
  фильтр `Chunk.tenant_id == tenant_id` уже стоит — не потерять при JOIN.
  На `materials` фильтр тоже явно: `Material.tenant_id == tenant_id`.
- **Приёмка:** тест: у найденного чанка `title` = название материала,
  `heading_path` = из БД; тест: чанки материала другого тенанта с тем же
  названием в выдачу не попадают.
- **Ловушки:** `title` должен быть человеческим («Правила отбора в
  акселератор»), а не именем файла — он уходит в промпт и в крошки
  эмбеддинга (BH-3).

## BH-2. Извлечение docx / pdf → Markdown

- **Зачем:** нарезка v2 держится на заголовках `#` и таблицах; из «плоского»
  текста секций не получится.
- **Блокирует:** BH-3 (реальные документы), переингест демо-корпуса.
- **Приоритет / Срок:** P0 · 30.09.
- **Что сделать:** функция `extract(file) -> str` (ваша, в `ingest/` или
  сервисе — это сеть/файлы):
  ```python
  # docx
  html = mammoth.convert_to_html(file).value
  markdown = markdownify.markdownify(html, heading_style="ATX")
  # pdf — страницы склеить символом \f (PAGE_BREAK из corp_ed.ingest.preprocess)
  pages = pymupdf4llm.to_markdown(str(path), page_chunks=True)
  markdown = PAGE_BREAK.join(page["text"] for page in pages)
  # txt, md — как есть
  ```
  Затем `preprocess(markdown)` (ML) → BH-3.
- **Где выбор:**
  - docx: `mammoth + markdownify` (проверено на 6 документах: заголовки,
    таблицы, списки) **или** `markitdown` (то же, он внутри на mammoth,
    но тащит больше зависимостей);
  - pdf: `pymupdf4llm` (заголовки и таблицы есть) **или** `docling` (лучше
    таблицы, но тяжёлый: модели, секунды на страницу); `markitdown` для
    PDF — **ноль заголовков**, не брать;
  - как файл попадает в систему: сейчас `POST /materials` принимает текст в
    JSON. Нужна загрузка файла (`multipart/form-data`) — это отдельное
    решение по API (лимиты размера, хранение исходника).
- **Изоляция тенантов:** материал создаётся с `tenant_id` текущего
  пользователя (как сейчас).
- **Приёмка:** на 2–3 файлах корпуса: заголовки → `#`, таблица → таблица,
  у PDF между страницами `\f`.
- **Ловушки:** сканы PDF без текстового слоя дадут пустой текст — отдавать
  ошибку «нет текста», а не пустой материал. Колонки таблиц в PDF
  перемешиваются (найдено 25.09 на Положении Старт-ИИ-1) — это ухудшает и
  поиск, и сверку цитат; ML разбирается в `preprocess`.

## BH-3. Схема `chunks`: `heading_path`, `embed_text`, ингест через `split_document`

- **Зачем:** эмбеддинг по чистому тексту с крошками, в промпт — Markdown
  (решение A3; замеры E1–E3 в `ml-report.md`).
- **Блокирует:** BH-1 (`heading_path`), BH-12 (`tsvector` из `embed_text`),
  все эксперименты A7 на живом API.
- **Приоритет / Срок:** P0 · 30.09.
- **Что сделать:**
  1. Миграция:
     ```sql
     ALTER TABLE chunks ADD COLUMN heading_path text[] NOT NULL DEFAULT '{}';
     ALTER TABLE chunks ADD COLUMN embed_text text NOT NULL DEFAULT '';
     -- chunks.content теперь = llm_text (крошки + Markdown)
     ```
  2. Ингест, по порядку:
     ```python
     from corp_ed.ingest.preprocess import preprocess
     from corp_ed.domain.split import split_document

     clean = preprocess(extract(file))                    # BH-2
     drafts = split_document(
         clean,
         title=material.title,
         chunk_tokens=rag.chunk_tokens,                   # BH-9
         overlap_tokens=rag.overlap_tokens,
     )
     for draft in drafts:
         emb = await embeddings.embed_document(draft.embed_text)   # НЕ llm_text
         Chunk(material_id=material.id, position=draft.position,
               heading_path=draft.heading_path, embed_text=draft.embed_text,
               content=draft.llm_text, embedding=emb.embedding, ...)
     ```
  3. Старая `split_into_chunks` остаётся, пока вы не переключились.
  4. Сделано у бэкенда 25.09 (7f2aa4f, миграция 453c802d4852) на
     `text-search` 256. Эмбеддер меняется на text-embeddings-v2 768
     (BH-16, BH-17): следующий переингест — через переиндексацию BH-6.
- **Где выбор:** `heading_path` как `text[]` (просто, индексируемо) **или**
  `jsonb` (гибче, но в Python — ручная валидация); `content` = `llm_text`
  **или** отдельная колонка `llm_text` (понятнее, но тогда `content` —
  мёртвая колонка до удаления). Рекомендую `text[]` и `content` = `llm_text`.
- **Изоляция тенантов:** `delete_by_material` — bulk `DELETE`, фильтр по
  тенанту уже явный, не потерять при переписывании ингеста.
- **Приёмка:** тест ингеста: у чанков `heading_path` и `embed_text`
  заполнены, эмбеддинг считался по `embed_text` (фейковый эмбеддер
  проверяет входной текст); чанк не пересекает секцию.
- **Ловушки:**
  - Лимит `text-search-doc` — 2048 токенов, у `text-embeddings-v2-doc` —
    8192; при превышении **ошибка 400**, а не обрезка. Чанки v2 при 400/50 —
    до ~350 реальных токенов, запас есть.
  - Эмбеддинг по `llm_text` вместо `embed_text` — тихая потеря качества
    (разметка в эмбеддинге — шум, Битрикс24).

## BH-4. Фоновый ингест с ограничителем частоты

- **Зачем:** сейчас ингест синхронный (~0,7 с на чанк): документ на 300
  чанков держит запрос минуты.
- **Блокирует:** загрузку реальных документов клиентом.
- **Приоритет / Срок:** P1 · 30.09.
- **Что сделать:** `POST /materials/{id}/ingest` ставит задачу и сразу
  отвечает `202` со статусом; воркер нарезает и эмбеддит. Все вызовы
  эмбеддера — через общий ограничитель частоты.
- **Где выбор:**
  - `BackgroundTasks` FastAPI (ноль инфраструктуры, но задача умирает с
    процессом и нет повтора) **или** очередь `arq`/Redis (повторы, статус,
    но новый сервис) **или** таблица `ingest_jobs` + воркер-процесс
    (надёжно без Redis, но пишете опрос сами);
  - ограничитель: общий токен-бакет в процессе (просто, но не работает при
    нескольких воркерах) **или** в Redis (честно на весь каталог).
- **Изоляция тенантов:** задача хранит `tenant_id`; воркер выставляет
  контекст тенанта (`current_tenant`) до работы с БД — иначе хуки записи
  отклонят чанки или, хуже, запишут без проверки.
- **Приёмка:** тест: запрос возвращается сразу, чанки появляются после
  воркера; тест: воркер тенанта A не видит материал тенанта B.
- **Ловушки:**
  - Квота эмбеддингов — **10 запросов в секунду на каталог**; без
    ограничителя параллельный ингест сразу ловит 429, а повторы «все разом»
    снова упираются в квоту. Повторы тоже должны идти через слоты
    ограничителя (образец — `RateLimiter` в `eval/yandex.py`).
  - Квота общая с поиском: **поиск приоритетнее ингеста** — например,
    ингесту 6 запросов в секунду, остальное — эмбеддингу вопросов.

## BH-5. `POST /api/v1/faq/search` — отладка поиска для eval

- **Зачем:** метрики поиска (Hit@K, MRR) и порог отказа (A8) по живой базе;
  ML-клиент `eval/run_eval.py --mode retrieval` уже готов.
- **Блокирует:** A7/A8 на живом API (до этого — офлайн-стенд).
- **Приоритет / Срок:** P0 · 01.10.
- **Что сделать:**
  ```http
  POST /api/v1/faq/search            роль: MANAGER
  {"question": "Сколько дней отпуска?", "limit": 10}
  ```
  ```json
  {"matches": [{"chunk_id": "…", "material_id": "…", "material_title": "…",
                "position": 12, "heading_path": ["Раздел 3", "3.1 …"],
                "content": "…", "distance": 0.52}]}
  ```
  Тот же поиск, что в `/faq/ask` (эмбеддинг вопроса → `search`), **без**
  порога `max_distance` и без LLM. Роль — `require_role(UserRole.MANAGER)`.
- **Где выбор:** отдельный эндпоинт (чисто) **или** флаг `debug=true` у
  `/faq/ask` (меньше кода, но смешивает отладку с продуктом). Рекомендую
  отдельный. `limit` ограничить сверху (например, 50).
- **Изоляция тенантов:** тот же `search` с явным фильтром.
- **Приёмка:** тест: MANAGER получает top-K с расстояниями, отсортированные
  по возрастанию; INTERN — 403; чанки чужого тенанта не возвращаются.
- **Ловушки:** порог здесь **не** применять — для подбора порога нужны
  расстояния и у вопросов, которые его не прошли.

## BH-6. Скрипт переиндексации

- **Зачем:** после каждой смены нарезки или эмбеддера (задача 1) — пересчёт
  всех чанков тенанта.
- **Блокирует:** A7 на живом API, переезд на новый эмбеддер.
- **Приоритет / Срок:** P1 · 02.10.
- **Что сделать:** `python -m corp_ed.scripts.reindex --tenant <id> | --all
  [--dry-run]`: для каждого материала — `preprocess → split_document →
  эмбеддинг` и замена чанков материала **в одной транзакции**
  (`delete_by_material` + `bulk_create`). Прогресс в лог.
- **Где выбор:** переиндексация «на месте» (просто; во время прогона поиск
  по материалу видит то старые, то новые чанки только внутри транзакции —
  снаружи атомарно) **или** в новую таблицу со сменой указателя (без окна,
  но сложнее). Для pre-MVP — на месте.
- **Изоляция тенантов:** скрипт выставляет контекст тенанта на каждый
  материал; `--all` идёт по тенантам по очереди.
- **Приёмка:** тест: после переиндексации чанков столько, сколько даёт
  `split_document`, у всех новая модель в `chunks.model`; чужой тенант не
  тронут.
- **Ловушки:** тот же ограничитель частоты (BH-4); при смене модели
  эмбеддингов переиндексировать **всё**, иначе в одной выдаче окажутся
  векторы двух моделей (расстояния несравнимы).

## BH-7. Ответ: `NOT_FOUND_ANSWER`, `normalize_citations`, порядок источников

- **Зачем:** одна фраза отказа на всю систему (фронт распознаёт её по
  началу), ссылки без битых номеров.
- **Блокирует:** —. **Приоритет / Срок:** P0 · вместе с BH-1.
- **Что сделать** (`services/faq_service.py`):
  ```python
  from corp_ed.domain.context import select_context
  from corp_ed.prompts.faq import (NOT_FOUND_ANSWER, PROMPT_VERSION,
      build_faq_messages, is_not_found, normalize_citations)

  matches = await chunk_repo.search(embedding, limit=rag.faq_limit)
  relevant = [m for m in matches if m.distance <= rag.faq_max_distance]
  context = select_context(relevant, max_tokens=rag.context_max_tokens)
  if not context:
      return FaqAnswer(content=NOT_FOUND_ANSWER, answer_given=False, sources=[])
  completion = await llm.generate(build_faq_messages(question, context),
                                  temperature=rag.faq_temperature)   # BH-8
  content = normalize_citations(completion.content, context)
  answer_given = not is_not_found(content)
  return FaqAnswer(content=content, answer_given=answer_given,
                   sources=context if answer_given else [])
  ```
  `NO_ANSWER_TEXT` удалить — вместо него `NOT_FOUND_ANSWER`.
- **Режим «не найдено» (Р1) решён 25.09:** общий ответ с пометкой — см.
  BH-24. Код выше — строгий отказ; он остаётся вариантом настройки.
- **Изоляция тенантов:** не затрагивает.
- **Приёмка:** тест: пустой контекст → `NOT_FOUND_ANSWER`, LLM не вызывается;
  тест: модель ответила «В документах компании ответа нет.» при найденных
  выдержках → `answer_given=False`, `sources=[]`; тест: `[4.2]` и `[38]` в
  ответе модели превращаются в номер выдержки.
- **Ловушки:**
  - **порядок `sources` = порядок выдержек в промпте**: модель ссылается
    номерами `[1]`, `[2]`;
  - модели ставят в скобки номер пункта `[4.2]` или строки таблицы `[38]`
    (замер 25.09: 9 таких ссылок на 220 ответов у Alice Flash, 22 у Lite) —
    `normalize_citations` вызывать всегда.

## BH-8. Температура FAQ = 0 отдельным параметром

- **Зачем:** при 0.3 на одних и тех же выдержках одинаковых ответов 15 из 24,
  один вопрос переключался «ответил ↔ отказал»; при 0 — 24 из 24, правильность
  не хуже (замер 24.09).
- **Приоритет / Срок:** P1 · 29.09.
- **Что сделать:** `RagSettings.faq_temperature` (BH-9) и передача в
  `llm_gateway.generate(..., temperature=...)` из `faq_service`. Дефолт
  `LLMGateway.generate` (0.3) для программ оставить.
- **Где выбор:** параметр в `RagSettings` (настраивается без деплоя) **или**
  константа в `faq_service` (проще, но ТЗ просит параметры через настройки).
- **Приёмка:** тест с фейковым шлюзом: `faq_service` передаёт температуру из
  настроек.
- **Ловушки:** не менять дефолт шлюза глобально — программам нужна
  вариативность.

## BH-9. `RagSettings` и `.env.example`

- **Зачем:** размеры теперь в токенах, добавились бюджет контекста и
  температура. Значения — за ML.
- **Приоритет / Срок:** P0 · 29.09 (вместе с BH-3).
- **Что сделать:**
  ```dotenv
  # RAG — значения за ML. Размеры в ТОКЕНАХ (count_tokens = len/3).
  RAG_CHUNK_TOKENS=400          # кандидат 600/50 — финал на золотом (A7 E3)
  RAG_OVERLAP_TOKENS=50
  RAG_FAQ_LIMIT=5               # уточнится в E4
  RAG_FAQ_MAX_DISTANCE=0.65     # для text-search; с v2-768 — 0.51 (BH-18); финал — A8 к 12.10
  RAG_CONTEXT_MAX_TOKENS=3000   # при 600/50 — 3300
  RAG_FAQ_TEMPERATURE=0
  ```
  Поля `chunk_tokens`, `overlap_tokens`, `context_max_tokens`,
  `faq_temperature` в `RagSettings`; `chunk_size`/`chunk_overlap` удалить после
  переключения ингеста. Валидация `0 <= overlap_tokens < chunk_tokens`.
- **Где выбор:** без дефолтов (как сейчас: придуманные числа не станут
  продакшен-значениями) **или** с дефолтами из этого пункта.
- **Приёмка:** тест настроек: `overlap >= chunk` → ошибка на старте.
- **Ловушки:** числа токенов — в единицах `count_tokens` (`len/3`), это
  ~1,5 раза больше реальных токенов Яндекса: 400 ≈ 245 реальных.

## BH-10. Поправить `test_found_chunks_reach_the_prompt`

- **Зачем:** тест ищет `"Выдержка 1"`, в промпте v2 — `"[1] …"`.
- **Приоритет / Срок:** P0 · вместе с BH-1.
- **Что сделать:** проверять, что текст чанка и `"[1]"` есть в сообщении
  пользователя (или `title` из BH-1).
- **Ловушки:** не проверять весь текст промпта целиком — он будет меняться
  по версиям (`PROMPT_VERSION`).

## BH-11. `qa_log` — минимум для pre-MVP

- **Зачем:** без версии промпта и модели нельзя привязать 👍/👎 и eval к
  изменениям. Полная схема под отчёт о пробелах — v2 (задача ML 3.5).
- **Приоритет / Срок:** P1 · 06.10 (полная — в v2).
- **Что сделать:** рядом с ответом писать `PROMPT_VERSION` (сейчас
  `faq-v2.1`), модель, расстояние лучшего чанка, `answer_given`.

---

## MVP (перенесено из контрактов; подробности — в v2 к 06.10)

### BH-12. M1 — гибридный поиск (P2)

1. Миграция:
   ```sql
   ALTER TABLE chunks ADD COLUMN fts tsvector
     GENERATED ALWAYS AS (to_tsvector('russian', embed_text)) STORED;
   CREATE INDEX chunks_fts_idx ON chunks USING gin (fts);
   ```
2. `search_fulltext(question, limit)` — **сырой SQL, фильтр по тенанту явно**:
   ```sql
   SELECT id, ts_rank_cd(fts, q) AS rank
   FROM chunks, websearch_to_tsquery('russian', :q) AS q
   WHERE tenant_id = :tenant_id AND fts @@ q
   ORDER BY rank DESC LIMIT :limit
   ```
   `:q = to_fulltext_query(question)` (`corp_ed.domain.fulltext`); пустая
   строка — ветку не вызывать. Вопрос как есть не передавать: слова вне
   кавычек `websearch_to_tsquery` соединяет через И, и ветка почти всегда
   пустая.
3. `rrf_merge([vector_ids, fulltext_ids], weights=[1.0, 0.5], k=60)`
   (`corp_ed.domain.fusion`).
4. **Порог отказа — на расстоянии лучшего ВЕКТОРНОГО кандидата**, не на
   скоре RRF (он зависит только от рангов; у МТС скоры «схлопывались»).
   Случай «вектор не прошёл порог, полнотекст нашёл точное совпадение» —
   решение по eval (задача ML 2.3).

### BH-13. M2 — small-to-big (P2; контракт чистовой 25.09; «в MVP / после» — по золотому dev)

Ищем маленькими чанками, модели показываем секцию целиком. Замер ML
25.09 (черновик золотого набора, 37 вопросов с цитатой, без LLM —
бесплатно): цитата эталона в контексте — **28 из 37 против 26 у чанков**
(+2: секция найдена, но чанк с ответом — не в top-5); контекст **+50 %
токенов** (2 144 против 1 425 на вопрос, ≈ +0,07 ₽ к ответу за 0,20 ₽).
Окно соседей без таблицы sections (чанк ± 1–2) не даёт ничего: 26 из 37.
Остальные 9 промахов — порог (5) и поиск (4), M2 их не лечит. Итог по
ответам (судья) — на золотом dev после проверки людьми, ~50 ₽; **до
этого не встраивать**, контракт — чтобы оценить объём.

1. Миграция: `sections(id, tenant_id, material_id, position, heading_path
   text[], content text)` под RLS, как `chunks`; у `chunks` —
   `section_id` (FK; NOT NULL после переингеста). `content` — `llm_text`
   секции из `SectionDraft`: крошки + Markdown секции целиком.
2. Ингест: `split_sections(...)` вместо `split_document(...)`
   (`corp_ed.domain.split`) — те же чанки (`split_document` — это
   `split_sections` без группировки) плюс `SectionDraft.llm_text` и
   `position` секции. Переингест обязателен: у старых чанков `section_id`
   нет.
3. Ответ: после порога и RRF — `select_sections(matches, sections,
   max_tokens=context_max_tokens, neighbours=1)` из `corp_ed.domain.context`
   вместо `select_context`. `sections` — `{section_id: Section(
   content=<sections.content>, chunks=[<chunks.content секции по
   position>])}` для секций найденных чанков — один запрос
   `section_id IN (...)`. `ContextBlock` подходит для `build_faq_messages`
   как есть; источники ответа и `answer_given` — по `block.match` (чанк, по
   которому секция попала в контекст), не по секции.
4. Настройка `RAG_CONTEXT_MODE=chunks|sections` без дефолта, как остальные
   `RAG_*`: переключает ML по замеру.
- **Приёмка:** найден один чанк секции из трёх → в промпте секция целиком
  под одним номером [1]; секция длиннее остатка бюджета → окно чанк ± 1
  (`merge_chunks`: крошки один раз, перекрытие без повтора); два чанка
  одной секции в выдаче → секция один раз, на месте лучшего; `sources`
  ответа — найденные чанки.
- **Ловушки:**
  - `Section.chunks` — тот же `content`, что у найденных чанков: чанк
    ищется в секции по тексту, иначе окна не будет;
  - секция длиннее `RAG_CONTEXT_MAX_TOKENS=3000` (8 из 175 в демо-корпусе,
    до 14 000 токенов) целиком не попадёт никогда — это норма, идёт окно;
  - `neighbours` 0 или 1 — в замере разницы нет; 2 и больше только
    дороже.

### BH-14. M5 — словарь сокращений (P2)

Таблица `glossary(term, expansion, tenant_id)`; перед эмбеддингом и
полнотекстом `question = expand_query(question, glossary)` из
`corp_ed.domain.query`. Выборка словаря — с фильтром по тенанту.

---

## Переезд на Alice AI LLM Flash и text-embeddings-v2 (задача ML 1, 25.09)

**Решение (Артём, 25.09): переезжаем.** Генерация — Alice AI LLM Flash,
эмбеддинги — text-embeddings-v2 с размерностью 768. Числа —
`docs/ml-report.md`, раздел «Задача 1».

**Порядок поставки:**
- **BH-15 (LLM)** не зависит от эмбеддингов, его можно выкатить первым.
- **BH-16 + BH-17 + BH-18** — одна поставка на тенанта: модель и
  размерность, колонка `vector(768)`, переингест, новый порог. Сначала
  переингест, потом новый порог. Старый порог 0.65 на векторах v2 пропускает
  почти всё: вопросы вне корпуса лежат на 0.37–0.65.
- **BH-6 (переиндексация) — раньше BH-17**: переингест всех тенантов идёт
  через неё. BH-3 уже сделан на старом эмбеддере.
- **A7 и A8 на живом API** — только после этой поставки: подбирать
  нарезку и порог на старом эмбеддере бессмысленно.

```dotenv
# Задача 1 — значения за ML; имена переменных — ваши.
LLM_PROVIDER=yandex-openai
LLM_MODEL=aliceai-llm-flash          # gpt://<folder>/aliceai-llm-flash/latest
EMBEDDING_MODEL=text-embeddings-v2   # -doc / -query
EMBEDDING_DIM=768
RAG_FAQ_MAX_DISTANCE=0.51            # предварительно; финал — A8 к 12.10
RAG_FAQ_TEMPERATURE=0
```

### BH-15. Адаптер LLMGateway для OpenAI-совместимого API (Alice AI LLM Flash)

- **Зачем:** Flash (и все модели каталога, кроме YandexGPT) доступны только
  через `POST https://llm.api.cloud.yandex.net/v1/chat/completions`. У Flash
  цена ответа 0,20 ₽ против 0,37 ₽ у Lite, p95 задержки 2,0 с против 4,1 с,
  F1 отказа 0,975–0,983 против 0,940 (e2e 25.09, 91 вопрос).
- **Блокирует:** переход на Flash. **Приоритет / Срок:** P1 · 02.10.
- **Что сделать:** новая реализация `LLMGateway` (`llm/yandex_openai.py` или
  аналог):
  ```http
  POST /v1/chat/completions
  Authorization: Api-Key <YC_API_KEY>
  OpenAI-Project: <YC_FOLDER_ID>
  {"model": "gpt://<folder>/aliceai-llm-flash/latest",
   "messages": [{"role": "system", "content": "…"}, …],
   "temperature": 0, "max_tokens": 1000}
  ```
  Ответ: `choices[0].message.content`, `choices[0].finish_reason`,
  `usage.prompt_tokens`, `usage.completion_tokens`
  (`completion_tokens_details.reasoning_tokens` — у рассуждающих моделей,
  тарифицируются как выход). Выбор реализации — по `LLM_PROVIDER` /
  `LLM_MODEL` в настройках. Образец — `YandexClient._chat` в `eval/yandex.py`.
- **Где выбор:** свой адаптер на `httpx` (как нативный, без зависимостей)
  **или** SDK `openai` с `base_url` (меньше кода, лишняя зависимость).
- **Приёмка:** тест с `httpx.MockTransport`: заголовки, `model`, разбор
  `usage`; ретраи на 429/5xx как у нативного адаптера.
- **Ловушки:**
  - **квота генерации — 10 одновременных запросов на каталог** (не в
    секунду): при нагрузке — семафор на число одновременных вызовов;
  - Flash при `temperature=0` не полностью детерминирован: на 91 вопросе
    одинаковых ответов в 3 прогонах 69 (у Lite — 91). Правильность и
    отказы от прогона к прогону не менялись.

### BH-16. Модель и размерность эмбеддингов — в конфиге

- **Зачем:** `text-embeddings-v2` ищет заметно лучше `text-search`
  (серебряный набор: MRR 0,386 → 0,632 на dev, p = 0,0001; 0,501 → 0,652
  на test, p = 0,019). Размерность у v2 задаётся полем `dim` (128, 256,
  512, 768; по умолчанию 256): 768 лучше 256 на +0,048 MRR (dev+test, n = 175,
  p = 0,003), 512 от 768 не отличается. Цена одна (0,0101 ₽ за 1000 токенов).
- **Приоритет / Срок:** P1 · 02.10.
- **Что сделать:** `YandexEmbeddingAdapter`: семейство моделей и размерность
  из настроек:
  ```python
  payload = {"modelUri": f"emb://{folder}/{family}-doc/latest", "text": text}
  if dim: payload["dim"] = str(dim)       # только для text-embeddings-v2
  ```
  `-doc` для документов, `-query` для вопросов — это **разные модели**
  (косинус 0,82 на одном тексте), путать нельзя. В `chunks.model` писать
  семейство и размерность (`text-embeddings-v2-doc@768`).
- **Решено (25.09): 768.** Отвергнутая альтернатива — 256: без миграции
  колонки, но MRR на 0,05 ниже.
- **Ловушки:**
  - лимит входа v2 — **8192 токена**, при превышении ошибка 400 (у
    text-search — 2048). Токенизатор другой: 3,5 символа на токен против
    4,65 — токенов на ~30 % больше, `count_tokens = len/3` по-прежнему с
    запасом;
  - 256 зашито в двух местах: проверка `EMBEDDING_DIM = 256` в
    `llm/yandex_embedding.py` и векторы `[0.1] * 256` в
    `FakeEmbeddingAdapter`. Размерность брать из настроек в обоих местах,
    иначе тесты молча проверяют старую.

### BH-17. Миграция `chunks.embedding` → `vector(768)` + полный переингест

- **Зачем:** колонка сейчас `Vector(256)`; векторы 768 в неё не лягут.
  Размерность 768 выбрана 25.09 — миграция обязательна.
- **Блокирует:** включение v2-768. **Приоритет / Срок:** P1 · 02.10
  (нужна BH-6).
- **Что сделать:**
  ```sql
  -- в одной миграции: старые векторы несовместимы, их всё равно пересчитывать
  DELETE FROM chunks;                                  -- или по тенанту, см. BH-6
  ALTER TABLE chunks ALTER COLUMN embedding TYPE vector(768);
  ```
  Потом BH-6 (переиндексация) для всех тенантов. Если есть ANN-индекс
  (`ivfflat`/`hnsw`) — пересоздать после переингеста.
- **Где выбор:** удалить чанки и переингестить (просто, окно без ответов на
  время переингеста) **или** новая колонка `embedding_v2 vector(768)`,
  заполнить в фоне, переключить поиск, старую удалить (без окна, две
  миграции).
- **Изоляция тенантов:** переингест — по тенантам, с контекстом тенанта.
- **Ловушки:** нельзя смешивать векторы двух моделей в одной выдаче —
  расстояния несравнимы; переключать модель и переингестить атомарно для
  тенанта.

### BH-18. Новый `RAG_FAQ_MAX_DISTANCE` для v2

- **Зачем:** шкала расстояний у v2 другая. Для text-search кандидат был
  0.65, для v2-768 — **0.51** (предварительно, задача ML 1.4: 91 вопрос,
  59 по корпусу / 32 вне; максимум «по корпусу» 0.506, F1 отказа по поиску
  0.944). Финал — A8 на золотом наборе к 12.10.
- **Что сделать:** `RAG_FAQ_MAX_DISTANCE=0.51` в `.env`; порог меняется
  **вместе** с моделью эмбеддингов (одна поставка с BH-16, BH-17).
- **Ловушки:** порог не отсекает близкие по теме вопросы вне корпуса
  («условия программы „Развитие“», «акселератор Сколково»): их расстояние
  0.37–0.49 — как у вопросов по корпусу. Отказ на них держит промпт
  (faq-v2.3), а не порог.

### BH-19. Температура FAQ и версия промпта

- BH-8 в силе: `temperature=0` и для Flash.
- `PROMPT_VERSION` теперь `faq-v2.3` (правило против переноса условий одной
  программы на другую, в правиле 4 и в напоминании) — писать в `qa_log`.
  С `ml/r1-general` — `faq-v2.4`: промпт по выдержкам тот же, изменилась
  пометка общего ответа (BH-24).

---

## Отчёт о пробелах (задача ML 3, 25.09)

ML-часть готова: `corp_ed.domain.gaps` (`classify_miss`, `cluster_questions`,
`cluster_priority`, `mask_pii`) и промпт `corp_ed.prompts.gaps` (gaps-v1:
`build_gap_messages`, `GAP_LABEL_SCHEMA`, `parse_gap_label`). Клиенту
показываем только темы, которых нет в базе; остальные классы — внутренний
мониторинг.

Замер на синтетике (корпус без УМНИК и правил акселератора, `eval/gaps_eval.py`,
`docs/ml-report.md`): одних gap-вопросов (вектор не прошёл порог) мало —
recall 0.36 при precision 0.82: близкие по теме пробелы проходят порог, и
модель на них отказывает. **Вместе с отказами модели при найденных
выдержках** — precision 0.88, recall 0.64. Поэтому в отчёт идут классы
`gap` **и** `model_refusal`.

### BH-20. Таблица `qa_log`

- **Зачем:** сигналы для отчёта о пробелах, привязка 👍/👎 и eval к версии
  промпта и модели (заменяет минимум из BH-11).
- **Приоритет / Срок:** P1 · 06.10.
- **Что сделать:**
  ```sql
  CREATE TABLE qa_log (
    id uuid PRIMARY KEY,
    tenant_id uuid NOT NULL REFERENCES tenants(id),
    user_id uuid NOT NULL REFERENCES users(id),
    question text NOT NULL,
    question_embedding vector(768) NOT NULL,   -- размерность = у chunks (BH-16/17)
    embedding_model text NOT NULL,             -- 'text-embeddings-v2-query@768'
    prompt_version text NOT NULL,              -- PROMPT_VERSION из prompts/faq.py
    llm_model text NOT NULL,
    best_vector_distance real,                 -- лучший ВЕКТОРНЫЙ кандидат, до порога
    best_fulltext_rank real,                   -- ts_rank_cd лучшего; NULL — ветка пуста
    answer_given boolean NOT NULL,
    source_chunk_ids uuid[] NOT NULL DEFAULT '{}',
    feedback smallint CHECK (feedback IN (-1, 1)),
    miss_kind text,                            -- заполняет ночная задача (BH-21)
    created_at timestamptz NOT NULL DEFAULT now()
  );
  CREATE INDEX qa_log_tenant_created_idx ON qa_log (tenant_id, created_at);
  ```
  Писать строку на каждый `/faq/ask` (в том числе отказ без LLM); 👍/👎 —
  отдельным `PATCH`.
- **Где выбор:** хранить сам текст вопроса (нужен для подписи кластеров,
  но это персональные данные по 152-ФЗ — срок хранения, например 90 дней,
  и удаление по запросу) **или** только маскированный (`mask_pii`) — меньше
  риска, но маскирование неидеально (списки имён).
- **Изоляция тенантов:** все выборки ночной задачи — сырым SQL с
  `WHERE tenant_id = :tenant_id`.
- **Приёмка:** тест: строка пишется и при отказе без LLM
  (`best_vector_distance` есть, `answer_given=false`, LLM не вызывался).
- **Ловушки:** `question_embedding` — тот же вектор, что ушёл в поиск (не
  считать второй раз); при смене модели эмбеддингов старые векторы с новыми
  не кластеризовать — окно начинать заново.

### BH-21. Ночная задача: классификация → кластеры → подпись

- **Приоритет / Срок:** P1 · 06.10 (v2).
- **Что сделать**, для каждого тенанта по очереди:
  ```python
  from corp_ed.domain.gaps import (GapThresholds, MissKind, MissSignals,
      classify_miss, cluster_questions, cluster_priority, ClusterQuestion)
  from corp_ed.prompts.gaps import GAP_LABEL_SCHEMA, build_gap_messages, parse_gap_label

  thresholds = GapThresholds(
      max_distance=rag.faq_max_distance,        # тот же порог, что в ответе
      strong_fulltext=gaps.strong_fulltext,     # подобрать по живым логам
      empty_fulltext=gaps.empty_fulltext,
      off_topic_distance=gaps.off_topic_distance,
  )
  # 1. классы для новых строк окна (например, 30 дней)
  kind = classify_miss(MissSignals(row.best_vector_distance,
                                   row.best_fulltext_rank, row.answer_given,
                                   row.feedback), thresholds)
  # 2. кластеры по строкам с kind in (GAP, MODEL_REFUSAL)
  labels = cluster_questions(vectors, max_distance=gaps.cluster_distance)  # 0.6
  # 3. приоритет и подпись каждого кластера
  priority = cluster_priority([ClusterQuestion(r.user_id, r.created_at) ...],
                              now=now, half_life_days=gaps.half_life_days)  # 14
  label = parse_gap_label(await llm.generate(
      build_gap_messages(top_questions), response_format=GAP_LABEL_SCHEMA))
  ```
  Параметры — в настройках (`GAPS_*`), не константами.
  `build_gap_messages` сам маскирует ПДн (`mask_pii`).
- **Где выбор:**
  - кластеризовать окно заново каждую ночь (просто; id кластеров меняются,
    статус «в работе» надо переносить по пересечению вопросов) **или**
    дописывать новые вопросы к существующим кластерам по близости к
    центроиду (id стабильны, больше кода);
  - подписывать LLM все кластеры (≈ 0,05 ₽ и 0,5 с на кластер на Flash)
    **или** только кластеры с приоритетом выше порога.
- **Изоляция тенантов:** кластеризовать **только в пределах тенанта** —
  векторы разных компаний не смешивать ни в одной матрице.
- **Приёмка:** тест с фейковыми векторами: вопросы тенанта B не попадают в
  кластеры тенанта A; строка с 👎 на ответе → `retrieval_miss`, в отчёт не
  идёт.
- **Ловушки:** кластеризация O(n²) по памяти — окно ограничить (несколько
  тысяч вопросов на тенант — секунды). Порог 0.6 — по синтетике (чистота
  0.83 по документу, крупнейший кластер 14 вопросов), уточнить на живых
  логах.

### BH-22. Таблицы `gap_clusters` и `gap_cluster_questions`

```sql
CREATE TABLE gap_clusters (
  id uuid PRIMARY KEY,
  tenant_id uuid NOT NULL REFERENCES tenants(id),
  title text NOT NULL,                 -- GapLabel.title
  missing text NOT NULL,               -- GapLabel.missing
  priority real NOT NULL,
  question_count int NOT NULL,
  user_count int NOT NULL,
  first_seen timestamptz NOT NULL,
  last_seen timestamptz NOT NULL,
  status text NOT NULL DEFAULT 'new'
    CHECK (status IN ('new', 'in_progress', 'resolved', 'dismissed')),
  prompt_version text NOT NULL,        -- gaps-v1
  updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX gap_clusters_tenant_priority_idx ON gap_clusters (tenant_id, priority DESC);
CREATE TABLE gap_cluster_questions (
  cluster_id uuid NOT NULL REFERENCES gap_clusters(id) ON DELETE CASCADE,
  qa_log_id uuid NOT NULL REFERENCES qa_log(id) ON DELETE CASCADE,
  tenant_id uuid NOT NULL,
  PRIMARY KEY (cluster_id, qa_log_id)
);
```
**Изоляция тенантов:** `tenant_id` в обеих таблицах, фильтр в каждом
запросе. **Приоритет / Срок:** P1 · 06.10.

### BH-23. `GET /api/v1/gaps` — только для админа

- **Что сделать:**
  ```http
  GET /api/v1/gaps?status=new&limit=20       роль: MANAGER
  ```
  ```json
  {"clusters": [{"id": "…", "title": "Оформление командировок",
                 "missing": "Нет положения о командировках: …",
                 "priority": 12.4, "question_count": 9, "user_count": 6,
                 "last_seen": "2026-10-05T…", "status": "new",
                 "sample_questions": ["Как оформить командировку?", "…"]}]}
  ```
  `sample_questions` — после `mask_pii`. Плюс `PATCH /api/v1/gaps/{id}`
  со `status`.
- **Изоляция тенантов:** `WHERE tenant_id = :tenant_id` явно; тест: админ
  тенанта A не видит кластеров тенанта B, INTERN — 403.
- **Ловушки:** показывать только `gap` и `model_refusal`; `retrieval_miss`,
  `unclear`, `off_topic` — во внутреннем мониторинге, клиенту не нужны.

---

## Р1: ответа в документах нет (задача ML 2.4, решено 25.09)

### BH-24. Общий ответ со строгой пометкой «не из документов компании»

- **Зачем:** решение Артёма 25.09. Когда в документах ответа нет,
  ассистент всё равно отвечает, но первой строкой прямо пишет, что ответ
  не из документов компании. Досье v3.2 описывает строгий отказ, его
  обновит Артём. Код сейчас строгий (BH-7) — меняется здесь.
- **Приоритет / Срок:** P1 · 02.10.
- **Что сделать** (`services/faq_service.py`, поверх BH-7):
  ```python
  from corp_ed.prompts.faq import (NOT_FOUND_ANSWER, build_faq_messages,
      build_general_messages, ensure_general_prefix, is_not_found,
      normalize_citations)

  if context:
      completion = await llm.generate(build_faq_messages(question, context),
                                      temperature=rag.faq_temperature)
      content = normalize_citations(completion.content, context)
      if not is_not_found(content):
          return FaqAnswer(content=content, answer_given=True,
                           answer_source="documents", sources=context)
  # Ответа в документах нет: ни одна выдержка не прошла порог
  # или модель по выдержкам ответила NOT_FOUND_ANSWER.
  if rag.not_found_mode == "strict":
      return FaqAnswer(content=NOT_FOUND_ANSWER, answer_given=False,
                       answer_source="none", sources=[])
  completion = await llm.generate(build_general_messages(question),
                                  temperature=rag.faq_temperature)
  return FaqAnswer(content=ensure_general_prefix(completion.content),
                   answer_given=False, answer_source="general", sources=[])
  ```
  Первая строка общего ответа — всегда `GENERAL_ANSWER_PREFIX`: «В
  документах компании ответа нет. Ниже — общая информация, не из
  документов компании:». `ensure_general_prefix` ставит её, даже если
  модель её потеряла.
- **API:** поле `answer_source` (`documents` / `general` / `none`) в ответе
  `/faq/ask`. По нему фронт рисует плашку «Ответ не из документов
  компании» отдельно от текста. Текстовая пометка остаётся для каналов без
  фронта: бот в мессенджере, Битрикс24.
- **Где выбор:** режим — настройка `RAG_NOT_FOUND_MODE=general|strict`, по
  умолчанию `general` (строгий — для клиентов, которым нельзя ничего сверх
  документов) **или** жёстко `general` (меньше кода). Рекомендую настройку:
  досье обещает «честный отказ», часть клиентов захочет именно его.
  **Сделано (772a719):** режим — поле компании `tenants.not_found_mode`,
  не настройка процесса. Лучше, чем предлагал handoff: строгий режим
  включается одной компании, а не всем сразу. Поле ответа — `origin`.
- **Изоляция тенантов:** не затрагивает (в общий промпт выдержки не идут).
- **Приёмка:**
  - пустой контекст → один вызов с общим промптом, ответ начинается с
    `GENERAL_ANSWER_PREFIX`, `sources=[]`, `answer_given=False`;
  - модель отказала по выдержкам → второй вызов, то же на выходе;
  - `strict` → `NOT_FOUND_ANSWER` без второго вызова.
- **Ловушки:**
  - **`answer_given=False` у общего ответа.** В `qa_log` и отчёте о
    пробелах (BH-20, BH-21) это промах: `gap` или `model_refusal`. Если
    считать его ответом, пробелы исчезнут из отчёта;
  - выдержки в общий промпт не передавать: модель смешает документы и
    общие знания, и пометка станет ложью;
  - второй вызов LLM — только на вопросах без ответа: Flash, общий промпт
    ~175 токенов на вход и 75 на выход — 0,03 ₽ и 0,6 с (p50, замер 25.09);
  - **общие знания модели устаревают:** без документа УМНИК модель пишет
    «от 18 до 30 лет», в документе 2026 года — до 35. Пометка обязательна
    всегда, плашка на фронте — крупно;
  - фронт распознаёт отказ по началу текста: общий ответ начинается с
    той же фразы `NOT_FOUND_ANSWER`, поэтому для плашки нужен
    `answer_source`, а не разбор текста.

---

## Найдено при сверке ветки бэкенда (25.09)

### BH-25. `finish_reason=content_filter` — отказ, а не 502 (P2)

`YandexOpenAIAdapter.parse_chat_response` бросает `LLMError(retryable=False)`
на любом `finish_reason`, кроме `stop` и `length`; `llm_error_handler`
превращает это в 502 «Сервис языковой модели недоступен». Но
`content_filter` — не недоступность: модель ответила («Я не могу
обсуждать эту тему…»), только пометила ответ. В замерах ML на Flash —
2 из 1 808 ответов (0,1 %), оба на законный вопрос сотрудника про
«международные списки, связанные с терроризмом»: в УМНИК-2026,
Старт-ИИ-1 и правилах ГПБ такие списки — основание для отказа в гранте.
Сотрудник увидит «сервис недоступен», повторит — и получит то же.
Кредит не спишется, но и строки в `qa_log` не будет: для отчёта о
пробелах вопрос исчезнет.

- **Предложение:** `content_filter` → новое `FinishReason.FILTERED`, без
  исключения. В `FaqService._answer` — как отказ модели, но **без второго
  вызова**: общий промпт на тот же вопрос отфильтруется так же. Ответ —
  `NOT_FOUND_ANSWER`, `origin=none`, `sources=[]`, строка в `qa_log`
  (`answer_given=False`; `miss_kind` проставит BH-21 как `model_refusal`).
- **Приёмка:** тест с `finish_reason="content_filter"` → 200,
  `origin=none`, одна строка в `qa_log`, один вызов модели.
- **Ловушка:** не подменять фильтрованный ответ текстом модели («Я не
  могу обсуждать…») — он без пометки и без источников, фронт покажет
  его как ответ по документам.

### BH-26. Версия модели в `qa_log` (P3)

`qa_log.llm_model` = алиас `aliceai-llm-flash`, а `/latest` Яндекс
переключает на новую версию без предупреждения (даты вывода моделей — в
`docs/ml-report.md`). Когда метрики поплывут, по журналу не понять,
сменилась ли модель. `Completion.model_version` (поле `model` из ответа
API) адаптер уже разбирает, но в журнал не пишет.

- **Предложение:** писать `model_version` — отдельным полем или как у
  эмбеддингов (`text-embeddings-v2-query@768`): `f"{model}@{version}"`.
  Одна строка в `FaqService.answer`.
- **Оговорка:** что именно Яндекс кладёт в `model` для Flash, ML не
  проверял. Если там тот же URI без версии — пункт снимается.

---

## Сводка v1

Что уже сделано у бэкенда на вечер 25.09 — в блоке «Статус у бэкенда»
выше: всё, кроме BH-13, BH-21…BH-23, BH-25, BH-26.

| Пункт | Приоритет | Срок | ML-часть |
|---|---|---|---|
| BH-1 `ChunkMatch.title/heading_path` | P0 | 28.09 | промпт готов, ждёт |
| BH-2 извлечение docx/pdf | P0 | 30.09 | рекомендация готова |
| BH-3 схема `chunks`, ингест v2 | P0 | 30.09 | `split_document` готов |
| BH-4 фоновый ингест | P1 | 30.09 | образец ограничителя в `eval/yandex.py` |
| BH-5 `/faq/search` | P0 | 01.10 | клиент и `run_eval` готовы |
| BH-6 переиндексация | P1 | 02.10 | офлайн-стенд закрывает до этого |
| BH-7 `NOT_FOUND_ANSWER`, `normalize_citations` | P0 | с BH-1 | готово |
| BH-8 температура 0 | P1 | 29.09 | замер в `ml-report.md` |
| BH-9 `RagSettings` | P0 | 29.09 | значения выше |
| BH-10 тест промпта | P0 | с BH-1 | — |
| BH-11 `qa_log` минимум | P1 | 06.10 | `PROMPT_VERSION` есть |
| BH-12, BH-14 M1, M5 | P2 | MVP | сделано у бэкенда 25.09 |
| BH-13 M2 small-to-big | P2 | после решения по золотому dev | код готов; замер 25.09: +2/37 цитат в контексте, +50 % токенов |
| BH-15 адаптер OpenAI-совместимого API (Flash) | P1 | 02.10 | образец `YandexClient._chat` |
| BH-16 модель и размерность эмбеддингов в конфиге | P1 | 02.10 | решено: v2, 768 |
| BH-17 `vector(768)` + переингест | P1 | 02.10 (лучше с BH-3) | — |
| BH-18 порог 0.51 для v2-768 | P1 | с BH-17 | предварительно; финал — A8 к 12.10 |
| BH-19 температура 0, `faq-v2.3` | P1 | с BH-15 | промпт готов |
| BH-20 `qa_log` (`vector(768)`) | P1 | 06.10 | `PROMPT_VERSION`, `mask_pii` готовы |
| BH-21 ночная задача пробелов | P1 | 06.10 | `classify_miss`, `cluster_questions`, gaps-v1 готовы |
| BH-22 таблицы кластеров | P1 | 06.10 | — |
| BH-23 `GET /api/v1/gaps` | P1 | с BH-22 | — |
| BH-24 Р1: общий ответ с пометкой, `answer_source` | P1 | 02.10 | промпт `faq-v2.4` готов; сделано (`origin`) |
| BH-25 `content_filter` — отказ, а не 502 | P2 | 02.10 | замер: 2 из 1 808 |
| BH-26 версия модели в `qa_log` | P3 | с BH-21 | — |
