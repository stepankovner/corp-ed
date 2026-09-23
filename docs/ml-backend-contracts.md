# ML ↔ бэкенд: контракты и что встроить

**От:** Артём (ML) · **Кому:** бэкенд · **Дата:** 23–24.09.2026
**Основа:** `ml-spec.md` (ТЗ ML-части). Здесь — конкретика по готовому коду:
какие функции вызывать, в каком порядке и что нужно от бэкенда.

Все функции ML — чистые: без БД, сети и файлов. Бэкенд вызывает их из своих
сервисов. Бэкенд-код в ML-ветках не менялся.

Код приходит четырьмя PR-стеком, вливать по порядку:

| Ветка | Разделы документа |
|---|---|
| `ml/preprocess-split` | 1 (ингест: `preprocess`, `split_document`), 7 |
| `ml/retrieval-utils` | 4 (M1: `rrf_merge`, `to_fulltext_query`), 6 (M5) |
| `ml/eval` | 8 (эксперименты, офлайн-стенд) |
| `ml/prompt-v2` | 2 (ответ: промпт v2.1, `select_context`, `normalize_citations`), 3 (клиент `run_eval` для `/faq/search`), 5 (M2) — **только вместе с задачей 1.5** |

---

## 1. Ингест (фон, при загрузке файла)

```python
from corp_ed.ingest.preprocess import preprocess
from corp_ed.domain.split import split_document

markdown = extract(file)                      # бэкенд, см. 1.1
clean = preprocess(markdown)                  # ML
drafts = split_document(
    clean,
    title=material.title,                     # имя документа; расширение файла отрежется само
    chunk_tokens=rag.chunk_tokens,            # 400
    overlap_tokens=rag.overlap_tokens,        # 50
)
for draft in drafts:
    embedding = await embeddings.embed_document(draft.embed_text)   # text-search-doc
    Chunk(
        material_id=material.id,
        position=draft.position,
        heading_path=draft.heading_path,      # list[str], без названия документа
        content=draft.llm_text,               # в промпт LLM
        embedding=embedding.embedding,        # посчитан по embed_text, НЕ по llm_text
        ...
    )
```

`ChunkDraft`:

| Поле | Что это | Куда |
|---|---|---|
| `position` | сквозной номер с 0 | `chunks.position` |
| `heading_path` | `["Раздел 3", "3.2 Перенос отпуска"]` | новая колонка `chunks.heading_path` (`text[]` или `jsonb`) |
| `embed_text` | крошки + текст без разметки | **только** в эмбеддинг (и в `tsvector` на MVP, см. 4) |
| `llm_text` | крошки + Markdown | в `chunks.content` (или новая колонка `llm_text`) |

Старую `split_into_chunks` не трогал: она работает, пока вы не переключитесь.

**Замеры эмбеддера 24.09 (A1), важные для ингеста:**
- Лимит `text-search-doc` — 2048 токенов. При превышении — **ошибка 400**,
  а не обрезка. Чанки v2 при 400/50 — до ~350 реальных токенов, запас
  большой. Самый длинный чанк v1 на реальных документах — ~1200 токенов.
- **Квота — 10 запросов эмбеддинга в секунду на каталог.** Параллельный
  ингест без ограничителя сразу получает 429, а повторы «все разом» снова
  упираются в квоту. Нужен общий ограничитель частоты (слоты), через
  который идут и повторы. В eval сделано так: `RateLimiter` в
  `eval/yandex.py`, 8 запросов в секунду. 1000 чанков ≈ 2 минуты. Квота
  общая с поиском (эмбеддинг вопроса): массовый ингест в рабочее время
  замедлит ответы.
- `material.title` уходит в крошки. **Название должно быть человеческим**
  («Правила отбора в акселератор»), а не именем файла
  («Pravila-otbora_Akselerator-GPB-…»). Как крошки влияют на поиск —
  см. `docs/ml-report.md` (E2).

### 1.1. Извлечение docx/pdf → Markdown (ваша часть, выбор по A2)

Проверено на 6 реальных документах (публичные регламенты, docx с таблицами):

| Формат | Брать | Почему |
|---|---|---|
| docx | `mammoth.convert_to_html` → `markdownify.markdownify(html, heading_style="ATX")` | заголовки → `#`, таблицы → таблицы, списки → списки. `markitdown` для docx даёт то же самое (он сам использует mammoth) |
| pdf | `pymupdf4llm.to_markdown(path, page_chunks=True)` | находит заголовки и таблицы. `markitdown` для PDF — **ноль заголовков** |
| txt, md | как есть | — |

**Договорённость про PDF:** страницы склеивать символом `\f`
(`corp_ed.ingest.preprocess.PAGE_BREAK`):

```python
pages = pymupdf4llm.to_markdown(str(path), page_chunks=True)
markdown = PAGE_BREAK.join(page["text"] for page in pages)
```

По границам страниц `preprocess` находит колонтитулы (строки, повторяющиеся
у края большинства страниц). Без `\f` удаляются только голые номера страниц.

Библиотеки извлечения добавляете в зависимости вы, вместе с кодом извлечения.
`razdel` (нужен нарезке) уже добавлен в `pyproject.toml` и `uv.lock`.

---

## 2. Ответ (онлайн)

```python
from corp_ed.domain.context import select_context
from corp_ed.prompts.faq import (
    NOT_FOUND_ANSWER,
    PROMPT_VERSION,
    build_faq_messages,
    build_general_messages,
    ensure_general_prefix,
    is_not_found,
    normalize_citations,
)

matches = await chunk_repo.search(embedding, limit=rag.faq_limit)  # top-K
relevant = [m for m in matches if m.distance <= rag.faq_max_distance]
context = select_context(relevant, max_tokens=rag.context_max_tokens)  # 3000

if not context:
    # Р1 = «строгий отказ»:
    return FaqAnswer(content=NOT_FOUND_ANSWER, answer_given=False, sources=[])
    # Р1 = «общий ответ с пометкой»:
    #   completion = await llm.generate(build_general_messages(question))
    #   return FaqAnswer(content=ensure_general_prefix(completion.content),
    #                    answer_given=False, sources=[])

completion = await llm.generate(build_faq_messages(question, context))
content = normalize_citations(completion.content, context)  # [4.2] → [k]
answer_given = not is_not_found(content)
return FaqAnswer(
    content=content,
    answer_given=answer_given,
    sources=context if answer_given else [],  # порядок = номера [1], [2] в ответе
)
```

Важно:
- **Порядок источников** в ответе API должен совпадать с порядком выдержек в
  промпте: модель ссылается на них номерами `[1]`, `[2]`.
- Константу `NO_ANSWER_TEXT` в `faq_service.py` заменить на `NOT_FOUND_ANSWER`
  из `prompts/faq.py`: фраза отказа одна на всю систему, фронт распознаёт её
  по началу ответа.
- Модель может отказать и при найденных выдержках — поэтому
  `answer_given = not is_not_found(...)`.
- `normalize_citations` — страховка ссылок. Даже с правилом в промпте lite
  иногда ставит в скобки номер пункта документа (`[4.2]`), а не номер
  выдержки (прогон 24.09: 1 ответ из 20). Функция заменяет такую ссылку
  на номер выдержки, где этот пункт начинает строку, а если выдержка не
  одна — на текст «(п. 4.2)». Фронт тогда не получает битых ссылок.

### 2.1. Что нужно от `ChunkMatch` (задача 1.5)

Промпт принимает любой объект с тремя полями (Protocol `SourceChunk`):

| Поле | Тип | Что |
|---|---|---|
| `content` | `str` | `llm_text` чанка (крошки + Markdown) |
| `title` | `str` | название документа |
| `heading_path` | `Sequence[str]` | путь разделов |

`select_context` нужен только `content`.

⚠️ **PR с промптом v2 вливать вместе с 1.5.** Сейчас `faq_service.py:61`
передаёт `ChunkMatch` без `title` и `heading_path`: mypy выдаёт одну ошибку
`arg-type`, в рантайме будет `AttributeError`. И поправить бэкенд-тест
`tests/test_faq_service.py::test_found_chunks_reach_the_prompt`: он ищет в
промпте строку `"Выдержка 1"`, а в v2 выдержка обозначается `"[1] …"`.

### 2.2. Логи (qa_log, задача 1.6)

Рядом с ответом писать: `PROMPT_VERSION` (сейчас `faq-v2.1`), модель,
расстояние ближайшего чанка, `answer_given`. Без версии промпта нельзя
привязать 👍/👎 и результаты eval к изменениям промпта.

---

## 3. Для eval: `POST /api/v1/faq/search` (до 1.10)

Отладочный эндпоинт, только для админа: top-K чанков с расстояниями, без LLM.
Клиент eval (`eval/api_client.py`) терпим к именам полей, но предлагаю так:

```http
POST /api/v1/faq/search
{"question": "Сколько дней отпуска?", "limit": 10}
```
```json
{
  "matches": [
    {
      "chunk_id": "4c7d…",
      "material_id": "a1b2…",
      "material_title": "Положение об отпусках.docx",
      "position": 12,
      "heading_path": ["Раздел 3", "3.1 Продолжительность"],
      "content": "Положение об отпусках > Раздел 3 > 3.1 Продолжительность\n…",
      "distance": 0.52
    }
  ]
}
```

Поиск — тот же, что в `/faq/ask`, **без** порога `max_distance`: для подбора
порога (A8) нужны расстояния и у тех вопросов, которые порог не прошли.
Фильтр по тенанту обязателен.

Для E5 (стоимость lite против Pro) было бы полезно, чтобы `/faq/ask` для админа
возвращал ещё `usage` (`input_tokens`, `output_tokens`) и `model`. Клиент eval
сохранит любые дополнительные поля ответа в колонку `extra`.

---

## 4. MVP · M1 — гибридный поиск

1. Колонка `tsvector`, генерируемая из **`embed_text`** (без разметки), + GIN:
   ```sql
   ALTER TABLE chunks ADD COLUMN embed_text text;  -- если ещё не храните
   ALTER TABLE chunks ADD COLUMN fts tsvector
     GENERATED ALWAYS AS (to_tsvector('russian', embed_text)) STORED;
   CREATE INDEX chunks_fts_idx ON chunks USING gin (fts);
   ```
2. Метод `search_fulltext(question, limit)` — **с фильтром по тенанту**:
   ```sql
   SELECT id, ts_rank_cd(fts, q) AS rank
   FROM chunks, websearch_to_tsquery('russian', :q) AS q
   WHERE tenant_id = :tenant_id AND fts @@ q
   ORDER BY rank DESC
   LIMIT :limit
   ```
   где `:q = to_fulltext_query(question)` из `corp_ed.domain.fulltext`.

   **Почему не передавать вопрос как есть.** `websearch_to_tsquery` соединяет
   слова вне кавычек через `&` (И). На вопросе «Сколько дней отпуска положено
   сотруднику?» чанк найдётся, только если в нём есть все значимые слова, и на
   живых вопросах ветка почти всегда пустая. `to_fulltext_query` превращает
   вопрос в `Сколько or дней or отпуска or положено or сотруднику`: `or` эта
   функция понимает как `|`, стоп-слова PostgreSQL выкинет сам. Заодно из вопроса
   убираются `-` (оператор НЕ) и кавычки (фразовый поиск). Пустая строка — ветку
   не вызывать.
3. Слияние: `rrf_merge([vector_ids, fulltext_ids], weights=[1.0, 0.5], k=60)`
   из `corp_ed.domain.fusion`.
4. **Порог отказа остаётся на расстоянии лучшего ВЕКТОРНОГО кандидата**, а не
   на скоре RRF. Скор RRF зависит только от рангов: лучший кандидат получает
   ~1/61, даже если вся выдача — мусор (у МТС скоры «схлопывались»). Если
   вектор не прошёл порог, а полнотекст нашёл точное совпадение (код формы,
   номер пункта), — отдельное решение по eval в рамках M1.

## 5. MVP · M2 — small-to-big

- `split_sections(...)` возвращает `SectionDraft(position, heading_path, llm_text,
  chunks)`: секция целиком (`llm_text`) + её дочерние чанки.
  `split_document` — то же, развёрнутое в плоский список.
- Таблица `sections(id, material_id, position, heading_path, content_md,
  tenant_id)` и `chunks.section_id`.
- В ответе: `select_sections(matches, sections={section_id: content_md},
  max_tokens=3000)` из `corp_ed.domain.context`. На вход — **финальный** порядок
  (после RRF/реранкера). Возвращает блоки, совместимые с `build_faq_messages`.
- Контракт черновой — согласовать на неделе 5.

## 6. MVP · M5 — словарь сокращений

Таблица `glossary(term, expansion, tenant_id)`. Перед эмбеддингом и
полнотекстом: `question = expand_query(question, glossary)` из
`corp_ed.domain.query`. Пример: «Как оформить ДМС?» →
«Как оформить ДМС? (ДМС — добровольное медицинское страхование)».

---

## 7. Параметры (`RagSettings` и `.env.example`)

Поля в `RagSettings` добавляете вы, значения — мои. Предлагаемый блок:

```dotenv
# RAG — значения за ML (Артём). Размеры в ТОКЕНАХ.
RAG_CHUNK_TOKENS=400            # тело чанка, включая перекрытие; крошки сверху
RAG_OVERLAP_TOKENS=50           # кандидат после E3 на демо — 600/50, финал на золотом
RAG_FAQ_LIMIT=5                 # top-K; уточнится в E4
RAG_FAQ_MAX_DISTANCE=0.6        # ВРЕМЕННО; демо 24.09 — кандидат 0.65; финал — A8, к 12.10
RAG_CONTEXT_MAX_TOKENS=3000     # бюджет выдержек (досье 8.2); при 600/50 — 3300
RAG_LLM_TEMPERATURE=0           # сейчас в коде 0.3; см. ниже
# MVP (M1):
# RAG_RRF_K=60
# RAG_RRF_VECTOR_WEIGHT=1.0
# RAG_RRF_FULLTEXT_WEIGHT=0.5
```

**Температура.** Сейчас `llm/yandex.py` по умолчанию берёт 0.3. Замер
24.09: 3 прогона lite на одних и тех же выдержках. При 0.3 одинаковых
ответов 15 из 24, и один вопрос переключался между «ответил» и «отказал».
При 0 — 24 из 24, правильность не хуже. Ответы по регламентам должны быть
одинаковыми для одного вопроса, поэтому для FAQ рекомендую 0: отдельный
параметр или аргумент в вызове из `faq_service`.

**Числа токенов — в единицах `count_tokens` (`len/3`).** Реально это
в ~1.5 раза меньше: 400 ≈ 245 реальных токенов, 3000 ≈ 1900 (A1).

Валидация: `0 <= overlap_tokens < chunk_tokens` (`split_document` сам кидает
`ValueError`, но лучше падать на старте).

## 8. Процедура экспериментов (A7)

После каждого изменения нарезки нужен переингест корпуса тенанта — скрипт
«переиндексировать всё» (задача бэкенда, 1 блок, до 2.10). Пока его нет,
нарезки сравниваются на офлайн-стенде (`eval/bench.py`), официальные числа —
после переингеста через `eval/run_eval.py`.

## 9. Сводка: что от бэкенда

| Что | Срок по ТЗ | Статус ML-части |
|---|---|---|
| `ChunkMatch` с `title`, `heading_path`; `content` = `llm_text` | 28.09 | промпт готов, ждёт |
| Извлечение docx/pdf, PDF через `\f` | 30.09 | рекомендация выше |
| Схема `chunks`: `heading_path`, эмбеддинг из `embed_text` | 30.09 | `split_document` готов |
| `POST /faq/search` | 1.10 | клиент и `run_eval` готовы |
| Переиндексация корпуса | 2.10 | офлайн-стенд закрывает до этого |
| `tsvector` + `search_fulltext` | MVP, нед. 4 | `to_fulltext_query`, `rrf_merge` готовы |
| Таблица `sections` | MVP, нед. 5 | `split_sections`, `select_sections` готовы (черновик) |
| `glossary` | MVP | `expand_query` готов |

Добавилось 24.09 после первых замеров на Яндексе:

| Что | Почему | Статус ML-части |
|---|---|---|
| `normalize_citations` на ответе модели | lite иногда пишет `[4.2]` вместо номера выдержки | функция и тесты готовы |
| Температура 0 для `/faq/ask` | при 0.3 ответы на один вопрос разные | замер в `docs/ml-report.md` |
| Ограничитель частоты эмбеддингов при ингесте | квота 10 запросов в секунду, 429 | образец — `RateLimiter` в `eval/yandex.py` |
| Человеческое `material.title` | название уходит в крошки эмбеддинга | — |
