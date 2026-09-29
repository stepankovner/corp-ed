# Мониторинг: исследование вариантов (29.09.2026)

Вопрос команды: **какую систему мониторинга поставить, чтобы собирать
статистику и следить за отказоустойчивостью**. Ниже по порядку:

- что именно нужно видеть в нашей системе (из кода и `RISKS.md`);
- какие решения существуют по слоям и что из них доступно российской
  компании в 2026 году;
- готовые сборки под нас с ценой и трудозатратами;
- наше предложение.

Версии и цифры даны на 29.09.2026 по ссылкам. Ресурсы — цифры вендоров,
а не наши замеры. Трудозатраты и объёмы — наша оценка, они помечены. Что
проверить не удалось — раздел 10.

---

## 0. Коротко

1. **Сейчас система не видит собственных отказов.**
   - Что есть: JSON-логи в stdout, `/health` без базы и Redis, пульс
     воркера, который видит только Docker, и бот в Telegram на три
     бизнес-события.
   - Чего нет: истории метрик, алертов на падение, проверки снаружи,
     контроля cron и бэкапа, логов вне хоста.
   - Падение базы `/health` не заметит (так задумано), а падение ВМ
     заметить просто некому.
2. **Главное ограничение — сервер один.** Мониторинг на той же ВМ падает
   вместе с ней. Нужны минимум проверка снаружи и «мёртвая рука» — алерт,
   который срабатывает, когда сигналы перестали приходить.
3. **Telegram — ненадёжный канал алертов.**
   - С 10.02.2026 РКН замедляет Telegram.
   - С марта пишут, что `api.telegram.org` не отвечает с российских
     серверов и что «встали» в том числе системы мониторинга.
   - С нашего будущего сервера это не проверено.
   - Касается и уже сделанных уведомлений команде (П-5).
4. **Иностранные SaaS отпадают.**
   - Sentry закрыл доступ из России с 10.09.2024.
   - Договор Datadog прямо исключает Россию.
   - Elastic с 2022 не продаёт в Россию и закрывает загрузки для
     российских адресов.
   - Сверх этого — 152-ФЗ ст. 18 ч. 5 (с 01.07.2025): первичная запись
     ПДн в базы вне РФ запрещена, а в логах есть IP и идентификаторы.
5. **Реальных кандидатов два.**
   - (а) **Self-hosted**: Prometheus (или VictoriaMetrics) + Alertmanager +
     Grafana на отдельной ВМ.
   - (б) **Управляемый Yandex Monium** — платформа, в которую Яндекс
     объединил Monitoring, Managed Prometheus и логи. Алерты по SMS,
     звонку, push и почте отправляет инфраструктура Яндекса, а не наш
     сервер. На нашем объёме это десятки рублей в месяц (оценка, §4.1).
   - Zabbix, ELK/Graylog, Sentry self-hosted и SigNoz работают, но
     тяжелее, чем нужно одной ВМ.
6. **Предложение — три шага (§7).** Обновление 29.09: Яндекс —
   конкурент, поэтому на шаге 2 рекомендуем B, а не Monium (§7).
   - **Шаг 1** (1–2 дня, при любом выборе): проверки снаружи, «мёртвая
     рука» для cron и бэкапа, канал алертов не только через Telegram,
     ротация логов Docker.
   - **Шаг 2** (3–5 дней): метрики приложения в формате Prometheus и
     алерты. В Monium, если сервер будет в Yandex Cloud; иначе
     Prometheus + Alertmanager + Grafana на отдельной ВМ.
   - **Шаг 3** (2–3 дня, по потребности пилота):
     - продуктовая статистика из суточных агрегатов — переживает
       90-дневный срок `qa_log` и не обходит RLS;
     - централизованные логи;
     - трекер ошибок.

---

## 1. Что есть сейчас

| Что | Где | Что видит | Чего не видит |
|---|---|---|---|
| JSON-логи в stdout с `request_id`, секреты вырезаются | `core/logging.py`, `core/middleware.py` | всё, что залогировано: `unhandled_error`, `llm_retry`, `llm_call_failed`, `rate_limiter_unavailable`, `worker_loop_error`, `team_notify_failed`… | живут в `docker logs` на том же хосте. Ротации нет: в `compose.yaml` нет ключа `logging:`, а у драйвера `json-file` ротация по умолчанию выключена ([Docker](https://docs.docker.com/engine/logging/drivers/json-file/)). DEPLOY §10 требует хранить логи ≥ 90 дней во внешней системе — сейчас не выполняется |
| `GET /health` | `main.py` | процесс `api` отвечает | базу и Redis — намеренно: их сбой не повод перезапускать контейнер |
| HEALTHCHECK образа, пульс воркера (< 120 с) | `Dockerfile`, `compose.yaml`, `worker.py` | зависший процесс получает статус `unhealthy` | статус видит только Docker на хосте. Политика `restart: unless-stopped` срабатывает, когда процесс завершился, а не когда он стал `unhealthy` ([Docker](https://docs.docker.com/engine/containers/start-containers-automatically/)): зависший воркер так и останется висеть, и никто об этом не узнает |
| Бот в Telegram для команды | `services/team_notify.py` | заявка, исчерпан пул, остановлено подключение | отказы самой системы; доставка под вопросом (§3.2) |
| Журнал аудита | `audit_events`, `GET /api/v1/audit` | входы, отказы входа, повторное использование refresh-токена, кредиты | админ компании видит только свою компанию; команде — только через SQL |
| `qa_log` | `domain/models.py`, `QALog` | вопросы, источник ответа (`origin`), токены, кредиты, оценка | хранится 90 дней (`QA_LOG_RETENTION_DAYS`, `purge`); задержки ответа в нём нет; стоит под RLS, поэтому BI напрямую его не прочитает (§2.3) |
| `python -m corp_ed.stand check` | `stand.py` | сквозной сценарий с настоящей моделью | запускается только руками |

---

## 2. Что нужно видеть

### 2.1. Сценарии отказов

Раздел «следить за отказоустойчивостью» — это прежде всего вопрос, какие
отказы возможны и кто о них узнает первым.

| № | Что ломается | Что видит пользователь | Чем обнаружить | Кто заметит сейчас |
|---|---|---|---|---|
| 1 | ВМ недоступна: сбой хоста, зоны, сети | сайт не открывается | проверка снаружи; алерт на отсутствие данных | никто |
| 2 | nginx или TLS, истёк сертификат Let's Encrypt | ошибка сертификата | HTTPS-проверка снаружи + срок сертификата | никто |
| 3 | `api` упал или завис | 502 от nginx | HTTPS-проверка `/health` снаружи; счётчик перезапусков | Docker перезапустит упавший процесс, но не зависший |
| 4 | PostgreSQL недоступен или кончились соединения | 500 на всех ручках, кроме `/health` | доля 5xx; `pg_up`; проверка готовности с базой (§8, п. 4) | никто: `/health` остаётся зелёным |
| 5 | Redis недоступен | вход отвечает 503; лимиты FAQ и квота эмбеддингов открываются (RISKS №21) | `redis_up`; 503 на `/auth/login` | никто |
| 6 | Воркер завис или упал | документы не индексируются, синхронизация стоит | возраст пульса; глубина очередей `ingest_jobs` и `connector_sync_jobs` в `QUEUED` и возраст самой старой задачи | `unhealthy` в `docker ps` — без перезапуска и без алерта |
| 7 | Yandex Cloud: ошибки, 429, смена формата API (RISKS №23) | ошибка или отказ вместо ответа | доля `llm_call_failed` и ретраев, задержка; ночной `stand check` | запись в логе |
| 8 | Диск заполнен: база, логи Docker без ротации, дампы | встаёт всё; база может не подняться | свободное место < 15 %, прогноз заполнения | никто |
| 9 | Нехватка памяти или OOM (разбор PDF занимает все ядра — WORKLOG, №33) | медленно, 5xx | RAM, события OOM kill, загрузка CPU | никто |
| 10 | Cron `purge` или `gaps` не отработал (RISKS №22) | тихо: журнал не чистится по сроку (152-ФЗ), отчёт о пробелах устаревает | «мёртвая рука»: задача отмечается после успеха, алерт — если отметки нет | никто |
| 11 | Бэкап не снят или не вывезен (DEPLOY §8) | потеря суток данных превращается в потерю всего | «мёртвая рука» + размер дампа | никто |
| 12 | У компании остановилось подключение | документы устаревают | Telegram команде (уже есть) + метрика «подключений в `error`» | Telegram, если доходит |
| 13 | Подбор паролей, повторное использование refresh-токена | — | частота `auth.login.failed`, `auth.refresh.reuse_detected`, `account_locked` | аудит, только руками |
| 14 | Сломан сам канал оповещений | не узнаём ни о чём из списка выше | Watchdog — алерт, который горит всегда; внешний сервис поднимает тревогу, когда он пропал (§3.1) | — |

### 2.2. Метрики

Классическая схема — «четыре золотых сигнала» Google SRE: задержка,
трафик, ошибки, насыщение
([SRE Book, гл. 6](https://sre.google/sre-book/monitoring-distributed-systems/)).
Для нас:

| Слой | Метрики | Откуда |
|---|---|---|
| Снаружи | доступность `/health` и `/` по HTTPS, время ответа, срок сертификата | внешний чекер или blackbox_exporter |
| API | запросы по шаблону маршрута и коду; задержка p50/p95/p99; 5xx; 503 на `/auth/login`; 402 (пул исчерпан); 429 | middleware + `prometheus_client` |
| LLM и эмбеддинги | вызовы по модели и исходу, ретраи, задержка, токены, ожидание квоты | `llm/retry.py`, `llm/throttle.py`, шлюзы |
| Воркер | время последнего цикла; задачи обработано и упало; длительность извлечения; глубина очередей и возраст самой старой задачи | `worker.py`; SQL по `ingest_jobs` и `connector_sync_jobs` — они без RLS |
| PostgreSQL | доступность; соединения относительно `max_connections`; размер базы; долгие транзакции; deadlock | postgres_exporter |
| Redis | доступность, память, отклонённые соединения | redis_exporter |
| Хост и контейнеры | CPU, RAM, диск, OOM, перезапуски | node_exporter, cAdvisor |
| Регулярные задачи | время последнего успеха `purge`, `gaps` и бэкапа; размер дампа | «мёртвая рука»: отметка по окончании задачи |

**Метки метрик — без ПДн и без сырых путей.** Нельзя использовать e-mail,
`user_id`, текст вопроса или путь с идентификатором. Можно: шаблон
маршрута (`/api/v1/materials/{material_id}`), код ответа, код компании
(это не ПДн). Иначе нарушаем 152-ФЗ и получаем взрыв числа рядов.

### 2.3. Продуктовая статистика

Под «собирать статистику» мы понимаем следующие числа по каждой компании
за день:

- число вопросов;
- доли ответов по документам, общих ответов и отказов (`origin`);
- 👍 и 👎;
- кредиты против пула;
- активные сотрудники;
- документы `READY` и `FAILED`;
- пробелы.

Это вопрос П-1 из `OPEN-QUESTIONS.md` (решение 28.09 — «до пилота не
делать»; варианты: отчёт из CLI, микроопрос, страница метрик).

Код накладывает два ограничения:

- **RLS с `FORCE`** (`core/db_policies.py`). Роль без `BYPASSRLS`, не
  выставившая `app.tenant_id`, видит в `qa_log` ноль строк. BI-инструмент,
  подключённый к базе напрямую, либо не увидит ничего, либо потребует
  роль в обход третьего кольца изоляции. Второе недопустимо.
- **`qa_log` хранится 90 дней** (`purge`). Статистику за квартал и дольше
  из него не построить.

Вывод — суточные агрегаты.

- Ночная задача `cli stats` работает как `gaps --all`: проходит по
  компаниям с `app.tenant_id` и пишет суточные числа в отдельную таблицу.
- В таблице нет текстов и ПДн.
- BI читает только эту таблицу через отдельную роль с `SELECT` на неё.
- Агрегаты переживают `purge`.

---

## 3. Три условия, которые важнее выбора инструмента

### 3.1. Сторож не должен жить на охраняемом хосте

Сервер один (DEPLOY §1); боевого стенда пока нет (WORKLOG, этап 5). Всё,
что стоит на нём, при его падении замолчит. Поэтому нужны:

- **Проверка снаружи** — с другой ВМ (желательно в другой зоне) или из
  внешнего сервиса проверок.
- **«Мёртвая рука»** (dead man's switch) — алерт срабатывает не на
  сигнал, а на его отсутствие:
  - Cron и бэкап после успешного завершения отправляют отметку в
    Healthchecks или push-монитор Uptime Kuma/Gatus.
  - Сам мониторинг держит постоянно горящий алерт `Watchdog` (приём из
    kube-prometheus,
    [runbook](https://runbooks.prometheus-operator.dev/runbooks/general/watchdog/)).
    Его принимает внешний сервис и поднимает тревогу, когда алерт
    перестал приходить.
  - В Monium та же задача решается политикой алерта на отсутствие данных
    (`No data policy`: `Alarm`/`Warn`/…,
    [документация](https://yandex.cloud/ru/docs/monitoring/concepts/alerting/alert)).
    Алерт вычисляется на стороне Яндекса, поэтому смерть нашей ВМ он
    увидит.

### 3.2. Канал оповещений должен доходить

**Что известно о блокировке:**

- 10.02.2026 РКН подтвердил замедление Telegram по стране
  ([Википедия](https://ru.wikipedia.org/wiki/Блокировка_Telegram_в_России_(2026))).
- С середины марта пишут, что запросы к `api.telegram.org` с российских
  серверов уходят в таймаут. Среди пострадавших прямо названы системы
  мониторинга ([AffTimes, 19.03.2026](https://afftimes.com/news/oshibka-504-i-taimauty/),
  [vc.ru](https://vc.ru/id5779154/2795944-blokirovka-telegram-api-v-rossii)).
- Первоисточника (РКН, провайдер) нет, конкретные облака не названы; с
  Yandex Cloud не проверено.

**Что это значит для нас.** Под удар попадает и `team_notify` (П-5,
решение 28.09): на сервере в РФ бот может молчать, а в логе будет только
`team_notify_failed`.

| Канал алертов | Плюсы | Минусы |
|---|---|---|
| SMS, звонок, push в приложении Yandex Cloud (Monium) | не зависят ни от нашего сервера, ни от Telegram; есть эскалации ([каналы](https://yandex.cloud/ru/docs/monitoring/concepts/alerting/notification-channel)) | только в Monium; SMS и звонки платные |
| Почта (SMTP Яндекс 360 или Yandex Cloud Postbox) | поддерживается везде: Alertmanager, Grafana, Zabbix, GlitchTip | ночью заметят не сразу |
| MAX (Bot API) | российский мессенджер | во встроенных интеграциях Alertmanager и Grafana его нет — нужен свой адаптер на вебхуке; новый адрес `platform-api2.max.ru` требует добавить в доверенные сертификат Минцифры ([релизы клиента](https://github.com/max-messenger/max-bot-api-client-go/releases)) |
| Telegram через ретранслятор за рубежом | Alertmanager умеет подменять `api_url` и ходить через прокси ([конфигурация](https://prometheus.io/docs/alerting/latest/configuration/)) | зависимость от зарубежного VPS; в тексте алерта не должно быть ПДн |
| Telegram напрямую | уже настроен для команды | доставка не гарантирована |

**Правило:** алерт «сайт лежит» отправляется минимум по двум каналам, и
хотя бы один из них — не Telegram.

### 3.3. 152-ФЗ и санкции

**Логи считаем ПДн.**

- В логах `api` и nginx — IP клиента (именно его проверяет чек-лист
  DEPLOY §11), в аудите — `ip` и `user_id`.
- Суды расходятся в том, является ли IP персональными данными. Признали:
  9 ААС, А40-198848/2019, IP вместе с данными аккаунта
  ([обзор](https://152fz.cyberosnova.ru/blog/ip-adres-personalnye-dannye)).
  Не признали: 13 ААС, А56-75017/2014
  ([обзор](https://zarlaw.ru/lifehacks/articles/4-neveroyatnykh-fakta-o-personalnykh-dannykh/)).
- `user_id` сопоставляется с базой и косвенно указывает на человека.

**Что говорит закон.**

- Ст. 18 ч. 5 в редакции 23-ФЗ (с 01.07.2025): при сборе ПДн «запись,
  систематизация, накопление, хранение… с использованием баз данных,
  находящихся за пределами территории РФ, не допускаются»
  ([КонсультантПлюс](https://www.consultant.ru/document/cons_doc_LAW_61801/cbf4e15b7c330f9372e876cdf2bc928bad7950ef/)).
  Логи, отправленные в зарубежный SaaS, — это первичная запись за
  рубежом.
- Отдельно — уведомление РКН о трансграничной передаче (ст. 12,
  [КонсультантПлюс](https://www.consultant.ru/document/cons_doc_LAW_61801/e4ebbe1780de623c7cf32a59ca82a7bb523a25dd/)).

**Санкции.**

- США: с 12.09.2024 OFAC запрещает американским компаниям оказывать
  лицам в России облачные услуги для корпоративного ПО
  ([OFAC](https://ofac.treasury.gov/faqs/added/2024-06-12)).
- ЕС: ст. 5n(2b) Регламента 833/2014
  ([обзор](https://www.leadersleague.com/en/news/the-impact-of-article-5n-2b-of-regulation-833-2014-on-software-services-provided-to-russian-established-entities-12th-package)).
- Российские карты за рубежом не работают с марта 2022.

**Вывод:** хранилища метрик и логов — в РФ (свой сервер или российское
облако); в метках метрик и текстах алертов — никаких ПДн.

---

## 4. Варианты по слоям

### 4.1. Метрики и алерты

| | Prometheus + Alertmanager + Grafana | VictoriaMetrics + vmalert (+ Alertmanager, Grafana) | Zabbix | Netdata | Yandex Monium (Monitoring + Managed Prometheus) |
|---|---|---|---|---|---|
| Версия | Prometheus 3.15.0 (24.09.2026; LTS-ветка 3.13 поддерживается до 07.2027), Alertmanager 0.34.1, Grafana 13.2.3 | 1.153.0 (09.2026) | 7.0.31 LTS; 8.0 LTS в бете, релиз обещан в октябре 2026 | 2.11.1 | коммерческий запуск Monium — 04.03.2026 |
| Лицензия | Apache 2.0; Grafana — AGPLv3 | Apache 2.0; Enterprise платный (LTS-ветки — только в нём) | AGPLv3 с 7.0 | агент GPLv3+; новый дашборд закрытый (NCUL1), бесплатный | сервис |
| Как собирает | опрашивает `/metrics` и экспортёры (pull) | pull сам или через vmagent; принимает remote write | Zabbix agent 2 (плагины Docker, PostgreSQL, Redis) + шаблоны | агент на хосте, находит сервисы сам | Unified Agent (опрос `/metrics`, метрики Linux) или remote write из Prometheus/vmagent; метрики управляемых PostgreSQL и Valkey собираются сами и бесплатно |
| Ресурсы (данные вендора) | у Prometheus официального минимума нет: 1–2 байта на значение, хранение 15 дней по умолчанию; Grafana — от 512 МБ RAM и 1 ядра | «до 7× меньше RAM и диска, чем Prometheus»; хранение 1 месяц по умолчанию | минимальная конфигурация в официальной таблице — около 1 000 метрик на 2 vCPU / 8 ГБ; база PostgreSQL или TimescaleDB | 100–350 МБ RAM, 1–5 % ядра, около 4 ГБ диска | у нас — только агент |
| Язык запросов | PromQL | MetricsQL: совместим с PromQL, но есть отличия (например, `rate` без экстраполяции) | выражения триггеров | — | PromQL (Managed Prometheus) и язык Monitoring |
| Алерты | Alertmanager: Telegram, почта, вебхук и др. | vmalert сам не уведомляет — отправляет в Alertmanager | Telegram (вебхук-медиатип), почта | Telegram встроен | почта, SMS, push, Telegram, Cloud Functions, звонки; эскалации |
| Отказоустойчивость самого мониторинга | локальное хранилище не реплицируется; HA — два одинаковых Prometheus и кластер Alertmanager | две независимые копии, vmagent пишет в обе | активный и резервный сервер (с 6.0) | Parent-узлы с репликацией | реплика в 2 зонах; алерты вычисляются у Яндекса |
| Ограничения для РФ | у Prometheus не нашли; бинарники Grafana Enterprise отдают 451 `geofence:blocked`, OSS-сборки скачиваются | не нашли; компания основана в Киеве, штаб-квартира в США, OSS «доступен всем» | с 07.03.2022 не продаёт в РФ поддержку и обучение; о загрузках в заявлении не сказано | не нашли | российский сервис |
| Деньги в месяц | ВМ под мониторинг | ВМ | ВМ побольше (от 8 ГБ) | 0 — на той же ВМ | запись 0,32 ₽ за 1 млн значений, первые 50 млн в месяц через Prometheus Remote API бесплатно; алерты 1,5 ₽ за 1 000 алерто-часов |
| Сильная сторона | стандарт де-факто; больше всего готовых дашбордов и правил | тот же стек на меньшем железе | инфраструктура «из коробки», классическая эксплуатация | мгновенная картина одного хоста | нет своей инфраструктуры мониторинга; алерты отправляет не наш сервер |
| Слабая сторона для нас | 4–7 контейнеров и ВМ, которую надо обновлять | +vmalert и Alertmanager | метрики приложения — не его сильная сторона; тяжелее; через месяц — миграция на 8.0 | нет долгой истории; алерты уходят с того же хоста; бесплатное облако — до 5 узлов | привязка к Яндексу; сервис молодой; логи хранятся до 31 дня |

Источники: Prometheus — [релизы](https://github.com/prometheus/prometheus/releases),
[цикл выпусков](https://prometheus.io/docs/introduction/release-cycle/),
[хранилище](https://prometheus.io/docs/prometheus/latest/storage/);
Alertmanager — [загрузки](https://prometheus.io/download/),
[конфигурация](https://prometheus.io/docs/alerting/latest/configuration/);
Grafana — [релизы](https://github.com/grafana/grafana/releases),
[лицензия](https://grafana.com/licensing/),
[требования](https://grafana.com/docs/grafana/latest/setup-grafana/installation/),
[451 в РФ](https://community.grafana.com/t/cant-download-grafana-9-0-2-from-russia/68391);
VictoriaMetrics — [single-node](https://docs.victoriametrics.com/victoriametrics/single-server-victoriametrics/),
[MetricsQL](https://docs.victoriametrics.com/victoriametrics/metricsql/),
[vmalert](https://docs.victoriametrics.com/victoriametrics/vmalert/),
[LTS](https://docs.victoriametrics.com/victoriametrics/lts-releases/),
[заявление 2022](https://victoriametrics.com/blog/no-war/);
Zabbix — [релизы](https://www.zabbix.com/release_notes),
[дорожная карта](https://www.zabbix.com/roadmap),
[лицензия](https://www.zabbix.com/license),
[требования](https://www.zabbix.com/documentation/7.0/en/manual/installation/requirements),
[заявление о России](https://www.zabbix.com/pr/pr389);
Netdata — [ресурсы](https://learn.netdata.cloud/docs/netdata-agent/resource-utilization),
[лицензии](https://www.netdata.cloud/open-source/);
Monium — [документация](https://yandex.cloud/ru/docs/monium/),
[запуск](https://www.cnews.ru/news/line/2026-03-04_yandex_b2b_tech_zapustila_platformu),
[тарифы](https://yandex.cloud/ru/docs/monium/pricing),
[пример расчёта](https://github.com/yandex-cloud/docs/blob/master/ru/_pricing_examples/monitoring/rub-example.md),
[бесплатный уровень](https://yandex.cloud/ru/docs/billing/concepts/serverless-free-tier),
[Managed Prometheus](https://yandex.cloud/ru/docs/monitoring/operations/prometheus/),
[Unified Agent](https://github.com/yandex-cloud/docs/blob/master/ru/monitoring/concepts/data-collection/unified-agent/inputs.md).

**Оценка стоимости Monium на нашем объёме** (наша оценка, до замера):

- Метрики: около 2 000 рядов (хост, контейнеры, PostgreSQL, Redis,
  приложение), значение раз в 30 с. За месяц:
  2 000 × (30 × 24 × 3 600 / 30) ≈ 173 млн значений. Минус 50 млн
  бесплатных = 123 млн × 0,32 ₽ за 1 млн ≈ **40 ₽**.
- Алерты: 30 правил × 720 ч = 21 600 алерто-часов × 1,5 ₽ за 1 000 ≈
  **32 ₽**.
- Итого около **70 ₽/мес** плюс SMS и звонки.

Для сравнения, отдельная ВМ под self-hosted мониторинг (2 vCPU / 4 ГБ)
стоит порядка **2–3 тыс. ₽/мес**. Оценка сделана от цены 1 659 ₽ за 2 vCPU /
2 ГБ у агрегатора ([serverscan](https://serverscan.ru/providers/yandexcloud)):
калькулятор Yandex Cloud из среды сессии не открылся.

### 4.2. Логи

| | Версия, лицензия | Что ставить | Ресурсы | Хранение | Для РФ | Итог для нас |
|---|---|---|---|---|---|---|
| `docker logs` + ротация | — | ничего: ключ `logging:` в `compose.yaml` | диск хоста | пока хватает диска | — | обязательно при любом выборе, но DEPLOY §10 (≥ 90 дней вне хоста) не выполняет |
| **Loki + Alloy** | Loki 3.7.8 (AGPL-3.0); Alloy 1.20.1 (Apache 2.0). Promtail снят с поддержки 02.03.2026 и удалён в Loki 3.7.3 | Loki, Alloy (читает логи Docker через `loki.source.docker`), Grafana | официального минимума для монолита нет; монолит рассчитан на объём «примерно до 20 ГБ в сутки» | настраивается: compactor + `retention_period` | Enterprise-бинарники Grafana — 451; OSS доступен | хорош, если Grafana уже стоит |
| **VictoriaLogs** | 1.52.0, Apache 2.0; GA с ноября 2024 | один бинарник без зависимостей + сборщик (Vector, Fluent Bit или Alloy) | «до 30× меньше RAM и до 15× меньше диска», чем Loki и Elasticsearch (заявление вендора) | 7 дней по умолчанию; `-retentionPeriod`, лимит по диску | ограничений не нашли | самый лёгкий self-hosted вариант; алерты через vmalert → Alertmanager, есть плагин Grafana |
| OpenSearch | 3.8.0, Apache 2.0 | OpenSearch, Dashboards, сборщик | куча JVM 1 ГБ по умолчанию, рекомендуют половину RAM; `vm.max_map_count`; от 4 ГБ | ISM-политики | — | тяжело для одной ВМ |
| Elasticsearch | 9.5.4 | как OpenSearch | как OpenSearch | — | Elastic с 2022 не продаёт в РФ; `artifacts.elastic.co` отдаёт 403 на российские IP; реестр образов закрыт | не подходит |
| Graylog | 7.1.9; Open — SSPL | MongoDB, Data Node (OpenSearch не новее 2.19.6), Graylog | официального минимума нет | настраивается | не проверено | тяжело; SSPL |
| **Monium Logs** | сервис | приём по OTLP (`ingest.monium.yandex.cloud:443`) через OTel Collector или Unified Agent | у нас только сборщик | «до 31 дня» (страница сервиса) | российский сервис | дёшево: 4,40 ₽ за ГБ записи, чтение бесплатно. 31 день < 90 дней из DEPLOY §10 — нужен архив или пересмотр требования |
| Cloud Logging | сервис | — | — | — | — | **закрывается во II квартале 2027**, замена — Monium ([документация](https://yandex.cloud/ru/docs/logging/)); начинать с него не стоит |

Источники: Loki — [релизы](https://github.com/grafana/loki/releases),
[режимы развёртывания](https://grafana.com/docs/loki/latest/get-started/deployment-modes/),
[хранение](https://grafana.com/docs/loki/latest/operations/storage/retention/),
[Promtail](https://grafana.com/docs/loki/latest/send-data/promtail/);
Alloy — [релизы](https://github.com/grafana/alloy/releases);
VictoriaLogs — [документация](https://docs.victoriametrics.com/victorialogs/),
[журнал изменений](https://docs.victoriametrics.com/victorialogs/changelog/);
OpenSearch — [Docker](https://docs.opensearch.org/latest/install-and-configure/install-opensearch/docker/);
Elastic — [2022](https://www.elastic.co/blog/elastic-stands-with-ukraine),
[403 для РФ](https://discuss.elastic.co/t/why-artifacts-elastic-co-gpg-key-elasticsearch-returns-403-for-russian-ips-but-not-always/300969);
Graylog — [релизы](https://graylog.org/releases/),
[совместимость](https://go2docs.graylog.org/current/downloading_and_installing_graylog/compatibility_matrix.htm);
Monium Logs — [документация](https://yandex.cloud/ru/docs/monium/logs/),
[страница сервиса](https://yandex.cloud/ru/services/logging).

**Объём наших логов неизвестен.** Прикидка на пилот — меньше 1 ГБ в
месяц; это предположение, а не замер. При таком объёме цена хранения
ни в одном варианте роли не играет — важны число компонентов и
сопровождение.

### 4.3. Ошибки (трекер исключений)

Трекер группирует одинаковые исключения, показывает трассировку, когда
ошибка появилась впервые и когда повторилась, и присылает алерт при
регрессии. Сейчас вместо него — `unhandled_error` в логе с трассировкой.

| | Лицензия, версия | Требования | Для РФ | Итог |
|---|---|---|---|---|
| Sentry SaaS | — | — | с 10.09.2024 закрыт для России: платные аккаунты расторгнуты, доступ из РФ заблокирован ([vc.ru](https://vc.ru/services/1466517-servis-dlya-monitoringa-oshibok-v-kode-sentry-zablokiroval-rossiyanam-dostup-k-platforme)) | не подходит |
| Sentry self-hosted | 26.9.0, FSL-1.1 (каждый релиз через 2 года становится Apache 2.0) | 4 ядра, 16 ГБ RAM + 16 ГБ swap (рекомендуют 32 ГБ), 20 ГБ диска; 28 сервисов по умолчанию; профиль `errors-only` всё равно включает Kafka, ClickHouse и Snuba ([требования](https://develop.sentry.dev/self-hosted/)) | self-host не затронут | тяжелее самого продукта |
| **GlitchTip** | 6.2.6, MIT | 512 МБ RAM; PostgreSQL 14+; Valkey/Redis по желанию; около 30 ГБ диска на 1 млн событий в месяц ([установка](https://glitchtip.com/documentation/install)) | SaaS для РФ закрыт (Stripe), self-host — без ограничений | лёгкий; принимает Sentry SDK; алерты — только почта и вебхук |
| **Hawk** (CodeX, РФ) | SaaS; self-host — 15 сервисов | — | серверы в РФ, заявлено соответствие 152-ФЗ; бесплатно до 1 000 событий в месяц, платно от 99 ₽/мес ([hawk-tracker.ru](https://hawk-tracker.ru/)) | принимает Sentry SDK — меняется только DSN ([интеграции](https://docs.hawk.so/integrations)); алерты: почта, Telegram, Slack; собственный Python SDK не обновлялся с 01.2025 |

Код приложения во всех вариантах один: `sentry-sdk` (2.71.0, MIT).
Интеграция с FastAPI включается сама
([документация](https://docs.sentry.io/platforms/python/integrations/fastapi/)).
По умолчанию `send_default_pii=False` — заголовки, cookie и данные
пользователя не отправляются
([собираемые данные](https://docs.sentry.io/platforms/python/data-management/data-collected/)).
Переход между GlitchTip и Hawk — это смена DSN.

**Итог:** для MVP хватит алерта на `unhandled_error` по логам. GlitchTip
или Hawk — когда ошибок станет больше, чем удобно читать в логах.

### 4.4. Трассировка и платформы «всё в одном»

| | Версия, лицензия | Что внутри | Алерты |
|---|---|---|---|
| Jaeger v2 | 2.21.0, Apache 2.0 | один бинарник на базе OTel Collector; хранилище: память, Badger, OpenSearch, ClickHouse и др. | — |
| Grafana Tempo | 3.0.3, AGPL-3.0 | монолит без Kafka | через Grafana |
| SigNoz | 0.144.0, MIT (кроме `ee/`) | ClickHouse, ClickHouse Keeper, PostgreSQL, OTel Collector, SigNoz; от 4 ГБ RAM | Slack, вебхук, почта; **Telegram нет** |
| OpenObserve | 1.0.4, AGPL-3.0 | один бинарник | вебхук, почта |
| Uptrace | 2.0.3, AGPL-3.0 | ClickHouse, PostgreSQL, Redis | Telegram есть |
| ClickStack (HyperDX) | 2.39.1, MIT | ClickHouse, HyperDX, OTel Collector, MongoDB; образ «всё в одном» сам вендор не рекомендует для продакшена | — |

Источники: [Jaeger](https://github.com/jaegertracing/jaeger/releases),
[Tempo](https://grafana.com/docs/tempo/latest/release-notes/v3-0/),
[SigNoz](https://signoz.io/docs/install/docker/),
[OpenObserve](https://github.com/openobserve/openobserve),
[Uptrace](https://uptrace.dev/features/alerting),
[ClickStack](https://clickhouse.com/docs/use-cases/observability/clickstack/deployment/all-in-one).

**OpenTelemetry для Python.** Трассы и метрики стабильны, логи — ещё в
разработке ([статус](https://opentelemetry.io/docs/languages/python/)).
Инструментирование FastAPI, asyncpg, redis и httpx — 0.66b0, в статусе
beta ([PyPI](https://pypi.org/project/opentelemetry-instrumentation-fastapi/)).

**Итог: не сейчас.**

- У нас два процесса, а у запроса один внешний вызов (Yandex Cloud).
  Запрос в логах связывает `request_id`.
- Разложить задержку ответа на поиск, эмбеддинг и LLM дешевле
  гистограммами по этапам.
- Трассы окупятся, когда процессов или сервисов станет больше.

### 4.5. Внешняя доступность и «мёртвая рука»

| | Лицензия, цена | Что умеет | Каналы | Где стоит |
|---|---|---|---|---|
| blackbox_exporter | 0.28.0, Apache 2.0 | HTTP(S), TCP, срок сертификата — часть стека Prometheus | через Alertmanager | «снаружи» — только если стоит не на продуктовой ВМ |
| **Uptime Kuma** | 2.5.5, MIT | веб-интерфейс; HTTP, ключевое слово, сертификат; push-мониторы («мёртвая рука»); мониторы Docker | 90+ каналов, включая Telegram | своя ВМ; без HA |
| **Gatus** | 5.37.0, Apache 2.0 | конфиг в YAML (можно хранить в git); push-эндпоинты; отдаёт `/metrics` | Telegram и др. | своя ВМ; без HA |
| **Healthchecks** | 4.4, BSD-3 (self-host на PostgreSQL ≥ 15) или SaaS healthchecks.io | классическая «мёртвая рука» для cron | Telegram (нужен публичный HTTPS), почта и др. | SaaS в Латвии; ограничений для РФ не нашли (не проверено) |
| **Selectel «Мониторинг доступности»** | 3 проверки бесплатно, далее 52 ₽/мес за проверку | 18 типов проверок: HTTP, TCP, PostgreSQL… | почта, SMS, HTTP | российский сервис |
| **Ping-Admin** | за проверку: 0,0004 $ за HTTP(S) | проверки из разных точек | звонок, SMS, Telegram, MAX и др. | российский сервис |
| Host-tracker | есть бесплатный тариф | HTTP и др. | 14 каналов, включая Telegram | — |
| Яндекс Метрика: «мониторинг доступности» | — | **закрыт 03.09.2024** ([ppc.world](https://ppc.world/news/metrika-priostanovila-dostup-k-monitoringu-sayta/)) | — | — |

Источники: [Uptime Kuma](https://github.com/louislam/uptime-kuma/releases),
[Gatus](https://github.com/TwiN/gatus),
[Healthchecks](https://github.com/healthchecks/healthchecks),
[Selectel](https://selectel.ru/services/additional/monitoring/),
[Ping-Admin](https://ping-admin.com/texts/3.html),
[Host-tracker](https://www.host-tracker.com/ru/).
Синтетических HTTP-проверок в Monium мы не нашли.

**Итог.** Внешний чекер ставим не на нашу ВМ. Два пути:

- Selectel или Ping-Admin — своей инфраструктуры не нужно.
- Uptime Kuma или Gatus на ВМ мониторинга в другой зоне.

Во внешний чекер уходят только адрес и код ответа, ПДн туда не попадают.

### 4.6. Продуктовая статистика (BI)

| | Лицензия, цена | Требования | Итог |
|---|---|---|---|
| Grafana, источник PostgreSQL | встроен ([документация](https://grafana.com/docs/grafana/latest/datasources/postgres/)) | 0 новых компонентов, если Grafana уже есть | алерты по SQL — только для формата временного ряда |
| Yandex DataLens (облако) | первое место бесплатно, далее 990 ₽ за место в месяц ([тарифы](https://yandex.cloud/ru/docs/datalens/pricing)) | — | удобно, если статистику смотрят не разработчики |
| DataLens open source | Apache 2.0 с 26.09.2023 | docker compose, от 4 ГБ RAM и 2 CPU; Highcharts — отдельная коммерческая лицензия ([GitHub](https://github.com/datalens-tech/datalens)) | тяжеловат ради одной таблицы |
| Metabase OSS | AGPL | от 1 CPU / 1 ГБ + своя PostgreSQL для метаданных ([в продакшене](https://www.metabase.com/learn/metabase-basics/administration/administration-and-operation/metabase-in-production)) | лишний сервис |
| Apache Superset | Apache 2.0 | docker compose для продакшена «не поддерживается», нужен Kubernetes ([документация](https://superset.apache.org/docs/installation/docker-compose/)) | не для нас |
| Отчёт из CLI (П-1, вариант а) | — | 0 компонентов | минимум: CSV по запросу |

**Итог:** сначала агрегаты (§2.3), а смотреть их — через Grafana или CLI.
Облачный DataLens — если статистику будет смотреть владелец продукта, а
не команда.

### 4.7. Иностранные SaaS — почему мы их не рассматриваем

| Сервис | Статус для РФ |
|---|---|
| Datadog | MSA §10.2(b): сервисы не оказываются «лицам в России или Беларуси» ([MSA](https://www.datadoghq.com/legal/msa/)) |
| Sentry | закрыт с 10.09.2024 (§4.3) |
| Elastic Cloud | 451, продажи в РФ прекращены в 2022 |
| Grafana Cloud | условия запрещают оказывать услуги в страны под санкциями США ([ToS](https://grafana.com/legal/terms/)); явную блокировку облака не проверяли |
| New Relic | запрет на «Sanctions Target» ([условия](https://newrelic.com/termsandconditions/paid)); Россия прямо не названа (не проверено) |
| Better Stack, UptimeRobot, healthchecks.io | в условиях Россия не упоминается, сообщений о блокировке не нашли (не проверено) |

Общие барьеры: оплата, 152-ФЗ ст. 18 ч. 5 для логов, риск отключения без
предупреждения. Допустимое исключение — внешний чекер или «мёртвая рука»
без ПДн (только URL и код ответа), и то если решён вопрос оплаты.

---

## 5. Как это устроено у других

**Данные опроса.** Опрос Grafana Labs 2026: 1 363 ответа, собраны с
10.2025 по 01.2026.

- В Prometheus инвестируют 77 % опрошенных, в OpenTelemetry — 76 %, в оба
  сразу — 65 %.
- OpenTelemetry растёт быстрее: пилоты и изучение — 35 % против 18 % у
  Prometheus.

Источники: [Grafana](https://grafana.com/press/2026/03/18/grafana-labs-4th-annual-observability-survey-reveals-a-field-at-a-crossroads-ai-economics-complexity-and-the-enduring-power-of-open-source/),
[OpenTelemetry](https://opentelemetry.io/blog/2026/otel-prometheus-interoperability/).
Оговорка: опрос проводил вендор Grafana, выборка смещена к его
пользователям и к крупным компаниям. Данных о малых российских SaaS мы
не нашли.

**Устойчивые сборки** (это практика, а не статистика):

1. **«PLG»** — Prometheus + Loki + Grafana (+ Alertmanager). Стандарт для
   контейнеров, больше всего готовых дашбордов и правил.
2. **VictoriaMetrics + VictoriaLogs + Grafana** — тот же подход на
   меньшем железе.
3. **Zabbix для инфраструктуры + отдельный трекер ошибок** — у компаний
   с классической эксплуатацией; в РФ очень распространён.
4. **Мониторинг облачного провайдера** — когда сервер у провайдера:
   - Yandex Monium;
   - VK Cloud Monitoring — бесплатно
     ([тарификация](https://cloud.vk.ru/docs/ru/monitoring-services/monitoring/tariffication));
   - Cloud.ru Monitoring — со встроенным Alertmanager
     ([страница](https://cloud.ru/products/cloud-monitoring)).
5. **OTel-платформа «всё в одном»** (SigNoz, OpenObserve, Uptrace) —
   когда нужны трассы.

**Общее правило при одном сервере:** хотя бы одна проверка снаружи и
канал алертов, который не зависит от этого сервера.

---

## 6. Сборки под нас

**A. Минимум: «узнать, что лежит».**

- Внешний чекер проверяет `/health`, `/` и срок сертификата. Варианты:
  Selectel (3 проверки бесплатно), Ping-Admin или Uptime Kuma на другой
  ВМ.
- Глубокая проверка с базой и Redis (§8, п. 4).
- «Мёртвая рука» (healthchecks.io или self-host) для `purge`, `gaps` и
  бэкапа.
- Канал алертов не только через Telegram.
- Ротация логов Docker.

Истории метрик и статистики нет.

**B. Self-hosted: Prometheus + Alertmanager + Grafana на отдельной ВМ в
другой зоне.**

- На продуктовой ВМ: node_exporter, cAdvisor, postgres_exporter,
  redis_exporter и `/metrics` у `api` и `worker`. Порты открыты только во
  внутреннюю сеть облака.
- На ВМ мониторинга:
  - Prometheus, Alertmanager, Grafana;
  - Uptime Kuma или blackbox_exporter — это и есть внешняя проверка
    продукта.
- Watchdog уходит во внешнюю «мёртвую руку» — так мониторинг
  контролируется снаружи.
- Логи: VictoriaLogs или Loki + сборщик (+1 день).
- Статистика: Grafana по агрегатам.

**B′. То же на VictoriaMetrics:** vmsingle + vmalert + Alertmanager +
Grafana + VictoriaLogs. Меньше RAM и диска, но на один компонент больше
(vmalert) и MetricsQL вместо PromQL. Имеет смысл, если ВМ мониторинга
маленькая или метрик станет много.

**C. Управляемый Yandex Monium.**

- На продуктовой ВМ агент снимает `/metrics`, экспортёры и метрики Linux
  и отправляет их в Managed Prometheus. Агент — Unified Agent, OTel
  Collector или Prometheus в режиме agent.
- Алерты Monium: SMS, звонок, push, почта, эскалации. Алерт на
  отсутствие данных ловит смерть ВМ.
- Логи: по OTLP в Monium Logs (31 день) + архив.
- Дашборды: в Monium или в Grafana через PromQL.
- Внешний чекер: Selectel или Ping-Admin.
- Статистика: DataLens или Grafana.

**D. Zabbix 7.0 LTS.**

- Сервер, веб-интерфейс и PostgreSQL на ВМ мониторинга — от 8 ГБ по
  официальной таблице.
- Agent 2 на продуктовой ВМ, шаблоны Docker, PostgreSQL, Redis, Nginx.
- Метрики приложения — через HTTP-агент с разбором формата Prometheus.
- Уведомления: Telegram-вебхук, почта.

**E. SigNoz (OpenTelemetry).** Метрики, логи и трассы в одном интерфейсе
на ClickHouse.

| | A. Минимум | B. Prometheus + Grafana | B′. VictoriaMetrics | C. Yandex Monium | D. Zabbix | E. SigNoz |
|---|---|---|---|---|---|---|
| Сценарии §2.1 | 1–5, 10, 11, 14 | все | все | все (1 — отсутствием данных + внешним чекером) | все; метрики приложения слабее | все |
| Новые компоненты у нас | 0–1 | 4 экспортёра + ВМ с 3–5 сервисами | как B, +vmalert | агент + 4 экспортёра | agent 2 + ВМ с 3 сервисами | 5 сервисов (ClickHouse) + сборщик |
| Ресурсы | — | ВМ 2 vCPU / 4 ГБ (оценка: Grafana ≥ 512 МБ по вендору, остальное — сотни МБ) | ВМ 2 vCPU / 2–4 ГБ (оценка) | только агент | ВМ от 8 ГБ (таблица Zabbix) | от 4 ГБ (вендор) |
| Деньги в месяц | 0–200 ₽ | ≈ 2–3 тыс. ₽ за ВМ (оценка) | ≈ 2 тыс. ₽ (оценка) | ≈ 70 ₽ + SMS + логи по 4,40 ₽/ГБ (оценка, §4.1) | ≈ 4–5 тыс. ₽ (оценка) | ≈ 2–3 тыс. ₽ (оценка) |
| Трудозатраты (наша оценка) | 1–2 дня | 4–6 дней | 4–6 дней | 3–5 дней | 4–6 дней + миграция на 8.0 | 5–7 дней |
| Сопровождение | почти нет | обновления, диск, бэкап конфигов: 2–4 ч/мес | как B | почти нет | как B, больше | как B, больше (ClickHouse) |
| Алерт при смерти нашей ВМ | да (внешний чекер) | да (ВМ мониторинга отдельно) | да | да (на стороне Яндекса) | да | да, если на отдельной ВМ |
| Алерт при смерти ВМ мониторинга | — | через Watchdog во внешний сервис | через Watchdog | не наша забота | через внешний сервис | через внешний сервис |
| Логи ≥ 90 дней | нет | да (настраивается) | да | 31 день + архив | нет (не для логов) | да |
| Привязка к поставщику | нет | нет | нет | к Яндексу (смягчается: код отдаёт стандартный формат Prometheus) | к Zabbix | к SigNoz (смягчается: OTel) |
| Риски для РФ | оплата внешнего чекера | Docker Hub и загрузки Grafana (§10) | Docker Hub | нет | нет | Docker Hub |

---

## 7. Предложение

> **Обновление 29.09, позже.** Яндекс стал прямым конкурентом
> («Алиса AI для бизнеса»). Метрики в Monium — это коды компаний-клиентов,
> их объёмы и рост, то есть коммерческая разведка для конкурента. Поэтому
> **вариант C отпадает, рекомендуем B.**
>
> - SMS и звонки при падении сайта берём у сервиса проверок без
>   Яндекса: Ping-Admin (звонок, SMS, MAX) или Selectel (SMS).
> - Этот же сервис снаружи проверяет ВМ мониторинга.
>
> Хостинг и отказоустойчивость ML — в `RESILIENCE-RESEARCH.md`.

**Шаг 1 — при любом выборе, 1–2 дня (сборка A).**

- Внешний чекер проверяет `/health`, `/health/ready` (новая, §8, п. 4) и
  `/`, а также срок сертификата.
- «Мёртвая рука» для `purge`, `gaps` и бэкапа.
- Ротация логов Docker.
- Проверка `api.telegram.org` с сервера и второй канал алертов.

Это закрывает сценарии, в которых мы сейчас узнаём о проблеме от
клиента.

**Шаг 2 — метрики и алерты, 3–5 дней.**

- Код одинаков для любого варианта (§8, п. 1–3, 8): стандартный
  `/metrics` в формате Prometheus.
- Куда отправлять:
  - **Сервер в Yandex Cloud и команда не хочет держать ещё одну ВМ →
    C (Monium).** Алерты отправляет инфраструктура Яндекса по SMS или
    звонку, отдельной ВМ нет, цена — десятки рублей.
  - **Сервер не в Yandex Cloud, или нужна независимость от поставщика,
    или логи ≥ 90 дней «из коробки» → B** (или B′, если ВМ мониторинга
    маленькая).
  - **Zabbix (D)** — только если в команде есть человек, который уже его
    эксплуатирует.
  - **SigNoz (E)** — когда понадобятся трассы.
- Первый набор алертов — §9.

**Шаг 3 — по потребности пилота, 2–3 дня.**

- Суточные агрегаты статистики (§2.3) и дашборд.
- Централизованные логи: VictoriaLogs или Loki (при B), Monium Logs
  (при C).
- Трекер ошибок (GlitchTip или Hawk) — если ошибок станет много.

**SLO на пилот.** Предлагаем 99,5 % доступности в месяц. Доли простоя на
30 дней (43 200 мин):

- 99 % — 7,2 ч;
- 99,5 % — 3,6 ч;
- 99,9 % — 43 мин.

Одна ВМ с ежедневным бэкапом реалистично держит 99–99,5 %. Если пилотный
клиент попросит 99,9 %, мониторинг этого не даст. Понадобятся вторая ВМ
и управляемая база с репликой, а это уже решение об архитектуре, а не о
мониторинге.

**Что изменит оценку:**

1. Ответ `api.telegram.org` с сервера. Если отвечает, Telegram годится
   как второй канал, но не как единственный.
2. Где будет сервер.
3. Реальный объём метрик и логов после первой недели пилота.
4. Требования клиентов к SLA.

---

## 8. Что поменять в коде и деплое (при любом выборе)

1. **Метрики API.**
   - Инструмент: `prometheus_client` 0.26.0 или
     `prometheus-fastapi-instrumentator` 8.1.0
     ([PyPI](https://pypi.org/project/prometheus-fastapi-instrumentator/)).
   - Отдавать на отдельном порту внутри сети compose, а не через основной
     порт. Так метрики не проходят через `TrustedHostMiddleware` и не
     попадают в nginx (в `kronto.conf` на API идут только `/api/` и
     `/health`). Порт наружу не публиковать.
   - Метки — шаблон маршрута.
   - uvicorn работает одним процессом, поэтому multiprocess-режим не
     нужен. При переходе на `--workers` понадобятся `PROMETHEUS_MULTIPROC_DIR`
     и его ограничения: нет Info/Enum, нет метрик процесса, чистка
     каталога при старте ([документация](https://prometheus.github.io/client_python/multiprocess/)).
2. **Метрики воркера.** Тот же `start_http_server`. Что отдавать:
   - время последнего цикла (заменяет файл-пульс для внешнего наблюдения);
   - счётчики задач;
   - длительность извлечения;
   - глубину и возраст очередей — запрос раз в 30 с к `ingest_jobs` и
     `connector_sync_jobs`, они без RLS.
3. **Метрики LLM.** В `llm/retry.py` и шлюзах — исход вызова, ретраи,
   задержка, токены; модель — меткой. В FAQ — гистограммы по этапам:
   эмбеддинг, поиск, ответ модели.
4. **Проверка готовности `GET /health/ready`.**
   - `SELECT 1` к базе и `PING` к Redis с таймаутом 1–2 с.
   - В ответе только статус, без деталей.
   - Нужна внешнему чекеру; `/health` не трогать — он для HEALTHCHECK
     Docker.
   - Маршрут добавить в `deploy/nginx/kronto.conf`.
5. **Ротация логов в `compose.yaml`.** Для всех сервисов:
   `logging: {driver: json-file, options: {max-size: "50m", max-file: "5"}}`
   (или драйвер `local`).
6. **Cron и бэкап.** После успешного завершения —
   `&& curl -fsS -m 10 --retry 3 <адрес отметки>`. Для бэкапа добавить
   ещё и размер дампа.
7. **Агрегаты статистики.** Таблица `usage_daily` без ПДн; `cli stats`
   ночью после `gaps`; роль `corp_ed_stats` с `SELECT` только на эту
   таблицу.
8. **Экспортёры.** Отдельный `compose.monitoring.yaml`, подключается
   через `-f`: node_exporter, cAdvisor, postgres_exporter (роль с
   `pg_monitor`, не владелец схемы), redis_exporter.
9. **Telegram.** До пилота проверить с сервера
   `curl -m 10 -sS -o /dev/null -w '%{http_code}' https://api.telegram.org`.
   При таймауте нужен второй канал и для `team_notify`.

---

## 9. Первый набор алертов (предложение)

| Алерт | Условие | Важность | Канал |
|---|---|---|---|
| Сайт недоступен снаружи | 2 неудачные проверки подряд (интервал 1 мин) хотя бы из 2 точек | critical | SMS или звонок + почта |
| `/health/ready` не проходит | 3 минуты подряд | critical | SMS или звонок + почта |
| Сертификат истекает | < 14 дней | warning | почта |
| Доля 5xx | > 2 % за 5 мин при ≥ 20 запросах | critical | SMS + почта |
| 503 на `/auth/login` (Redis, RISKS №21) | > 0 за 5 мин | critical | SMS + почта |
| Задержка ответа FAQ | p95 > 10 с в течение 10 мин (на прогоне 28.09 ответы шли 0,8–2,3 с) | warning | почта |
| Ошибки LLM | доля неудачных вызовов > 10 % за 10 мин | critical | SMS + почта |
| Воркер | последний цикл старше 2 мин | critical | SMS + почта |
| Очередь | самая старая задача в `QUEUED` старше 15 мин | warning | почта |
| Диск | свободно < 15 % — warning; < 5 % или прогноз заполнения < 24 ч — critical | warning / critical | почта / SMS |
| Память | > 90 % в течение 10 мин или OOM kill | warning | почта |
| PostgreSQL | недоступен — critical; соединений > 80 % от `max_connections` — warning | critical / warning | SMS / почта |
| Redis | недоступен | critical | SMS + почта |
| Контейнер | > 3 перезапусков за 15 мин или `unhealthy` дольше 5 мин | warning | почта |
| `purge`, `gaps` | нет отметки > 26 ч | warning | почта |
| Бэкап | нет отметки > 26 ч или дамп меньше половины предыдущего | critical | SMS + почта |
| Неудачные входы | всплеск `auth.login.failed` по сервису (порог — после недели наблюдений) | warning | почта |
| Watchdog | пропал | critical | внешний сервис → SMS |

Пороги — стартовые. Первую неделю пилота алерты уровня warning только
пишутся в журнал, а пороги подбираются по фактическим данным.

---

## 10. Что осталось непроверенным

- **Доступность `api.telegram.org`** из Yandex Cloud и с будущего
  сервера. Первоисточника о блокировке API нет — только отраслевые СМИ и
  жалобы пользователей.
- **Канал Telegram в самом Monium:** работает ли он в 2026 году.
- **Цены:**
  - Monium сверх примера из документации (таблицы на странице
    подгружаются динамически);
  - Cloud Logging;
  - ВМ под мониторинг — цифра от агрегатора, не из калькулятора Yandex
    Cloud.
- **Хранение Monium Logs «до 31 дня»** — взято со страницы сервиса, не из
  документации; можно ли хранить дольше — не выяснили.
- **Синтетические HTTP-проверки в Monium** — не нашли; это не значит, что
  их нет.
- **Загрузки из РФ:**
  - блокирует ли Grafana установку плагинов с grafana.com;
  - что сейчас покрывает зеркало `cr.yandex/mirror`;
  - доступен ли Docker Hub (блокировался 30.05–03.06.2024, по вторичным
    источникам:
    [The Record](https://therecord.media/docker-hub-suspends-services-russia)).
- **Ресурсы Prometheus, Loki и VictoriaLogs на нашем объёме.**
  Официальных минимумов нет; «в N раз меньше» — заявления вендоров.
- **Около 2 000 рядов метрик** — наша оценка до замера.
- **Позиция РКН по IP как ПДн** — известна только из вторичных
  источников (сайт РКН отвечал 503).
- **265-ФЗ от 26.07.2026** (изменения ст. 12): меняет ли он процедуру
  уведомления о трансграничной передаче.
- **Россия в условиях сервисов.** Явных блокировок у New Relic, Grafana
  Cloud, Better Stack, UptimeRobot и healthchecks.io не нашли; это не
  значит, что их нет.
- **Версии с GitHub без года в дате** (Gatus 5.37.0, Healthchecks 4.4)
  отнесены к 2026 году: GitHub не показывает год для дат текущего года.

---

## 11. Решения команды

Пока не приняты. Вопросы, от которых зависит выбор:

1. Где будет боевой сервер: Yandex Cloud или другой? От этого зависит
   выбор между C и B.
2. Готовы ли держать вторую ВМ под мониторинг?
3. Какой канал будит ночью (SMS или звонок, почта, MAX, Telegram через
   ретранслятор) и кто дежурит?
4. Целевой SLO на пилот — 99,5 %?
5. Остаётся ли требование хранить логи 90 дней (DEPLOY §10)?
6. Агрегаты статистики — сейчас (история дольше 90 дней копится только с
   момента запуска) или после пилота (П-1)?
