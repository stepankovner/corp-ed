# Eval ML-части corp-ed

Принцип ТЗ: **сначала eval, потом улучшения.** Каждое изменение (нарезка,
промпт, порог, модель) проверяется прогоном, и в описание PR идёт строка
из `eval/results/summary.csv` до и после.

## Установка

```bash
uv sync --dev
uv pip install -r eval/requirements.txt   # numpy, стеммер, извлечение docx/pdf
```

Ключи Яндекса — в `.env` (как у бэкенда): `YC_FOLDER_ID`, `YC_API_KEY`.
Доступ к API бэкенда: `CORP_ED_BASE_URL` (по умолчанию `http://localhost:8000`)
и `CORP_ED_TOKEN` или `CORP_ED_COMPANY` + `CORP_ED_EMAIL` + `CORP_ED_PASSWORD`
(для `/faq/search` нужен админ).

Все команды — из корня репозитория, через `python -m eval.<скрипт>`.

## Наборы вопросов

### Золотой — `eval/private/golden.csv` (A5, пишется руками)

```
id,question,expected_answer,expected_material,expected_section,in_corpus,type[,level][,split]
```

- Лежит в `eval/private/` — в git не попадает (живые вопросы). Как писать
  вопросы — `docs/ml-golden-guide.md`.
- 40 вопросов: 25 по корпусу (из них ≥ 5 `negation`), 10 вне корпуса,
  5 написанных людьми (по ТЗ — из интервью Влада; на демо-корпусе черновик
  пишет ИИ, люди проверяют — `docs/ml-golden-guide.md`).
- `level` — `тема` / `деталь` (или `topic` / `detail`).
- `evidence` (необязательно) — дословная цитата с ответом: правильный чанк
  определяется по ней, а не по разделу. `author` — `llm` / `human`.
- `split` — `dev` / `holdout`, ставит `python -m eval.datasets split …`
  (50/50 по слоям). **Holdout скрипты не берут без `--split holdout`** — он
  открывается один раз, на финальном прогоне.
- Типы: `fact`, `procedure`, `negation`, `comparison`, `out_of_corpus`.
- **Соглашение:** у вопросов из интервью `id` начинается с `iv` (`iv01`…).
- `expected_material` — название документа (расширение не важно).
- `expected_section` — начало заголовка раздела: `3.1` совпадёт с
  «3.1 Продолжительность» и «3.1. Продолжительность», но не с «3.12».
- Пример — `eval/golden.example.csv`. Проверка состава:
  `python -m eval.datasets eval/private/golden.csv`.

Правильный чанк для золотого вопроса — из нужного документа и с цитатой
`evidence` (≥ 80 % пар соседних слов, как у серебряного); без цитаты — из
нужного раздела.

### Серебряный — `eval/silver.csv` (A6, генерирует LLM)

```bash
python -m eval.generate_silver --corpus путь/к/документам --n-chunks 120
```

```
id,question,material,position,heading_path,evidence,split,chunk_config
```

**Эталон — цитата `evidence`, а не позиция чанка.** Позиции меняются при
каждой перенарезке (E1–E3), цитата — нет. Правильный чанк — тот, где есть
≥ 80% пар соседних слов цитаты. `split` = `dev` / `test` (70/30, по чанку):
параметры подбирать на `dev`, подтверждать на `test`.

Фильтры генерации: цитата должна найтись в чанке и быть не короче 5 слов;
вопрос, копирующий 5+ слов подряд из чанка, выбрасывается; повторы (тот же
вопрос или почти та же цитата из того же документа) выбрасываются. После
генерации просмотреть 20 случайных вопросов глазами.

Модель — `--model yandexgpt` (Pro): на пробе 24.09 её вопросы конкретнее
и больше похожи на живые, чем у lite. 100 чанков → ~185 вопросов, ~57 тыс.
токенов.

**Перекос синтетики:** вопросы пишутся по тексту чанка и лексически к нему
близки, даже после фильтра копирования. BM25 на таком наборе выглядит
сильнее, чем на живых вопросах. Сравнивать нарезки между собой можно,
сравнивать векторный поиск с полнотекстовым — только на золотом наборе.

## Прогоны через API (официальные числа)

```bash
# Поиск: Hit@1/3/5/10, MRR с 95% CI, «воронка», расстояния
python -m eval.run_eval retrieval --dataset eval/silver.csv --config v2-400-50 --split dev

# Сквозной: F1 отказа, латентность p50/p95
python -m eval.run_eval e2e --dataset eval/private/golden.csv --config lite-k5

# После ручной разметки колонки correct (0/1/2) в CSV из e2e:
python -m eval.run_eval score --results eval/results/2026-10-02_lite-k5_e2e.csv
```

`--config` — просто имя конфигурации бэкенда: сам скрипт ничего на бэкенде
не меняет.

Разметка `correct`: 2 — по существу верно, 1 — частично, 0 — неверно.
Вопросы вне корпуса размечаются автоматически (отказ — 2, ответ — 0).
`score` проверяет планку качества mvp-plan (допущение): ≥ 80% верных
по корпусу и ≤ 20% ложных ответов вне корпуса.

«Ответил / отказал»: отказ — фиксированная фраза «В документах компании
ответа нет» **или** отказ своими словами без ссылок `[n]`. Второе считается
в `refusal_paraphrases` как нарушение формата.

## Офлайн-стенд (без бэкенда)

Режет корпус сам и ищет в памяти. BM25 работает без ключей, `vector` и
`hybrid` — с ключами Яндекса (эмбеддинги кэшируются в
`eval/.cache/embeddings.sqlite`).

```bash
python -m eval.bench --corpus docs/ --dataset eval/silver.csv --split dev \
    --chunker v2 --chunk-tokens 400 --overlap-tokens 50 --retriever vector
```

| Флаг | Эксперимент |
|---|---|
| `--chunker v1` / `v2` | E1 |
| `--no-crumbs` | E2 (крошки не идут в `embed_text`) |
| `--crumbs-without-title` | E2 (в крошках эмбеддинга только заголовки разделов, без названия документа) |
| `--chunk-tokens N --overlap-tokens M` | E3 |
| `--retriever hybrid --weights 1.0,0.5` | M1 (предпросмотр) |
| `--glossary glossary.csv` | M5 (CSV `term,expansion`) |

Эмбеддинги по умолчанию — `text-embeddings-v2` с размерностью 768 (решение
по задаче 1, 25.09). Старые: `--embedding-model text-search`.

Квота AI Studio — 10 запросов эмбеддинга в секунду на каталог. Клиент
держит 8 (`EMBEDDING_RPS` в `eval/yandex.py`), повторы после 429 тоже идут
через ограничитель. Два стенда одновременно квоту превысят: запускать
по очереди.

## Офлайн-e2e (без бэкенда)

Конвейер `/faq/ask` в памяти: векторный поиск → порог → бюджет контекста →
промпт → LLM. Для того, что на бэкенде меняется переменными
окружения: `faq_limit` (E4), модель (E5), порог (A8), режим Р1.

```bash
python -m eval.offline_e2e --corpus docs/ --dataset eval/private/golden.csv \
    --limit 5 [--not-found general]
```

По умолчанию — решение по задаче 1 (25.09): Alice AI LLM Flash
(`--api openai --model aliceai-llm-flash`), `text-embeddings-v2` 768, порог
0.51. Режим Р1 — `general` (решение 25.09): когда ответа в документах нет
(выдержки не прошли порог или модель по ним отказала), общий ответ с
пометкой «не из документов компании»; `--not-found strict` — только отказ. Старая конфигурация: `--api native --model yandexgpt-lite
--embedding-model text-search --max-distance 0.65`.

CSV совместим с `run_eval score` и `eval.judge`. Сверх колонок `run_eval
e2e` — расстояния, токены и проверки ссылок: `citations`,
`invalid_citations` (ссылка на выдержку, которой не было),
`section_citations` (номер пункта документа вида `[2.2]` вместо номера
выдержки). `latency_ms` — только вызов LLM.

**Small-to-big (M2):** `--context sections` — на место найденного чанка
встаёт его секция целиком, если влезает в бюджет, иначе окно «чанк ±
`--neighbours`» по секции, иначе сам чанк; `--context window` — то же без
секции целиком (вариант без таблицы `sections`). Колонки `context_kinds`
(из чего собран контекст), `context_tokens` и `evidence_in_context`
(дословная цитата эталона попала в контекст — только у вопросов с
`evidence`). `--dry-run` — без вызовов LLM: поиск и контекст считаются
бесплатно, F1 и ссылки — нет.

```bash
python -m eval.offline_e2e --corpus docs/ --dataset eval/private/golden.csv \
    --dry-run --context sections
```

## Сравнение двух прогонов

```bash
python -m eval.compare eval/results/A.csv eval/results/B.csv --metric rr
```

Разница B − A, её 95% доверительный интервал и p-value парного
перестановочного теста. «B лучше» — только если интервал не содержит ноль
**и** p < 0.05. Иначе — «в пределах шума».

## Порог отказа (A8)

```bash
python -m eval.run_eval retrieval --dataset eval/private/golden.csv --config v2-400-50
python -m eval.threshold eval/results/<дата>_v2-400-50_retrieval.csv
```

Два распределения расстояний (из корпуса / вне корпуса), гистограмма,
F1 отказа по порогам и 2–3 кандидата. **Финальный порог — по e2e**
(F1 отказа + правильность), прогнав кандидатов через `run_eval e2e`.

## Разовые проверки

| Скрипт | Задача |
|---|---|
| `python -m eval.probe_embedding_limit` | A1: лимит входа `text-search-doc`, молчаливая обрезка, символов на токен |
| `python -m eval.judge --results …_e2e.csv` | M4: LLM-судья; `--calibrate` — совпадение с ручной разметкой (цель ≥ 85%) |
| `python -m eval.bench_reranker` | M3: задержка bge-reranker-v2-m3 на CPU (нужен `sentence-transformers`) |

## Результаты

`eval/results/<дата>_<конфиг>_<режим>.csv` — по вопросам;
`eval/results/summary.csv` — одна строка на прогон. Оба коммитятся:
история сравнений — часть репозитория.

## Эксперименты A7 (каждый меняет одну вещь)

| # | Что | Набор | Метрика | Инструмент |
|---|---|---|---|---|
| E1 | v1 против v2 | серебряный | Hit@1, Hit@5, MRR | `bench --chunker v1/v2` → `compare` |
| E2 | крошки в `embed_text` / без | серебряный | Hit@5, MRR | `bench --no-crumbs` → `compare` |
| E3 | `chunk_tokens` ∈ {250, 400, 600} × `overlap` ∈ {0, 50} | серебряный | MRR | `bench` × 6 → `compare` |
| E4 | `faq_limit` ∈ {3, 5, 8} | золотой, e2e | F1 отказа, правильность | `run_eval e2e` × 3; «воронка» из retrieval подсказывает k |
| E5 | lite против Pro | золотой, e2e | правильность, латентность, стоимость | `run_eval e2e` × 2 + `score` |
