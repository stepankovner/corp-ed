# Мониторинг ML-части продукта (22.10.2026; сверка с кодом — 24.10)

Запрос Артёма 22.10: «хочется сделать подробный наглядный мониторинг всех
систем с первого дня — это залог безотказной работы и возможности
улучшаться на данных клиента». Здесь — **ML-часть**: шаг решения
(BH-46…BH-48, схема «решение до ответа»), поставщики моделей, качество
ответов и данные для улучшений. Стек уже стоит у бэкенда
(`deploy/monitoring`: Prometheus, Alertmanager → почта и Telegram,
Grafana, Loki, Alloy, blackbox, node-exporter; решения команды —
`MONITORING-RESEARCH.md` §11). Этот документ — **предложение ML**:
имена метрик, тревоги, панели, ночной прогон, выборки. Встраивает
бэкенд — пункт **BH-49** в `backend-handoff.md`; готовые строки
дашборда — `docs/ml-monitoring-dashboard.json`.

Слои по скорости реакции:

| Слой | Вопрос | Чем | Реакция |
|---|---|---|---|
| 1. Работает ли | отвечает ли шаг решения, живы ли оба звена | метрики `corp_ed_decision_*` | секунды–минуты |
| 2. Тревоги | кого и когда будить | правила Prometheus → Alertmanager | минуты |
| 3. Наглядно | что происходит сейчас и за сутки | 3 строки дашборда `kronto.json` | — |
| 4. Хорошо ли отвечает | не поехало ли качество | ночной прогон, суточные агрегаты, теневой режим | часы–дни |
| 5. Улучшаться на данных | из чего делать новые наборы и правки | выборки из `qa_log` | недели |

Итого: **19 метрик** (17 в API, 2 — от прогона через textfile; две
метрики предохранителя решения — «не на старте», вне счёта), **14 правил
тревог**, **3 строки дашборда, 27 панелей** (30 объектов JSON вместе со
строками).

**24.10 — сверка с кодом бэкенда** (PR #151–#161, `main` 6162b0a).
Документ приведён к тому, что встроено: дублёр решения — Qwen у Яндекса
(`provider="yandex"`, `effort="none"`), MWS не подключён; предохранителя
решения нет; проба Cloud.ru — раз в 15 минут; промпт `decision-v7`;
правила — как в `deploy/monitoring/prometheus/rules/ml.yml`; текст ответа
тени не хранится (4.3, вариант (б)). Что бэкенду ещё поправить, помечено
«просьба 24.10»: исход `cancelled` у звена, проигравшего дубль, шаг
`answer`, метрика сбоев тени, `DecisionAllLinksDown`, ошибки разбора — не
`error_config`.

---

## 0. Правила, которые не нарушаем

1. **Метки — только фиксированные перечни** (докстринг `core/metrics.py`):
   ни текста вопроса, ни запроса добора, ни роли из справочника, ни id
   компании или пользователя, ни текста ошибки — только **класс** ошибки.
   Каждый перечень ниже выписан явно; тест бэкенда сверяет с ним.
   Причины — 152-ФЗ и число рядов.
2. **По компаниям — не в Prometheus.** Суточные числа по компании идут в
   таблицу агрегатов (слой 4, П-1), а не в метку: код компании в метрике
   допускает `MONITORING-RESEARCH.md` §2.2, но (а) Prometheus с метками
   компаний — это объёмы и рост клиентов в системе, которую видит вся
   команда, (б) число рядов растёт с числом клиентов. Метка `mode` (off /
   shadow / on) — без компании.
3. **Все комбинации меток создаются при старте** (`.labels(...)` для
   каждого значения перечня): иначе `increase()` не видит первое
   срабатывание нового ряда, и тревога «> 0» молчит на первой ошибке
   настройки — ровно там, где она нужнее всего.
4. **Режим `shadow` не будит.** В тени сотрудник получает ответ конвейера;
   тревоги про время и откаты схемы — `warning` только при `mode="on"`.
   Ошибки настройки и «нет денег» — тревога в любом режиме: в `on` они
   станут простоем.
5. **Один процесс uvicorn** (`MONITORING-RESEARCH.md` §8 п. 1): `Info` и
   `Gauge` работают как есть. При переходе на `--workers` — multiprocess,
   там нет `Info`: заменить на `Gauge` = 1 с теми же метками.

Оценка числа рядов: ≈ 900 (из них ≈ 700 — корзины гистограмм) к ≈ 2 000
по оценке `MONITORING-RESEARCH.md` §10.

---

## 1. Работает ли (секунды): метрики

Имена — в стиле `corp_ed_*`, значения меток — те же, что в колонках
журнала `decision_*` (handoff v3.18, BH-46 «Журнал»): тогда стенд,
журнал и метрики сравниваются без словаря. «Где писать» — шаги BH-46
(«Порядок» пп. 0–6 и подпункты «Откат», «Бюджет времени шага решения и
дублирующий запрос», «Теневой режим»).

### 1.1. Перечни меток

| Метка | Значения |
|---|---|
| `mode` | `off`, `shadow`, `on` — режим компании на момент вопроса (`decision_mode`) |
| `route` | `answer`, `directory`, `search`, `not_found`, `other_subject`, `small_talk` (`Route.value`, `decision_route`), `rule` (реплика правилом BH-48), `fallback` (откат: решения нет) |
| `provider` | `cloudru`, `yandex` — дублёр Qwen (`decision_provider`); `mws` код создаёт при старте, но MWS не подключён (замеренный резерв) — нулевые ряды, в панелях отсечены `provider!="mws"` |
| `effort` | `low`, `medium`, `none` — у Qwen всегда (`decision_effort`) |
| `call` | `first`, `recheck`, `after_search` (как в `decision_timeout`) |
| `outcome` вызова | `ok`; `invalid` — ответ не прошёл схему (`parse_decision` → None); `timeout`; `error_fast` — 5xx, 429, обрыв связи, ответ не по контракту (не JSON); `error_config` — 400, 401, 403, 404, только по HTTP-коду (просьба 24.10: сейчас ошибка разбора тоже даёт `error_config` и critical-тревогу); `error_payment` — Cloud.ru 402 (нет денег). Просьба 24.10: `cancelled` — звено, проигравшее дубль (сейчас не считается вовсе). Коды — BH-46 «Бюджет…» п. 4 |
| `reason` отката | `timeout` — бюджет 6 с истёк (и при зависании звеньев, и когда бюджет съели живые вызовы); `invalid`; `provider_error` — оба звена упали быстрой ошибкой |
| `reason` перехода к дублёру | `hedge` — Cloud.ru не ответил за задержку (`low` 3 с); сразу — `error_fast`, `error_config`, `error_payment`, `invalid` (Cloud.ru ответил не по схеме) |
| `stage` | `search`, `decision`, `recheck`, `directory`, `extra_search`, `answer`, `total` (поля `search_ms` … `answer_ms`, `latency_ms`) |
| `outcome` спекулятивного ответа | `used`, `wasted`, `skipped` |
| `reason` спекулятивного ответа | `none` (при `used`); при `wasted` — `small_talk`, `other_subject`, `not_found`, `shown_changed` (добор или справочник), `other_edition`; при `skipped` — `gate`, `contact` (`contact` не сделан — ряд нулевой). В журнале (`decision_wasted_reason`) — имена стенда: `refusal`, `directory`, `search`, `edition`, `small_talk` (handoff BH-46, «Журнал») |
| `outcome` справочника | `hit`, `partial`, `miss`, `absent` (у компании справочника нет; в коде не пишется — без справочника цикл не идёт в `DIRECTORY`) |
| `result` перепроверки | причина × исход: `refusal_confirmed`, `refusal_flipped`, `role_confirmed`, `role_flipped` (причина — `decision_recheck_reason`: `refusal` — отказ low, `role` — R2; `flipped` — medium изменил маршрут) |
| `result` добора | `new`, `empty`, `repeated`, `limit` |
| `result` тени | `agree_answer`, `agree_refusal`, `scheme_refused` (схема отказала, конвейер ответил), `pipeline_refused` (наоборот) |
| `vote` | `up`, `down` |
| `feedback_reason` | `inaccurate`, `incomplete`, `outdated`, `wrong_source`, `other`, `none` (`chat_service.FEEDBACK_REASONS`) |
| `direction` | `input`, `output` |

### 1.2. Метрики шага решения

| # | Имя | Тип | Метки | Где писать | Зачем |
|---|---|---|---|---|---|
| 1 | `corp_ed_faq_decision_mode_total` | Counter | `mode` | на каждый вопрос в начале `faq_service`, и при `off` | сколько трафика идёт через схему; без компании |
| 2 | `corp_ed_decision_questions_total` | Counter | `mode` (`shadow`, `on`), `route` | по окончании цикла схемы — п. 3 (итоговый маршрут); `rule` — п. 0; `fallback` — «Откат» | распределение маршрутов на живом трафике (стенд: без добора 174 из 196) |
| 3 | `corp_ed_decision_calls_total` | Counter | `provider`, `effort`, `call`, `outcome` | каждый вызов модели решения — п. 2, «Перепроверка», п. 4; звено, отменённое после дубля, не считается (просьба 24.10 — `cancelled`) | доля ошибок и таймаутов по звену; класс ошибки |
| 4 | `corp_ed_decision_call_duration_seconds` | Histogram, корзины 0,25 / 0,5 / 1 / 1,5 / 2 / 3 / 4 / 5 / 7 / 10 / 15 | `provider`, `effort` | там же; при таймауте — остаток бюджета; отменённое звено не наблюдается (просьба 24.10 — время до отмены: без него p95 Cloud.ru обрезан на ≈ 3,7 с, и зависание не видно) | p95 low и medium по звеньям; проба 20.10: Cloud.ru low p95 1,5–1,8 с, medium 3,5–4,8 с |
| 5 | `corp_ed_decision_fallback_total` | Counter | `mode`, `reason` (отката) | «Откат» и «Бюджет…» BH-46: маршрут `ANSWER` с `decision=None` | главная тревога «схема не работает» |
| 6 | `corp_ed_decision_failover_total` | Counter | `reason` (перехода) | «Бюджет…» пп. 2–4: вызов ушёл к дублёру (по задержке или сразу) | как часто основное звено подводит; дубль ожидается на 5–15 % вызовов `low`; кто выиграл — пока только в журнале (`decision[].winner`) |
| 7 | `corp_ed_decision_breaker_state` | Gauge | `provider` | **не на старте** (BH-46 «Бюджет…» п. 6): в коде нет, вне счёта | — |
| 8 | `corp_ed_decision_breaker_opened_total` | Counter | `provider` | то же — в коде нет, вне счёта | — |
| 9 | `corp_ed_decision_probe_total` | Counter | `provider`, `outcome` (как у вызова) | «Бюджет…» п. 5: проба шага решения раз в 15 мин (`llm/probes.py`, `RESERVE_EVERY = 900`), только `cloudru` и только при ключе Cloud.ru; Qwen не пробуется — его видно по дублям | основное звено не умерло тихо; ночью — единственный сигнал |
| 10 | `corp_ed_decision_probe_duration_seconds` | Histogram, корзины как у № 4 | `provider` | там же | время звена без живого трафика (ночью тоже) |
| 11 | `corp_ed_decision_probe_consecutive_failures` | Gauge | `provider` | там же: неудача +1, успех → 0 | тревога «две неудачи подряд» без хитрого PromQL |
| 12 | `corp_ed_decision_stage_duration_seconds` | Histogram, корзины 0,05 / 0,1 / 0,25 / 0,5 / 1 / 2 / 3 / 4 / 5 / 7 / 9 / 12 / 15 / 20 / 30 | `mode`, `stage` | один раз на вопрос по окончании цикла — суммы веток (`search_ms`, `decision_ms`, `recheck_ms`, `directory_ms`, `extra_search_ms`, `answer_ms`), `total` = `latency_ms`; нулевые ветки не наблюдать. 24.10: `answer` создан, но не пишется (`answer_ms` нет — просьба); в тени `total` = поиск + фон схемы с ожиданием конвейера, не равно времени `on` | где время: приёмка 4 — p50 4,2 / p95 11,6 с; критерий Артёма p50 ≤ 5 с, p95 ≤ 12 с (`ml-agent-plan.md` §7.4 п. 1) |
| 13 | `corp_ed_decision_question_duration_seconds` | Histogram, корзины как у № 12 | `mode`, `route` | по окончании цикла: `latency_ms` по итоговому маршруту | время по веткам: без добора p95 9,7 с, справочник 10,8 с, добор 17,8 с, отказы 6,3 с (фаза 1) |
| 14 | `corp_ed_decision_speculative_total` | Counter | `mode`, `outcome`, `reason` | п. 2 (запуск или отсечение), п. 5 (взят), «Когда выбрасывается»; в тени `gate` не применяется — доля выброшенных в тени ≠ доле в `on` | цена: выброшено ≈ 25 % на стенде, 48 % на смешанных, 83 % на пакете с отказами |
| 15 | `corp_ed_decision_branch_total` | Counter | `mode`, `kind` (`recheck`, `directory`, `extra_search`, `shadow`), `result` (значения из 1.1 для своего `kind`) | «Перепроверка» (причина из `decision_recheck_reason` × `confirmed`/`flipped`), п. 3 `DIRECTORY` (BH-47), п. 4, «Теневой режим» (`decision_shadow_agree`) | исходы веток одним счётчиком: перепроверка — у 29 % ответов (стенд 15 %), справочник, доборы, согласие тени с конвейером |
| 16 | `corp_ed_decision_tokens_total` | Counter | `provider`, `direction` | каждый вызов решения (токены пробы в коде не считаются — просьба 24.10) | цена шага решения в ₽/час; gpt-oss у Cloud.ru 15,86 / 61 ₽ за 1 млн |

Справочник и перепроверка сведены в № 15, чтобы не плодить счётчики:
`kind` и `result` — пары из перечня (`recheck` × {`refusal_confirmed`, `refusal_flipped`, `role_confirmed`, `role_flipped`},
`directory` × {`hit`, `partial`, `miss`, `absent`}, `extra_search` ×
{`new`, `empty`, `repeated`, `limit`}, `shadow` × {`agree_answer`,
`agree_refusal`, `scheme_refused`, `pipeline_refused`}), других пар
нет. Причина перепроверки вшита в `result` (а не отдельной меткой), чтобы
пары оставались перечнем; `role_*` живые — R2 в коде включён. Таймаут
или ответ не по схеме у перепроверки сейчас пишется как `*_confirmed` —
неверно (просьба 24.10: откат на спекулятивный ответ, не `confirmed`).

**Сбой фона тени** (`decision_shadow_timeout` / `decision_shadow_failed`)
сейчас только в логе: тень может перестать писать незаметно, а
`questions_total{mode="shadow"}` просто перестанет расти. Просьба 24.10 —
счётчик, например `corp_ed_decision_shadow_errors_total{reason}` с
перечнем `timeout`, `error`.

### 1.3. Метрики версий и качества (работают и без BH-46)

| # | Имя | Тип | Метки | Где писать | Зачем |
|---|---|---|---|---|---|
| 17 | `corp_ed_ml_build_info` | Info: `Info("corp_ed_ml_build", …)` даёт ряд `corp_ed_ml_build_info` = 1 (проверено на prometheus_client из `.venv`) | `decision_prompt` (`decision-v7`), `answer_prompt` (`faq-v2.7`), `faq_prompt` (`faq-v2.9`), `general_prompt` (`faq-v2.10` или пусто), `edition_prompt` (`edition-v1`), `contact_prompt` (`contact-v1`), `embedding_model`, `answer_model`, `decision_model_cloudru`, `decision_model_yandex`, `decision_mode`, `decision_effort` (`low+recheck`) | при старте API из констант ML и настроек | на графике видно, с какой версии поехало; одна строка, значения — константы кода |
| 18 | `corp_ed_faq_best_distance` | Histogram, корзины 0,30 / 0,35 / 0,40 / 0,43 / 0,46 / 0,49 / 0,51 / 0,53 / 0,55 / 0,57 / 0,59 / 0,62 / 0,65 / 0,70 / 0,75 / 0,80 / 0,90 / 1,0 | нет | `faq_service` там же, где `best_vector_distance` уходит в `qa_log`; вопросы с выдержками из вложения — не наблюдать | сдвиг распределения — порог устарел (`RESILIENCE-RESEARCH.md` §2.5); корзины гуще у 0,51 / 0,59 / 0,70 (порог, граница выдержек, шлюз BH-37) |
| 19 | `corp_ed_faq_feedback_total` | Counter | `vote`, `feedback_reason` | ручка оценки ответа (`chat_service`, где проверяется `FEEDBACK_REASONS`); снятие оценки не вычитать | 👍/👎 в реальном времени без компании; по компаниям — агрегаты (слой 4) |

Версия модели из ответа поставщика (`llm_model_version`, BH-26) в метку
**не** берём: строка приходит снаружи, её значения не перечислить. Смена
версии видна в суточном агрегате и в журнале (слой 4).

### 1.4. Метрика ночного прогона (textfile, как у `cron-run.sh`)

| Имя | Тип | Метки | Кто пишет |
|---|---|---|---|
| `corp_ed_ml_eval_score` | Gauge | `suite` (`regress`, `stand`, `dev_retrieval`, `dev_judge`), `metric` (`pass_rate`, `refusal_ok`, `hit5`, `judge_mean`, `p95_seconds`, `cost_rub`) | скрипт ночного прогона, в `/var/lib/node_exporter/textfile` |
| `corp_ed_ml_eval_threshold` | Gauge | те же | тот же скрипт: порог из протокола (слой 4) |

Запуск — через `deploy/stage/cron-run.sh ml_eval …`: отметку успеха
(`corp_ed_cron_last_success_timestamp_seconds{task="ml_eval"}`) он
пишет сам. Но прогон — при выкатке и раз в неделю (4.1, вариант «г»), а
`NightlyJobMissed` бэкенда — `time() - corp_ed_cron_last_success_timestamp_seconds
> 26h` по любому `task`: она висела бы 6 дней из 7. Свой порог: в
`NightlyJobMissed` — `task!="ml_eval"`, для прогона — копия правила с
порогом ≈ 8 сут (`> 8 * 86400`, например `MlEvalMissed`; правило бэкенда
в `kronto.yml`, в 14 правил ML не входит). Скрипт textfile и строка cron —
за ML (24.10 ещё нет).

Уже есть и остаются: `corp_ed_faq_answers_total{origin}` (доли ответов
по документам, общих, отказов, реплик), `corp_ed_faq_degraded_total`,
`corp_ed_http_*`. Вызовы **ответа** Flash и эмбеддингов по исходу — BH-52
(`corp_ed_llm_*`, встроено 24.10), вне этого документа.

---

## 2. Тревоги (Alertmanager → почта и Telegram)

Маршрутизация — как сейчас (`alertmanager/entrypoint.sh`): один
получатель `team` (почта через alert-mail и Telegram), `critical` —
повтор раз в час, `warning` — раз в 4 часа. **Ночью на стенде не будит
ничего** (решение 30.09: звонок или SMS — только с боевого сервера через
Ping-Admin, и Ping-Admin проверяет доступность снаружи, а не правила
Prometheus). Кого будить ночью на боевом — открытый вопрос §11 п. 3;
ниже в колонке «Ночью» — кандидаты ML (только то, что ведёт к простою у
сотрудника в `on`).

Две правки конфига, нужные для этих правил (сделаны бэкендом, 24.10):
- в `group_by` маршрута добавить `provider` и `suite` — иначе тревоги
  двух звеньев склеятся в одно письмо;
- в `inhibit_rules` (источник `NotReady`) добавить цели
  `Decision.*|MlGeneral.*|MlDistance.*` — при лежащем стенде частные
  ML-тревоги не нужны.

**Два этапа (решение Артёма 22.10).** С первого дня с получателем —
**пять** правил: 3 `DecisionProviderConfigError`, 4
`DecisionProviderNoFunds`, 9 `DecisionAllLinksDown`, 1
`DecisionFallbacksHigh` (только `on`) и 12 `MlNightlyEvalDropped`. Это
всё, что означает «система сломана или кончились деньги» и требует
действия сразу. Остальные девять (p95, пробы, доли,
сдвиг дистанций, тень, выброшенные) первую неделю теневого режима
пишутся в Prometheus без получателя в Alertmanager — по ним видно
нормальные уровни — и включаются по одной с порогами, уточнёнными по
журналу. Правило: тревога, которая срабатывает и не требует действия,
через месяц перестаёт читаться. Качество между тревогами смотрят на
дашборде раз в неделю. Правила — как встроены бэкендом (`deploy/monitoring/prometheus/rules/ml.yml`,
`main` 6162b0a, 14 правил; без получателя — метка `observe: "true"`,
маршрут на получателя `none`; включать по одной — убирать метку):

```yaml
# Тревоги ML (BH-49, проект — docs/ml-monitoring.md раздел 2). С получателем
# с первого дня — пять (решение Артёма 22.10): ошибка настройки поставщика
# решения, 402 «нет денег», оба поставщика недоступны, откаты > 20 % в on,
# ночной прогон ниже порога. Остальные — с меткой observe="true": пишутся в
# Prometheus без получателя (alertmanager/entrypoint.sh), первую неделю тени
# смотрим нормальные уровни, потом включаем по одной.
#
# Отличия от черновика ML: дублёр решения — Qwen у Яндекса (provider="yandex"),
# не MWS; предохранителя решения нет («не на старте», BH-46 п. 6), поэтому
# DecisionBreakerOpen нет, а «оба поставщика недоступны» — по откатам с
# причиной provider_error (оба звена упали).
groups:
  - name: ml-decision
    rules:
      # 1. Доля откатов схемы за 15 мин (только on)
      - alert: DecisionFallbacksHigh
        expr: |
          sum(increase(corp_ed_decision_fallback_total{mode="on"}[15m]))
          / sum(increase(corp_ed_decision_questions_total{mode="on", route!="rule"}[15m])) > 0.2
          and sum(increase(corp_ed_decision_questions_total{mode="on", route!="rule"}[15m])) >= 20
        for: 5m
        labels:
          severity: critical
        annotations:
          summary: "Схема ответа откатывается на конвейер: {{ $value | humanizePercentage }} за 15 мин"
          description: "Поставщик решения тормозит или отвечает ошибками. Панель «Поставщики моделей»."
      - alert: DecisionFallbacksElevated
        expr: |
          sum by (mode) (increase(corp_ed_decision_fallback_total[15m]))
          / sum by (mode) (increase(corp_ed_decision_questions_total{route!="rule"}[15m])) > 0.05
          and sum by (mode) (increase(corp_ed_decision_questions_total{route!="rule"}[15m])) >= 20
        for: 15m
        labels:
          severity: warning
          observe: "true"
        annotations:
          summary: "Откатов схемы больше 5 % ({{ $labels.mode }})"
      # 2. Ошибка настройки у поставщика — сразу
      - alert: DecisionProviderConfigError
        expr: |
          sum by (provider) (increase(corp_ed_decision_calls_total{outcome="error_config"}[5m])) > 0
          or sum by (provider) (increase(corp_ed_decision_probe_total{outcome="error_config"}[20m])) > 0
        labels:
          severity: critical
        annotations:
          summary: "{{ $labels.provider }}: ошибка настройки (ключ, права, модель)"
          description: "400/401/403/404. Вопросы идут через второе звено; код — в журнале (qa_log.decision)."
      # 3. Нет денег (Cloud.ru 402)
      - alert: DecisionProviderNoFunds
        expr: |
          sum by (provider) (increase(corp_ed_decision_calls_total{outcome="error_payment"}[5m])) > 0
          or sum by (provider) (increase(corp_ed_decision_probe_total{outcome="error_payment"}[20m])) > 0
        labels:
          severity: critical
        annotations:
          summary: "{{ $labels.provider }}: 402 — на счёте нет денег"
      # 4. Медленный вызов решения (low) 30 мин
      - alert: DecisionCallSlow
        expr: |
          histogram_quantile(0.95, sum by (le, provider)
            (rate(corp_ed_decision_call_duration_seconds_bucket{effort="low"}[30m]))) > 4
        for: 30m
        labels:
          severity: warning
          observe: "true"
        annotations:
          summary: "{{ $labels.provider }}: p95 решения low {{ $value | humanize }} с (норма до 1,8–3,0 с)"
      # 5. Ответ схемы дольше критерия Артёма (p95 ≤ 12 с)
      - alert: DecisionAnswerSlow
        expr: |
          histogram_quantile(0.95, sum by (le)
            (rate(corp_ed_decision_stage_duration_seconds_bucket{mode="on", stage="total"}[30m]))) > 12
          and sum(increase(corp_ed_decision_stage_duration_seconds_count{mode="on", stage="total"}[30m])) >= 20
        for: 30m
        labels:
          severity: warning
          observe: "true"
        annotations:
          summary: "p95 ответа схемы {{ $value | humanize }} с — выше 12 с"
      # 6. Пробный вызов Cloud.ru не прошёл 2 раза подряд
      - alert: DecisionProbeFailing
        expr: corp_ed_decision_probe_consecutive_failures >= 2
        labels:
          severity: warning
          observe: "true"
        annotations:
          summary: "{{ $labels.provider }}: пробный вызов не прошёл {{ $value }} раза подряд"
          description: "Баланс, ключ, модель. Исход — corp_ed_decision_probe_total."
      # 7. Оба поставщика не ответили — схема откатывается
      - alert: DecisionAllLinksDown
        expr: sum(increase(corp_ed_decision_fallback_total{reason="provider_error"}[10m])) >= 3
        labels:
          severity: critical
        annotations:
          summary: "Оба поставщика решения не отвечают — вопросы идут без схемы"
          description: "Cloud.ru и Qwen у Яндекса упали на одних и тех же вопросах. Тревоги DecisionProvider*, Llm*."
      # 11. Тень: схема отказывает там, где конвейер ответил
      - alert: DecisionShadowDisagreement
        expr: |
          sum(increase(corp_ed_decision_branch_total{kind="shadow", result="scheme_refused"}[1d]))
          / sum(increase(corp_ed_decision_branch_total{kind="shadow"}[1d])) > 0.05
          and sum(increase(corp_ed_decision_branch_total{kind="shadow"}[1d])) >= 50
        labels:
          severity: warning
          observe: "true"
        annotations:
          summary: "Тень: схема отказала, а конвейер ответил — {{ $value | humanizePercentage }} за сутки"
      # 12. Выброшенные спекулятивные ответы — цена
      - alert: DecisionSpeculativeWasteHigh
        expr: |
          sum(increase(corp_ed_decision_speculative_total{outcome="wasted"}[1d]))
          / sum(increase(corp_ed_decision_speculative_total{outcome=~"used|wasted"}[1d])) > 0.5
          and sum(increase(corp_ed_decision_speculative_total[1d])) >= 50
        labels:
          severity: warning
          observe: "true"
        annotations:
          summary: "Выброшено {{ $value | humanizePercentage }} спекулятивных ответов за сутки"

  - name: ml-quality
    rules:
      # 8. Рост доли общих ответов и отказов: 6 ч против 7 сут
      - alert: MlGeneralShareRising
        expr: |
          (
            sum(increase(corp_ed_faq_answers_total{origin=~"general_knowledge|none"}[6h]))
            / sum(increase(corp_ed_faq_answers_total{origin!="small_talk"}[6h]))
          ) - (
            sum(increase(corp_ed_faq_answers_total{origin=~"general_knowledge|none"}[7d]))
            / sum(increase(corp_ed_faq_answers_total{origin!="small_talk"}[7d]))
          ) > 0.15
          and sum(increase(corp_ed_faq_answers_total{origin!="small_talk"}[6h])) >= 50
        labels:
          severity: warning
          observe: "true"
        annotations:
          summary: "Доля «нет в документах» выросла на {{ $value | humanizePercentage }} к неделе"
          description: "Сломался поиск или эмбеддинги, удалили документы, новая тема вопросов. Отчёт о пробелах, журнал."

      # 9. Сдвиг медианы ближайшей дистанции: сутки против суток неделю назад
      - alert: MlDistanceShift
        expr: |
          abs(
            histogram_quantile(0.5, sum by (le) (rate(corp_ed_faq_best_distance_bucket[1d])))
            - histogram_quantile(0.5, sum by (le) (rate(corp_ed_faq_best_distance_bucket[1d] offset 7d)))
          ) > 0.05
          and sum(increase(corp_ed_faq_best_distance_count[1d])) >= 100
        labels:
          severity: warning
          observe: "true"
        annotations:
          summary: "Медиана ближайшей дистанции сдвинулась на {{ $value | humanize }} за неделю"
          description: "Сменилась модель эмбеддингов или корпус — пороги 0,59 / 0,70 могли устареть. Сказать ML."

      # 10. Ночной прогон ML (метрики пишет скрипт ML в textfile; расписание —
      # решение команды): итог ниже порога — с получателем, «ни разу не было
      # итога» — на наблюдении, пока прогон не настроен.
      - alert: MlNightlyEvalDropped
        expr: corp_ed_ml_eval_score < on (suite, metric) corp_ed_ml_eval_threshold
        labels:
          severity: warning
        annotations:
          summary: "Ночной прогон {{ $labels.suite }}: {{ $labels.metric }} = {{ $value }} ниже порога"
      - alert: MlNightlyEvalNeverRan
        expr: absent(corp_ed_ml_eval_score{suite="regress"})
        for: 26h
        labels:
          severity: warning
          observe: "true"
        annotations:
          summary: "Ночной прогон ML ни разу не дал итога"
```

**Известные дыры (просьба бэкенду 24.10):**
- `DecisionAllLinksDown` смотрит на откаты `provider_error` — оба звена
  упали быстрой ошибкой. Главный сценарий аварии — Cloud.ru висит (21.10:
  30–315 с), Qwen падает — даёт откат `timeout`, и правило молчит. Просто
  добавить `timeout` нельзя: он смешан с исчерпанием бюджета живыми
  вызовами (`low` 1,8 с + перепроверка 4,8 с > 6 с — вызов после добора
  получает пустой бюджет без единого запроса). Набросок — считать по
  исходам звеньев, после появления `cancelled`:
  `count(sum by (provider) (increase(corp_ed_decision_calls_total{provider!="mws", outcome=~"timeout|error_fast|error_config|error_payment"}[10m])) >= 2) >= 2`
  — проверить по журналу (`decision_timeout`). Будить ли этим правилом в
  `shadow` (по 0.4 — нет) — решение команды. Без трафика правило молчит —
  ночью это ловит только проба.
- `DecisionCallSlow` для Cloud.ru слепо, пока отменённые звенья не
  наблюдаются (p95 обрезан на ≈ 3,7 с при пороге 4 с); у Qwen
  (`effort="none"`) правила по времени нет.
- Проба ночью будит только на `error_config` / `error_payment` (правила
  2–3); `timeout`, `error_fast`, `invalid` пробы — `DecisionProbeFailing`,
  он на наблюдении.
- Ответ 200 не-JSON (страница прокси) сейчас даёт `error_config` — ложная
  critical-тревога «ключ» (1.1).

| № | Тревога | Порог | Почему такой (`ml-report.md`, handoff) | Важность | Ночью |
|---|---|---|---|---|---|
| 1 | `DecisionFallbacksHigh` / `…Elevated` | 20 % за 15 мин (on) / 5 % (любой режим), ≥ 20 вопросов | в норме откатов 0: невалидных решений 0 при лимите 2000 (попытки 2–3), в фазе 1 вызов решения ≤ 6 с; при торможении 21.10 (medium p95 7,3–11,9 с) откатилась бы заметная доля — это авария | critical / warning | кандидат (on) |
| 2 | `DecisionProviderConfigError` | > 0 за 5 мин (вызовы) или 20 мин (пробы — раз в 15 мин) | BH-46 «Бюджет…» п. 4: ошибки настройки не переключают молча, а будят | critical | кандидат |
| 3 | `DecisionProviderNoFunds` | > 0 (пробы — за 20 мин) | Cloud.ru 402 — предоплата: баланс кончается без предупреждения (у ML на счёте было ≈ 79 ₽ 21.10, `ml-agent-plan.md` §10); при 402 у основного звена весь трафик решения уходит на Qwen (≈ 0,37 ₽ за вызов против 0,05) | critical | кандидат |
| 4 | `DecisionCallSlow` | p95 low > 4 с, 30 мин | проба 20.10: low p95 Cloud.ru 1,5–1,8 с; при торможении 21.10 low p95 ≤ 3,8 с — 4 с ловит только аварию. Для Cloud.ru слепо, пока отменённые звенья не наблюдаются; у Qwen (`none`) правила нет | warning | нет |
| 5 | `DecisionAnswerSlow` | p95 общего > 12 с, 30 мин, ≥ 20 ответов | критерий Артёма p95 ≤ 12 с (§7.4 п. 1); приёмка 4 — 11,6 с, фаза 1 — 8,3 с | warning | нет |
| 6 | `DecisionProbeFailing` | 2 неудачи подряд | BH-46 «Бюджет…» п. 5; только `cloudru` | warning | нет |
| 7 | `DecisionAllLinksDown` | ≥ 3 откатов `provider_error` за 10 мин | оба звена упали быстрой ошибкой — схема не работает. `DecisionBreakerOpen` нет: нет предохранителя. Дыра «висит + падает» — выше | critical | кандидат |
| 8 | `MlGeneralShareRising` | +15 п. п. к неделе, ≥ 50 ответов | на стенде вне документов ≈ 10 % вопросов (15 из 155); +15 п. п. — не колебание темы, а поломка поиска или корпуса; на живом трафике — уточнить через неделю | warning | нет |
| 9 | `MlDistanceShift` | медиана ±0,05 к неделе, ≥ 100 вопросов | 0,05 — ширина `near_margin` BH-37: сдвиг на неё переводит вопросы через порог; корзины гуще у 0,51–0,59 | warning | нет |
| 10 | `MlNightlyEvalDropped` / `…NeverRan` | ниже порога протокола / нет итога 26 ч | пороги — слой 4 | warning | нет |
| 11 | `DecisionShadowDisagreement` | > 5 % за сутки, ≥ 50 | приёмка 4: схема 121 против 119 конвейера из 140 — расходятся единицы процентов; больше — ложные отказы схемы (low без перепроверки давал 7 из 85) | warning | нет |
| 12 | `DecisionSpeculativeWasteHigh` | > 50 % за сутки | стенд ≈ 25 %, смешанные наборы до 48 %, пакет с отказами 83 %; выше 50 % — цена ×1,3 растёт, повод включить отсечение (фаза 1) | warning | нет |

Всего **14 правил** (12 строк таблицы; в 1 и 10 по два). Смену версии
модели у поставщика (`RESILIENCE-RESEARCH.md` §2.5) ловим не тревогой, а
суточным агрегатом и ночным прогоном: версия — не метка (1.3).

---

## 3. Наглядно (Grafana)

Три новые строки внизу `kronto.json` (после «Логи», с `y = 57`); id
панелей 23–52. Готовый JSON — `docs/ml-monitoring-dashboard.json`
(массив `panels`; бэкенд дописывает его в конец `panels` дашборда).
Стиль — как у существующих панелей: `stat` с порогами цвета,
`timeseries` с легендой снизу, источник `prometheus`.

**Строка «Решение до ответа»** (id 23)

| id | Панель | Тип | Запрос (кратко) | Норма |
|---|---|---|---|---|
| 24 | Вопросы по режиму, сутки | stat | `sum by (mode) (increase(corp_ed_faq_decision_mode_total[1d]))` | видно, сколько идёт в тень и в схему |
| 25 | Доля откатов, 15 мин | stat | откаты / вопросы без `rule` | 0 %; жёлтый от 5 %, красный от 20 % |
| 26 | p95 ответа схемы (on), 30 мин | stat | `histogram_quantile(0.95, … stage="total", mode="on")` | ≤ 12 с; жёлтый от 9 с |
| 27 | Выброшено спекулятивных, сутки | stat | `wasted / (used + wasted)` | ≈ 25 %; красный от 50 % |
| 28 | Маршруты | timeseries | `sum by (route) (increase(corp_ed_decision_questions_total[1h]))` | большинство — `answer` (стенд 174 из 196) |
| 29 | Время по шагам, p95 | timeseries | `histogram_quantile(0.95, sum by (le, stage) (…stage_duration…))` | решение ≤ 2–5 с, ответ ≤ 2–3 с |
| 30 | Время по маршрутам, p95 | timeseries | `… by (le, route) (…question_duration…)` | добор — самый медленный (17,8 с в приёмке 4) |
| 31 | Откаты по причине | timeseries | `sum by (reason) (increase(corp_ed_decision_fallback_total[1h]))` | пусто |
| 32 | Спекулятивный ответ | timeseries | `sum by (outcome, reason) (increase(…speculative_total[1h]))` | `used` и `wasted/not_found` |
| 33 | Перепроверка, справочник, доборы | timeseries | `sum by (kind, result) (increase(…branch_total{kind!="shadow"}[1h]))` | перепроверка ≈ 15–29 %, `refusal_flipped` редко (1 из 21 в фазе 1) |

**Строка «Поставщики моделей»** (id 34)

| id | Панель | Тип | Запрос | Норма |
|---|---|---|---|---|
| 35 | Доля решений дублёра (Qwen у Яндекса), час | stat | `ok` у `yandex` / все `ok`, 1 ч | 5–15 %; жёлтый от 15 %, красный от 50 % (Cloud.ru лежит или висит) |
| 36 | Ошибки ключа и оплаты поставщиков решения, час | stat | `sum(increase(…calls_total{outcome=~"error_config\|error_payment"}[1h]))` | 0; красный от 1 |
| 37 | Пробный вызов Cloud.ru: неудач подряд | stat | `corp_ed_decision_probe_consecutive_failures{provider="cloudru"}` | 0; красный от 2; проба раз в 15 мин |
| 38 | Переходы к дублёру, сутки | stat | `sum(increase(…failover_total[1d]))` | без порога: дубль — штатный (`hedge` на 5–15 % вызовов `low`) |
| 39 | Вызовы решения по исходу | timeseries | `sum by (provider, outcome) (rate(…calls_total{provider!="mws"}[5m]))` | только `ok`; Cloud.ru, проигравший дубль, не виден (нет `cancelled`) |
| 40 | Время вызова решения, p50 / p95 | timeseries | `histogram_quantile(…, sum by (le, provider, effort) (…call_duration…))` | low p95 ≤ 2 с, medium ≤ 5 с, Qwen ≈ 0,4–0,7 с; p95 Cloud.ru обрезан, пока отменённые не наблюдаются |
| 41 | Переходы к дублёру по причине | timeseries | `sum by (reason) (increase(…failover_total[1h]))` | `hedge` — 5–15 % вызовов `low`; ошибки и `invalid` — единицы |
| 42 | Пробные вызовы: время | timeseries | `histogram_quantile(0.95, sum by (le, provider) (…probe_duration…[30m]))` | ≈ 1–2 с; ночью — единственный сигнал |
| 43 | Токены решения в час | timeseries | `sum by (provider, direction) (increase(…tokens_total[1h]))` | цена: × 15,86 / 61 ₽ за 1 млн (Cloud.ru) |
| 44 | Версии промптов и моделей | table | `corp_ed_ml_build_info` | одна строка; меняется только с выкаткой |

**Строка «Качество ответов»** (id 45)

| id | Панель | Тип | Запрос | Норма |
|---|---|---|---|---|
| 46 | Доля общих ответов и отказов | timeseries | `general_knowledge + none` к ответам без реплик, 1 ч и 7 сут | ровная линия; стенд ≈ 10 % |
| 47 | Ближайшая дистанция: медиана и p90 | timeseries | `histogram_quantile(0.5 / 0.9, …best_distance…[1h])` и медиана `offset 7d` | линии сегодня и неделю назад совпадают |
| 48 | Вопросы в зоне шлюза и за порогом | timeseries | доли корзин `le="0.59"`, `le="0.7"` | стабильно |
| 49 | Тень: схема против конвейера | timeseries | `sum by (result) (increase(…branch_total{kind="shadow"}[1h]))` | почти всё `agree_*` |
| 50 | Оценки ответов | timeseries | `sum by (vote, feedback_reason) (increase(corp_ed_faq_feedback_total[1d]))` | 👎 — единицы |
| 51, 52 | Ночной прогон: пакет регрессии / стенд, доля верных | stat | `corp_ed_ml_eval_score{suite=…, metric="pass_rate"}` | не ниже порога (жёлтый ниже) |

Ночной прогон «часов с последнего успеха» — уже есть панелями 18–20
(шаблон `task="backup"`); для `task="ml_eval"` — та же панель, её бэкенд
может добавить копией (в JSON не включена, чтобы не дублировать).

24.10: бэкенд скопировал 27 из 30 объектов байт в байт, а 35, 36, 38
переделал под дублёр, оставив подписи и пороги предохранителя (одно
решение Qwen за час — «проба», два — «открыт»). ML взял их смысл, убрал
подписи, поставил свои пороги и поправил описания 25, 31, 37, 40, 41 и
`provider!="mws"` в 39, 40, 43 — бэкенду скопировать панели 25, 31,
35–41, 43 заново.

Проверка (22.10, скрипт ML; 24.10 — повторно): JSON читается `json.load` — 30 объектов
(3 строки, 27 панелей), id 23–52 не пересекаются с 1–22 `kronto.json`,
новые строки начинаются ниже последней (`y` от 57), `gridPos` после
правок 24.10 не менялись; 18 метрик в запросах (без суффиксов `_bucket` /
`_count` / `_info`) — 17 есть в `core/metrics.py` (`main` 6162b0a),
`corp_ed_ml_eval_score` пишет скрипт прогона (1.4), неизвестных нет.
YAML правил раздела 2 читается `yaml.safe_load` — 14 правил;
`promtool check rules` у ML не запускался (нет на машине).

---

## 4. Хорошо ли отвечает (часы–дни)

### 4.1. Ночной прогон

Что гоняем — **только наборы, которые уже есть и не закрыты**: holdout
золотого набора не открываем никогда (инструменты закрывают его без
`--split holdout`; ночной скрипт этот флаг не передаёт и проверяет, что в
наборе нет `split=holdout`).

| Набор | Где | Чем считается | Вопросов | ₽ за прогон (оценка) |
|---|---|---|---|---|
| `regress` — пакет регрессии фазы 1: stand13 + вне документов (15) + NP2 (4) | стенд, через API продукта (`eval.run_eval`), компания с корпусом стенда | `stand_cases.verdict`, без судьи | 32 | ≈ 7–8 (0,21–0,255 ₽ на вопрос, фаза 1) |
| `dev_retrieval` — золотой dev, только поиск | там же или офлайн | Hit@5, MRR (`retrieval_eval`) | 39 | копейки (эмбеддинги вопросов) |
| `stand` — весь стенд BH-38 | стенд, API продукта | `stand_cases.verdict` | 155 (140 + 15) | ≈ 37 (0,24 ₽) |
| `dev_judge` — золотой dev с судьёй | офлайн-стенд ML | судья Alice AI LLM | 39 | ≈ 56 (ответ ≈ 0,24 + судья ≈ 1,2 ₽) |

Варианты расписания (цены — Яндекс + Cloud.ru по ценам из общих правил):

| Вариант | Расписание | ₽ в сутки | ₽ в месяц |
|---|---|---|---|
| а | каждую ночь `regress` + `dev_retrieval`; раз в неделю `stand`; `dev_judge` — только по тревоге или перед сменой версии | ≈ 8 + 37 / 7 ≈ 13 | ≈ 400 |
| б | как «а», плюс `dev_judge` раз в неделю | ≈ 21 | ≈ 640 |
| в | каждую ночь всё | ≈ 100 | ≈ 3 000 — дороже всего бюджета ML, не предлагаем |

| г | **`regress` + `dev_retrieval` при каждой выкатке и раз в неделю; `stand` раз в месяц; `dev_judge` — по тревоге или перед сменой версии** | — | ≈ 100 |

**Принято «г» (решение Артёма 22.10).** Ответы на фиксированном наборе
меняются только когда меняется наш код или модель у поставщика:
прогон при выкатке ловит первое, еженедельный — второе; ежедневный
имел бы смысл только с живыми вопросами клиентов, а это другой механизм
и другое согласие (раздел 5). Судья — ≈ 85 % цены любого прогона с ним,
а dev по судье (39 вопросов) различает только крупные сдвиги (1,632 →
1,737 за две недели работы); поломку быстрее и дешевле ловит пакет
регрессии. Деньги — ключи продукта: **расписание и бюджет — команда**.

Итоги в метрику (1.4) и пороги из протокола:
- `regress`, `pass_rate` — порог 0,75 (базовый фазы 1: 29 из 32 = 0,91;
  шум stand13 ±2 из 13 — порог берёт его с запасом);
- `regress`, `refusal_ok` — вне документов с пометкой: 15 из 15, порог
  1,0 (здесь ошибка — не шум);
- `stand`, `pass_rate` — порог 0,80 (приёмка 4: 121 из 140 = 0,86);
- `dev_retrieval`, `hit5` — порог 0,75 (dev Hit@5 0,794 на v2, 15.10);
- `p95_seconds`, `cost_rub` — пишутся без порога, для графика.

Два режима прогона: конвейер (`off`) и схема (`on` у тестовой компании)
— одна ночь, одни вопросы; тогда сдвиг поставщика виден в разнице.

Что нужно, чтобы запустить (раздел 0 handoff, п. 5 — ещё открыт):
тестовая компания на стенде с корпусом BH-38 (`tests/fixtures/stand_quality`
— в репозитории) и учётка ADMIN для `eval.run_eval`. Золотой dev лежит в
`eval/private` (не в git): ночной прогон dev на сервере — копия набора на
сервер, **решение команды**; иначе `dev_*` гоняет ML со своей машины
раз в неделю.

### 4.2. Суточные агрегаты по компаниям (П-1)

П-1 открыт (решение 28.09 — «до пилота не делать»; `MONITORING-RESEARCH.md`
§2.3, §11 вопрос 6). Предложение §2.3 (таблица `usage_daily` без текстов,
ночная `cli stats` под `app.tenant_id` после `gaps`, роль `corp_ed_stats`
с `SELECT` только на неё, хранение дольше 90 дней) ML поддерживает и
просит добавить ML-колонки:

| Колонки | Из `qa_log` | Есть сейчас? |
|---|---|---|
| вопросов; по `origin` (`documents`, `general_knowledge`, `none`, `small_talk`) | `origin` | есть |
| 👍, 👎, 👎 по причине (5 значений) | `feedback`, `feedback_reason` | есть |
| кредиты, токены ответа | `credits`, `input_tokens`, `output_tokens` | есть |
| медиана и p90 ближайшей дистанции; доля в зоне шлюза (0,59; 0,70] | `best_vector_distance` | есть |
| вопросов с вложением; со вторым шагом поиска BH-43 | `attachment_chunks`, `search_query` | есть |
| версии: `prompt_version`, `llm_model_version` (последняя за день) | есть | есть |
| p50 / p95 времени ответа | `latency_ms` | есть (BH-49, 24.10) |
| режим; маршруты; откаты; таймауты; выброшенные; справочник; перепроверки; расхождения тени | колонки `decision_*`, `decision_trace` (JSONB) | есть (BH-46, 24.10) |

Число строк — компании × дни; агрегаты видит команда (Grafana с
источником PostgreSQL под ролью `corp_ed_stats`, или CSV из CLI), админ
компании видит своё в нынешней аналитике (`analytics_service`).

### 4.3. Теневой режим

В тени главное — **расхождения**: схема отказала, а конвейер ответил
(`scheme_refused`), и наоборот (`pipeline_refused`). Метрика — № 15
`kind="shadow"`; по компаниям — агрегат; для разбора — выборка 5.2.
Отчёт ML команде после 1–2 недель или ≈ 500 вопросов (BH-46, «Порядок
включения»): доля маршрутов, выброшенных, таймаутов по часам и звеньям,
расхождения с разбором, пороги для `on`.

**Текст ответа тени — действует вариант (б)** (код бэкенда, 24.10): в
`qa_log` только флаги, и не отдельными колонками, а в `decision_trace`
(JSONB) — `decision_shadow_source`, `decision_shadow_not_found`,
`decision_pipeline_not_found`, `decision_shadow_agree`,
`decision_shadow_ms`, токены и вызовы своего ответа тени; текста ответа
схемы нет ни в `qa_log`, ни в `chat_messages`. Варианты были: (а)
хранить ответ тени рядом с ответом в `chat_messages` (тот же срок и
доступ) — ML предлагал: без текста не понять, лучше ли ответ схемы при
`agree_answer` на ветках «свой вызов» (справочник, добор, другая
редакция — ≈ 10–15 % вопросов); (б) только флаги. Решение команды
формально не записано; пока не решит иначе — (б), и разбор тени идёт по
маршрутам, оговоркам, флагам отказа и выдержкам.

---

## 5. Улучшаться на данных клиента (недели)

### 5.1. Что есть в `qa_log` (миграции и `QaLog`, 24.10)

Есть: вопрос **после `mask_pii`**, `standalone_question`, эмбеддинг
вопроса, `origin`, `answer_given`, `best_vector_distance`,
`best_fulltext_score`, `source_chunk_ids`, `prompt_version`,
`llm_model`, `llm_model_version`, `embedding_model`, токены, кредиты,
**`feedback` (±1), `feedback_reason` (5 причин), `feedback_comment`
(после `mask_pii`)**, `attachment_chunks`, `miss_kind` (отчёт о
пробелах), `rerank_model`, `rerank_ms`, `search_query` (BH-43),
`conversation_id`, `history_turns`, `created_at`.

С 24.10 есть и `latency_ms` (BH-49), колонки `decision_*` и
`decision_trace` (BH-46), `answer_provider`, `answer_fallback_reason`
(BH-50); вектор и модель эмбеддингов могут быть NULL (реплики правилом
BH-48). Нет: **текста ответа** (он в `chat_messages`, связь `qa_log_id`),
текста ответа тени (4.3), агрегатов `usage_daily`. Хранение — 90 дней
(`QA_LOG_RETENTION_DAYS`, `purge`), RLS с `FORCE`.

### 5.2. Какие выборки нужны ML

| Выборка | Поля | Во что превращается |
|---|---|---|
| 👎 с причиной | вопрос, причина, комментарий, `origin`, версии, `source_chunk_ids` → тексты выдержек, ответ из `chat_messages` | разбор по классам провалов (`ml-agent-plan.md` §10.4); вопросы с проверенным ответом — в свежий набор |
| расхождения тени | вопрос, `decision`, маршрут, `missing`, флаги `*_not_found`, оба ответа (если 4.3 «а») | ложные отказы схемы и конвейера — пакет регрессии; пороги для `on` |
| промахи справочника | `decision_directory`, роль (`lookup_role`), запрос добора | синонимы ролей, замещение (BH-47, фаза 0); набор «кто отвечает за…» |
| общие ответы и отказы | вопрос, `miss_kind`, дистанция, `search_query` | отчёт о пробелах уже их кластеризует; ML берёт кластеры «по теме, но не нашли» (дистанция ≤ 0,70) как кандидаты провала поиска |
| откаты и таймауты | только счётчики и время по веткам, без текста | время и звенья; текст не нужен |

### 5.3. Как не нарушить 152-ФЗ и RLS

1. **По умолчанию — только агрегаты** (4.2): без текстов, без
   согласия, переживают `purge`.
2. **Тексты — только по согласию клиента** (в договоре поручения или
   отдельным письмом: «обезличенные вопросы и оценки для улучшения
   качества»). Выгрузку делает бэкенд-команда CLI (например `cli
   export-ml --kind feedback|shadow|directory|general --since …`) по одной
   компании под её `app.tenant_id` — RLS не обходится; в выгрузке —
   повторный `mask_pii`, без `user_id`, `conversation_id` и почты, id
   строки заменён случайным.
3. **Куда:** в `eval/private` ML (не в git), срок — 90 дней, как у
   `qa_log`, потом удалить; копия резервная — `backup_private.py`, тот же
   срок.
4. **Свежие наборы** из выгрузок: человек (Артём или админ клиента)
   ставит эталон — нужный документ и суть ответа; набор получает метку
   `split=dev` или «свежий для приёмки» — **в золотой holdout никогда**.
   Вопросы клиента мерятся только на корпусе этого клиента: офлайн —
   если клиент дал и документы, иначе — через API продукта на его
   компании (кредиты — договорённость с клиентом).
5. **Кто видит:** Grafana и агрегаты — команда (вход по паролю);
   выгрузки текстов — ML-инженер и Артём; клиенту — его аналитика в
   админке, как сейчас.

---

## 6. Чего не хватает вне ML (не проектируем — для списка команды)

- **Внешний сторож** — Ping-Admin и проверка самой ВМ мониторинга
  (`MONITORING-RESEARCH.md` §3.1, §7, решение 30.09); в правилах нет
  **Watchdog** (тревоги, которая горит всегда, §2.1 № 14).
- **Боевой сервер и отдельная ВМ мониторинга** — не выбраны (§11, П-10);
  сейчас мониторинг на прерываемом стенде и падает вместе с ним.
- **Ночной канал** — кто и чем будит (§11 вопрос 3); Telegram с сервера
  в России под вопросом (§0 п. 3, RISKS №50).
- **Метрики вызовов ответа (Flash) и эмбеддингов** — сделаны в BH-52
  (`corp_ed_llm_*`, 24.10); `ModelErrors` по-прежнему видит только 502
  на `/faq/ask`, а не ошибки в чате.
- **postgres_exporter, redis_exporter** (§8 п. 8) — в `compose.yaml`
  мониторинга их нет.
- **Логи 90 дней** против 14 (§11 вопрос 5, DEPLOY §10).
- **Агрегаты статистики** `usage_daily` — П-1 (§11 вопрос 6); на них
  стоит слой 4.2.
