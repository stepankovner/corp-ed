# Статус контракта ML ↔ бэкенд

Сверка `docs/backend-handoff.md` (ML, v2.4) с кодом на 1 октября 2026;
BH-1…BH-26 — сверка 25.09.
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
| BH-13 small-to-big (M2) | ⏸ после MVP | — | контракт чистовой 25.09 (таблица `sections`, `split_sections`, `select_sections`, `RAG_CONTEXT_MODE`); решение ML по золотому dev: +2/37 цитат за +50 % токенов; судья — прирост в пределах шума при +58 % токенов → после MVP |
| BH-14 словарь сокращений (M5) | ✅ | `services/glossary_service.py`, `/api/v1/glossary` | расшифровки только в поиск, не в промпт |
| BH-15 адаптер OpenAI-совместимого API | ✅ | `llm/yandex_openai.py`, `llm/factory.py` | + `response_format` для строгого JSON |
| BH-16 модель и размерность в конфиге | ✅ | `LLMSettings` | `EMBEDDING_DIM` — константа схемы, проверяется на старте |
| BH-17 `vector(768)` + переингест | ✅ | миграция `97d70ebf2e07` | переингест ставится самой миграцией |
| BH-18 порог 0.51 | ✅ подтверждён на золотом dev (25.09); заменён BH-31 — 0,59 с 30.09 | `.env.example` | финал — holdout к 12.10 |
| BH-19 температура и версия промпта | ✅ | `qa_log.prompt_version` | — |
| BH-20 `qa_log` | ✅ | `domain/models.py::QaLog` | вопрос после `mask_pii`; `user_id` nullable (`SET NULL`); `best_fulltext_score` вместо `best_fulltext_rank`; + `origin`, токены, кредиты |
| BH-21 ночная задача | ✅ | `services/gap_report_service.py`, `cli gaps` | пороги полнотекста не заданы до подбора (полнотекст в классификации не участвует) |
| BH-22 таблицы кластеров | ✅ | миграция `925d32966b44` | + `embedding_model`, `prompt_version`; статус переживает пересборку |
| BH-23 `GET /api/v1/gaps` | ✅ | `api/v1/endpoints/gaps.py` | роль `ADMIN` (пивот: `MANAGER` → `ADMIN`) |
| BH-25 `content_filter` — отказ, а не 502 | ✅ | `llm/types.py::FinishReason.FILTERED`, `FaqService._filtered` | без второго вызова; `origin=none`, строка в `qa_log`, кредит за вызов списан |
| BH-26 версия модели в `qa_log` | ✅ | `qa_log.llm_model_version`, `diagnostics.model_version` | отдельное поле, не конкатенация: что кладёт Яндекс в `model` для Flash — проверить на живом ответе |
| BH-24 общий ответ с пометкой | ✅ | `services/faq_service.py` | поле `origin` (не `answer_source`), значения `documents\|general_knowledge\|none`; строгий режим — настройка компании, не переменная окружения |
| BH-28 память диалога | ✅ 30.09; 3 по умолчанию — 01.10 | `services/faq_service.py`, `core/dialogue_store.py`, `prompts/dialogue.py` (ML) | **реплики — в Redis, не в `qa_log`** (решение Артёма 30.09: ответ модели не хранится); окно — 12 ч от последнего вопроса (`RAG_HISTORY_TTL_MINUTES=720`), не 30 мин; индекс `qa_log` по `conversation_id` не нужен. Остальное — по контракту: `conversation_id` в `/faq/ask`, переписывание T = 0, 100 токенов, таймаут 5 с, сбой → исходный вопрос; поиск, порог, словарь, общий ответ — по переписанному; в `qa_log` — `conversation_id`, `standalone_question` (после `mask_pii`), `condense_prompt_version`, `history_turns`; отчёт о пробелах подписывает кластеры переписанным вопросом. `RAG_HISTORY_TURNS=3` по умолчанию — по замеру ML 01.10 (верных уточнений 8 → 15 из 19) |
| BH-29 общий ответ по умолчанию | ✅ 30.09 | `domain/types.py`, миграция `b29e5c1a7f30` | заведённых компаний в бою нет — переводить некого |
| BH-30 кредит = 4 000 токенов | ✅ 30.09 | `core/config.py` | — |
| BH-31 порог 0,59 | ✅ 30.09 | `.env.example` (ML), `tests/stand_harness.py` | проверка стенда читает `RAG_*` из `.env.example` — следующий порог подхватит сама |
| Р-3 `x-data-logging-enabled: false` | ✅ 30.09 | `llm/yandex_headers.py` | во всех трёх адаптерах; действие на эмбеддинги Яндекс не подтверждает (RISKS №48) |
| BH-32 реранкер за флагом | ✅ 01.10 (архитектура — 30.09), выключен | `domain/rerank.py` (ML: правило), `services/faq_service.py::_rerank`, `llm/reranker.py`, `compose.yaml` (профиль `reranker`) | модель — отдельный сервис text-embeddings-inference, а не в процессе API (контракт допускает оба; без torch в образе API, CPU и память ограничены отдельно); `RAG_RERANK_MAX_LENGTH` — только 512 (сервис режет по окну модели); `RAG_RERANK_DEPTH` ≤ 64; см. «Реранкер» ниже |
| BH-33…BH-35 приём .xlsx, .pptx, .doc | ✅ 01.10 | `ingest/extract.py` (`SourceFormat`, `detect_format`, `extract`, `error_message`), `core/config.py::IngestSettings.extra_formats`; разбор — `ingest/xlsx.py`, `pptx.py`, `doc.py` (ML) | флаг `INGEST_EXTRA_FORMATS=xlsx,pptx,doc` (по умолчанию все три; опечатка в имени — ошибка старта); выключенный формат не скачивается и из коннекторов (`supported_extensions()`). Подсказка `.doc` («сохраните как .docx или PDF») оставлена для Word 6.0/95 — его `ingest.doc` не читает. Фронт принимает все семь расширений и не знает флага: выключенный формат отклоняет сервер. Для ML: `domain/split.py::_FILE_EXTENSION` не отрезает `.xlsx` и `.pptx` от названия файла в крошках — у файлов из систем с таким названием крошки «Отчёт.xlsx > Лист» |
| BH-36 верхний индекс в .docx и HTML | ✅ 04.10 | `ingest/extract.py::_extract_docx`, `connectors/html.py` — `sup_symbol="<sup>"` | номер сноски, набранный верхним индексом, уходит, «м<sup>2</sup>» → «м²» (`preprocess` ML); ссылки на сноски Word встают на место, как раньше. **Переиндексация (BH-6) уже загруженные файлы не починит**: исходный файл не хранится (`Material`), `reindex` режет сохранённый текст — в нём `<sup>` уже снят. Файлы — загрузить заново; страницы источников обновятся со следующей правкой страницы. На стенде клиентских документов нет |
| BH-37 порог «отвечать» и «выдержки» раздельно | ✅ 04.10, выключен | `services/faq_service.py::_retrieve`, `_relevance_limit`; `core/config.py::RagSettings` (`faq_gate_distance`, `faq_near_margin`, `answer_distance`); `cli gaps` | по контракту: `RAG_FAQ_GATE_DISTANCE` пусто — поведение прежнее (тест), `RAG_FAQ_NEAR_MARGIN=0.05`; гибрид — по тому же порогу; реранкеру — порог вопроса, а без ответа по документам он не вызывается (как и раньше: пул был пуст); отчёт о пробелах делит отказ и промах по `answer_distance`. Gate ближе `RAG_FAQ_MAX_DISTANCE` — ошибка старта. Включить — `RAG_FAQ_GATE_DISTANCE=0.70` в `.env` сервера после Р-17 |
| BH-38 корпус проверки стенда 02.10 | ✅ 04.10 | `tests/fixtures/stand_quality/` (`README.md` — состав и колонки) | 17 документов, 19 граничных файлов, `golden.json` (176 случаев), генератор `gen.py`; `cases.csv` — по строке на вопрос (у диалогов `id` вида `D-001.2`), колонки контракта плюс `forbidden`, `expect_origin`, `verdict_first_run`, `origin`, `expected_rank`/`expected_distance` (место нужного документа в выдаче, первая реплика); `best_distance` — ближайший фрагмент при ответе. Итог 02.10 — 111 и 117 из 155, `PR-010` засчитан вручную (неразрывный дефис в «25‑го»). Трёх файлов на 4–26 МиБ нет — их создаёт генератор |

## Что бэкенд ждёт от ML

| Что | Зачем | Срок по плану ML |
|---|---|---|
| Реранкер: включать ли, fp32 или int8, глубина (`RAG_RERANK_DEPTH`) | BH-32; в коде за флагом, выключен | holdout 11–12.10 |
| Финальный `RAG_FAQ_MAX_DISTANCE` (A8 на holdout) | порог отказа; сейчас 0,59 (BH-31), holdout — при 0,51 и 0,59 | 12.10 |
| Решение по `RAG_RETRIEVER=hybrid` | на dev остаётся `vector`; пересмотр на holdout | 12.10 |
| Пороги `GAPS_STRONG_FULLTEXT` / `GAPS_EMPTY_FULLTEXT` на `ts_rank_cd` | различать gap и retrieval_miss | по живым логам |
| Замер случая (б) Р1 (отказ модели при найденных выдержках → общий ответ) | не противоречит ли общий ответ документам | задача 2.3/2.4 |
| Решение «встраивать ли M2» после судьи на золотом dev | BH-13: таблица `sections`, `select_sections`, `RAG_CONTEXT_MODE`; судья — прирост в пределах шума при +58 % токенов | после MVP (решение ML) |

### Поправить в документах ML (сверка бэкенда 01.10)

Документы ML правит ML; бэкенд их не трогает. Расхождения с кодом
`main` (BH-28…BH-35 влиты в `main` 01.10 из ветки
`claude/vibrant-mccarthy-jfc11y`):

- `backend-handoff.md`: BH-28…BH-35 стоят «🆕 в работу» — сделаны
  (BH-28…31 — 30.09, BH-32…35 — 01.10); «В коде пока `STRICT` — BH-29»
  — снят; ждёт только BH-36 (после слияния `ml/superscript`).
- `backend-handoff.md`, BH-28 «Хранение»: реплики не в `qa_log`, а в
  Redis (`core/dialogue_store.py`), 12 часов (`RAG_HISTORY_TTL_MINUTES`
  = 720), без индекса — пометить «сделано иначе», как у BH-24.
- `backend-handoff.md`, BH-32 «Модель»: не `CrossEncoder` в процессе
  API, а отдельный сервис text-embeddings-inference 1.9.4 (профиль
  `reranker`, `deploy/reranker/fetch-model.sh`); `HF_HUB_OFFLINE` не
  используется.
- `backend-handoff.md`, BH-33…35: умолчание `INGEST_EXTRA_FORMATS` —
  `xlsx,pptx,doc`, не `xlsx`; константы `SUPPORTED_EXTENSIONS` больше
  нет — функция `supported_extensions()` (учитывает флаг); совет «.doc»
  оставлен намеренно — для Word 6.0/95.
- `backend-handoff.md`: «`tests/stand_harness.py` держит 0,51» —
  исправлено (читает `RAG_*` из `.env.example`); перевод заведённых
  компаний на общий ответ (BH-29) не нужен — боевых компаний нет.
- `ml-backend-contracts.md`: «скрипта переиндексации нет» — есть,
  `python -m corp_ed.cli reindex`.
- `ml-code-guide.md`: память диалога и реранкер — в продукте (3 пары;
  реранкер за флагом, выключен), порог — 0,59, решён 30.09.
- `ml-summary.md`, `ml-formats.md`: «ждут бэкенда (BH-28, BH-29,
  BH-33…35)», «сейчас в коде отказ» — сделано; реранкер — решено 01.10.
- `ml-report.md`: настройки `RAG_NOT_FOUND_MODE` нет — режим в поле
  компании `tenants.not_found_mode`, меняется `cli set-not-found-mode`.
- `ml-plan.md`: файла `prompts/program.py` нет (удалён пивотом 25.09).
- `backend-handoff.md`, раздел 0, п. 2 и BH-36 «Переиндексация»
  (сверка 04.10): `reindex` не читает файл заново — исходники не
  хранятся, переиндексация режет сохранённый текст. Правки `preprocess` и
  нарезки она применит (сноски .docx — #45, крошки — #64), правки
  извлечения — нет (`ingest/doc.py`, `xlsx.py`, `pptx.py`, верхний индекс
  BH-36): таким файлам нужна повторная загрузка.
- `backend-handoff.md` v3.0 (PR ML #72), BH-39 «Переиндексация всех
  PDF»: по той же причине переиндексация PDF заново не разберёт —
  сохранён текст pymupdf4llm. PDF, загруженные до BH-39, нужно загрузить
  заново; клиентов нет, на стенде — тестовые данные. «Проверка ML после
  встраивания» (загрузка 4 PDF через API) это и делает — ей ничего не
  мешает.
- `backend-handoff.md`, раздел 0, п. 2 (переиндексация всех компаний
  после BH-36): **решение владельца 04.10 — отложить до BH-39** и
  сделать один раз вместе с ним. На стенде только тестовые данные, а
  после BH-39 PDF всё равно загружать заново.
- `eval/api_client.py` (раздел 0, п. 5 и проверка после BH-39): вход
  устарел — `/auth/login` с `company_code` одним шагом. С этапов 2–4
  код компании во входе не нужен, а лишнее поле — 422; дальше —
  второй фактор, у администратора обязательно приложение. Нужно:
  `/auth/login` (`email`, `password`) → `status: mfa_required` →
  `/auth/mfa/verify` (`token`, `method: totp`, `code` из
  `CORP_ED_TOTP_SECRET`); тот же код второй раз сервер не примет. Готовый
  `CORP_ED_TOKEN` живёт 15 минут — на прогон не хватит. Образец —
  `corp_ed/stand.py::StandClient.login`. Секрет приложения — в `.env` ML,
  не в чат.

## Что ML ждёт от бэкенда

| Что | Статус |
|---|---|
| Стенд с `/faq/search` и `/faq/ask` для `eval.run_eval` | код и инструкция готовы (`STAGE.md`); сервер Selectel — команда |
| `diagnostics` в ответе `/faq/ask` для E5 (модель, токены, кредиты, расстояние) | ✅ только ADMIN |
| `fulltext_rank` в `/faq/search` для подбора порогов пробелов | ✅ |
| `source_url` в источниках ответа (коннекторы) | ✅ поле `FaqSourceResponse.source_url`, пусто у загрузок |

## Реранкер (BH-32): как включить и мерить

В MVP за флагом, по умолчанию выключен (решение Артёма 01.10); включение —
по итогам holdout 11–12.10.

- **Где в пайплайне.** С реранкером вектор берёт `RAG_RERANK_DEPTH`
  кандидатов (30) с теми же фильтрами компании и прав сотрудника.
  Перестановка — функцией ML `domain.rerank.rerank`, той же, что на
  стенде (`eval/rerank.py::rerank_candidates`): модель оценивает только
  прошедших порог `RAG_FAQ_MAX_DISTANCE`, они идут первыми по баллу; в
  промпт — первые `RAG_FAQ_LIMIT` из них. Отвечать или нет — по-прежнему
  по лучшему векторному расстоянию. Пара — вопрос, ушедший в поиск
  (после переписывания и словаря), и `embed_text` фрагмента. Меньше двух
  прошедших — модель не зовём.
- **Сервис.** HuggingFace text-embeddings-inference 1.9.4 (профиль
  `reranker` в `compose.yaml`), контракт `POST /rerank`, пачка до 64 пар.
  Модель — `deploy/reranker/fetch-model.sh fp32|int8`: закреплённая
  ревизия `cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`, ONNX, sha256
  каждого файла. Пары длиннее окна модели (512 токенов, как
  `max_length=512` у ML) сервис обрезает сам. Замер 30.09 на 4 ядрах,
  30 фрагментов по ~1 200 символов: fp32 — ~1,9 с, баллы совпадают с
  fp32-моделью; int8 — ~1,2 с, баллы немного другие — нужен замер ML.
- **Сбой, таймаут (`RAG_RERANK_TIMEOUT_MS=3000`) или кривой ответ** —
  порядок вектора, без ошибки; событие `faq_rerank_failed` и метрика
  `corp_ed_faq_degraded_total{reason="rerank"}`. В `qa_log` —
  `rerank_model` (пусто — порядок вектора) и `rerank_ms`: ответы с
  реранкером и без можно сравнить по 👍/👎, пробелам и времени.
- **Замер на стенде.** `POST /faq/search` с `"rerank": true` — порядок,
  который дал бы ответ: прошедшие порог по `rerank_score`, за ними
  остальные (409 — выключен или поиск не векторный, 503 — не ответил);
  в диагностике `/faq/ask` (ADMIN) — `rerank_model`, `rerank_ms`.
  Включить на стенде: `deploy/reranker/fetch-model.sh`, в `.env` —
  `COMPOSE_PROFILES=reranker` и
  `RAG_RERANK_MODEL=cross-encoder/mmarco-mMiniLMv2-L12-H384-v1`.

## Память диалога: как мерить на стенде

`/faq/ask` принимает `conversation_id` и всегда возвращает его. ADMIN
видит в `diagnostics` `standalone_question` (как понят вопрос) и
`history_turns`. Клиенту eval достаточно передавать `conversation_id` из
прошлого ответа в пределах диалога. `RAG_HISTORY_TURNS=3` — по
умолчанию с 01.10 (замер ML).

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
