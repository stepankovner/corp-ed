# Статус контракта ML ↔ бэкенд

Сверка `docs/backend-handoff.md` (ML, после PR #16–#18) с кодом на 25 сентября 2026 (ночь).
Кто что должен дальше — в конце. Правила разделения: ML пишет чистые
функции и промпты (`domain/split.py`, `context.py`, `fusion.py`,
`fulltext.py`, `query.py`, `gaps.py`, `ingest/preprocess.py`,
`prompts/`, `eval/`), бэкенд — сеть, базу, транзакции и API.

| Пункт | Статус | Где в коде | Отличия от контракта |
|---|---|---|---|
| BH-1 `ChunkMatch` с `title`, `heading_path`, `content = llm_text` | ✅ | `domain/types.py`, `repositories/chunk_repository.py` | название материала — JOIN, не копия |
| BH-2 docx/pdf → Markdown | ✅ | `ingest/extract.py`, `ingest/sandbox.py` | разбор в дочернем процессе с rlimits |
| BH-3 схема `chunks`, ингест через `split_document` | ✅ | миграция `453c802d4852`, `services/ingest_service.py` | — |
| BH-4 фоновый ингест с ограничителем | ✅ | `worker.py`, `llm/throttle.py` | очередь в Postgres, слоты квоты в Redis |
| BH-5 `POST /faq/search` | ✅ | `api/v1/endpoints/faq.py` | + `retriever` для сравнения способов поиска, + `fulltext_rank` |
| BH-6 переиндексация | ✅ | `cli reindex`, `services/reindex_service.py` | — |
| BH-7 `NOT_FOUND_ANSWER`, `normalize_citations`, порядок источников | ✅ | `services/faq_service.py` | режим «не найдено» — по BH-24 |
| BH-8 температура FAQ = 0 | ✅ | `RAG_FAQ_TEMPERATURE` | — |
| BH-9 `RagSettings` и `.env.example` | ✅ | `core/config.py` | размеры в токенах |
| BH-10 тест промпта | ✅ | `tests/test_faq_service.py` | — |
| BH-11 `qa_log` минимум | ✅ (заменён BH-20) | — | — |
| BH-12 гибридный поиск (M1) | ✅ под флагом | `RAG_RETRIEVER=vector\|hybrid`, `chunk_repository.search_fulltext` | решение ML по золотому dev (25.09): остаётся `vector` |
| BH-13 small-to-big (M2) | ⏸ после MVP | — | контракт чистовой 25.09 (таблица `sections`, `split_sections`, `select_sections`, `RAG_CONTEXT_MODE`); решение ML по золотому dev: +2/37 цитат за +50 % токенов — не встраивать до замера судьёй |
| BH-14 словарь сокращений (M5) | ✅ | `services/glossary_service.py`, `/api/v1/glossary` | расшифровки только в поиск, не в промпт |
| BH-15 адаптер OpenAI-совместимого API | ✅ | `llm/yandex_openai.py`, `llm/factory.py` | + `response_format` для строгого JSON |
| BH-16 модель и размерность в конфиге | ✅ | `LLMSettings` | `EMBEDDING_DIM` — константа схемы, проверяется на старте |
| BH-17 `vector(768)` + переингест | ✅ | миграция `97d70ebf2e07` | переингест ставится самой миграцией |
| BH-18 порог 0.51 | ✅ подтверждён на золотом dev (25.09) | `.env.example` | финал — holdout к 12.10 |
| BH-19 температура и версия промпта | ✅ | `qa_log.prompt_version` | — |
| BH-20 `qa_log` | ✅ | `domain/models.py::QaLog` | вопрос после `mask_pii`; `user_id` nullable (`SET NULL`); `best_fulltext_score` вместо `best_fulltext_rank`; + `origin`, токены, кредиты |
| BH-21 ночная задача | ✅ | `services/gap_report_service.py`, `cli gaps` | пороги полнотекста не заданы до подбора (полнотекст в классификации не участвует) |
| BH-22 таблицы кластеров | ✅ | миграция `925d32966b44` | + `embedding_model`, `prompt_version`; статус переживает пересборку |
| BH-23 `GET /api/v1/gaps` | ✅ | `api/v1/endpoints/gaps.py` | роль `ADMIN` (пивот: `MANAGER` → `ADMIN`) |
| BH-25 `content_filter` — отказ, а не 502 | ✅ | `llm/types.py::FinishReason.FILTERED`, `FaqService._filtered` | без второго вызова; `origin=none`, строка в `qa_log`, кредит за вызов списан |
| BH-26 версия модели в `qa_log` | ✅ | `qa_log.llm_model_version`, `diagnostics.model_version` | отдельное поле, не конкатенация: что кладёт Яндекс в `model` для Flash — проверить на живом ответе |
| BH-24 общий ответ с пометкой | ✅ | `services/faq_service.py` | поле `origin` (не `answer_source`), значения `documents\|general_knowledge\|none`; строгий режим — настройка компании, не переменная окружения |

## Что бэкенд ждёт от ML

| Что | Зачем | Срок по плану ML |
|---|---|---|
| Финальный `RAG_FAQ_MAX_DISTANCE` (A8 на holdout) | порог отказа; на dev остаётся 0.51 | 12.10 |
| Решение по `RAG_RETRIEVER=hybrid` | на dev остаётся `vector`; пересмотр на holdout | 12.10 |
| Пороги `GAPS_STRONG_FULLTEXT` / `GAPS_EMPTY_FULLTEXT` на `ts_rank_cd` | различать gap и retrieval_miss | по живым логам |
| Замер случая (б) Р1 (отказ модели при найденных выдержках → общий ответ) | не противоречит ли общий ответ документам | задача 2.3/2.4 |
| Решение «встраивать ли M2» после судьи на золотом dev | BH-13: таблица `sections`, `select_sections`, `RAG_CONTEXT_MODE` | после MVP |

## Что ML ждёт от бэкенда

| Что | Статус |
|---|---|
| Стенд с `/faq/search` и `/faq/ask` для `eval.run_eval` | код готов; развёртывание — `DEPLOY.md` |
| `diagnostics` в ответе `/faq/ask` для E5 (модель, токены, кредиты, расстояние) | ✅ только ADMIN |
| `fulltext_rank` в `/faq/search` для подбора порогов пробелов | ✅ |
| `source_url` в источниках ответа (коннекторы) | ✅ поле `FaqSourceResponse.source_url`, пусто у загрузок |

## Договорённости, которые нельзя молча менять

- Пометка общего ответа и `PROMPT_VERSION` — только в `prompts/faq.py`
  (ML); бэкенд ссылается на константы.
- `PAGE_BREAK` (`\f`) между страницами PDF — по нему `preprocess`
  находит колонтитулы.
- Строка полнотекстового запроса — только через `to_fulltext_query`.
- Порог отказа в гибриде — расстояние лучшего векторного кандидата, не
  скор RRF.
- `answer_given = False` у общего ответа и строгого отказа — иначе
  пробелы исчезнут из отчёта.
