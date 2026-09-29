# Тестовый стенд

Сервер, на котором приложение работает в боевой форме: `compose.yaml` в
`ENVIRONMENT=production`, TLS-прокси по `deploy/nginx/kronto.conf`, cron,
бэкап. Выкатка идёт из GitHub Actions. На стенде проверяем деплой, CI/CD,
pgvector, миграции, коннекторы и настоящую модель — до того, как появится
клиент. Боевой деплой и его чек-лист описаны в `DEPLOY.md`, выбор боевого
хостинга — в `RESILIENCE-RESEARCH.md` §3.

---

## 1. Сервер

**Timeweb Cloud, Москва, Ubuntu 24.04, конфигурация Cloud-80: 4 vCPU,
8 ГБ RAM, 80 ГБ NVMe** — 1 800 ₽/мес. Это цена со страницы тарифов, где
указана скидка 10 % за оплату на 12 месяцев; почасовая оплата тоже есть
([Timeweb](https://timeweb.cloud/services/cloud-servers)).

**Почему такой размер.** На сервере работают PostgreSQL, `api`, `worker`,
Redis, `web` и nginx. Самое тяжёлое — разбор PDF: песочница берёт до
1,5 ГБ (`ingest/extract_worker.py`), а модель разметки занимает все ядра
(WORKLOG, №33). Cloud-50 (2 vCPU / 4 ГБ, 1 080 ₽) формально вместит
систему, но без запаса под параллельную загрузку документов и агенты
мониторинга. Разница — 720 ₽ в месяц.

**Почему Timeweb, если для боя мы советуем Selectel.**

- На стенде нет клиентских данных, и отказоустойчивость ему не нужна.
- Он втрое дешевле.
- Стек переносим: переезд на другой хостинг — это `bootstrap.sh` на новом
  сервере.
- Если бой будет в Selectel, стенд стоит перенести туда до пилота, чтобы
  поймать особенности именно этого провайдера.
- Если с сервера не открывается Docker Hub, у Timeweb есть зеркало:
  `REGISTRY_MIRROR=https://dockerhub.timeweb.cloud` в `bootstrap.sh`.

---

## 2. Как устроено

```
push в main ─► CI (тесты, e2e, образы) ─► Deploy
                                           ├─ images: сборка по проверенному коммиту
                                           │   → ghcr.io/stepankovner/corp-ed:<sha>, kronto-web:<sha>
                                           └─ deploy: ssh deploy@стенд "deploy <sha>"

стенд:  corp-ed-deploy (deploy/stage/ssh-entry.sh)
          коммит есть в ветке origin? → git checkout <sha>
          → deploy/stage/deploy.sh: pull → compose up (migrate → api, worker, web; db, redis)
          → все healthy → https://<стенд>/health через nginx
        nginx: TLS (Let's Encrypt); /api и /health → 127.0.0.1:8000, остальное → 127.0.0.1:8080
        cron: 03:10 purge, 03:30 gaps --all, 04:00 pg_dump (хранится 7 дней)
```

- **Где собираются образы.** Сервер ничего не собирает: образы строит
  workflow по тому коммиту, который прошёл CI. На сервере нет npm и uv,
  а на стенде — ровно те образы, что проверены.
- **Что можно сделать ключом выкатки.** Ключ GitHub Actions на сервере
  привязан к одной команде — `corp-ed-deploy` (`command=` в
  `authorized_keys`): ни shell, ни проброса портов. Выкатить можно только
  коммит из ветки этого репозитория. Коммиты форков GitHub тоже отдаёт
  по SHA, поэтому они отсекаются отдельной проверкой.
- **Имена образов.** Скачанные образы получают локальные имена из
  `compose.yaml` (`corp-ed:local`, `kronto-web:local`). Поэтому
  `compose.yaml` на стенде тот же, что в разработке и в бою.

---

## 3. Первый запуск (около часа)

0. **Слить ветку со скриптами в `main`.** `bootstrap.sh` берёт код из
   `main`, а GitHub запускает `workflow_run` и показывает кнопку Run
   workflow только для workflow из ветки по умолчанию.
1. **Заказать сервер.** Timeweb Cloud → Облачные серверы → Москва →
   Ubuntu 24.04 → Cloud-80. Сразу добавить свой SSH-ключ.
2. **DNS.** A-запись `stage.krontoai.ru` → IP сервера. Проверить:
   `dig +short stage.krontoai.ru`.
3. **Ключ выкатки** — на своей машине:
   ```bash
   ssh-keygen -t ed25519 -N '' -C corp-ed-stage-deploy -f corp-ed-stage-deploy
   ```
4. **Настроить сервер** — под root, один раз. SSH должен слушать порт 22:
   ufw открывает только его, 80 и 443.
   ```bash
   curl -fsSLO https://raw.githubusercontent.com/stepankovner/corp-ed/main/deploy/stage/bootstrap.sh
   DOMAIN=stage.krontoai.ru LETSENCRYPT_EMAIL=<почта команды> \
   DEPLOY_PUBKEY="<содержимое corp-ed-stage-deploy.pub>" bash bootstrap.sh
   ```
   Что делает скрипт:
   - ставит Docker и compose из архива Ubuntu с ротацией логов, swap 4 ГБ,
     nginx, certbot, ufw, автообновления безопасности;
   - заводит пользователя `deploy` с ключом выкатки;
   - кладёт код в `/opt/corp-ed` и генерирует `.env`
     (`deploy/stage/make-env.sh`);
   - выпускает сертификат Let's Encrypt;
   - ставит боевой конфиг nginx с именем стенда и cron.

   В конце скрипт печатает строку для `STAGE_SSH_KNOWN_HOSTS`. Повторный
   запуск безопасен — так же обновляются конфиг nginx и cron, когда они
   меняются в репозитории.
5. **Ключи Yandex Cloud.** Вписать `YC_FOLDER_ID` и `YC_API_KEY` в
   `/opt/corp-ed/.env`. Остальные секреты скрипт сгенерировал сам.
   Для стенда лучше отдельный каталог и сервисный аккаунт: своя квота и
   видно, сколько тратит стенд.
6. **GitHub** — настройки репозитория:
   - Environments → New environment `stage` → секрет `STAGE_SSH_KEY`:
     содержимое приватного `corp-ed-stage-deploy`. По желанию — правило
     «Required reviewers», тогда каждую выкатку кто-то подтверждает.
   - Secrets and variables → Actions → Variables:
     - `STAGE_ENABLED` = `true`;
     - `STAGE_HOST` = `stage.krontoai.ru`;
     - `STAGE_DOMAIN` = `stage.krontoai.ru`;
     - `STAGE_SSH_KNOWN_HOSTS` = строка из шага 4.
7. **Первая выкатка:** Actions → Deploy → Run workflow (`main`).
   - Пакеты GHCR при первой публикации приватные, поэтому первая выкатка
     остановится на `docker pull`.
   - Нужно открыть их: профиль → Packages → `corp-ed` и `kronto-web` →
     Package settings → Change visibility → Public, затем Re-run. Репозиторий
     и так публичный, секретов в образах нет (`.dockerignore` исключает
     `.env`).
   - Другой путь — на сервере под `deploy` выполнить `docker login ghcr.io`
     с токеном, у которого только `read:packages`.
8. **Компания и проверка:**
   ```bash
   cd /opt/corp-ed
   sudo -u deploy docker compose -f compose.yaml run --rm --no-deps api \
       python -m corp_ed.cli create-tenant --code stage --name "Стенд" --seats 5 \
       --admin-email admin@krontoai.ru
   ```
   Затем с любой машины: `CORP_ED_BASE_URL=https://stage.krontoai.ru …
   python -m corp_ed.stand check` (DEPLOY.md §4) и пункты чек-листа
   DEPLOY §11, которые относятся к стенду.

---

## 4. Каждый день

- **`main`** выкатывается сам после зелёного CI.
- **Ветка:** Actions → Deploy → Run workflow → выбрать ветку.
  Осторожно с миграциями в ветках: если ветка добавила ревизию, а потом
  выкатывают `main` без неё, `migrate` упадёт — база стоит на неизвестной
  `main` ревизии. Перед возвратом на `main`: `docker compose -f compose.yaml
  run --rm migrate alembic downgrade <ревизия main>`.
- **Откат** на прошлый коммит `main` (его образы уже лежат в GHCR):
  ```bash
  sudo -u deploy SSH_ORIGINAL_COMMAND="deploy <sha>" /usr/local/bin/corp-ed-deploy
  ```
  Миграции сами не откатываются — см. предыдущий пункт.
- **Логи:**
  - сервисы: `docker compose -f compose.yaml logs -f api worker`;
  - история выкаток: `/var/log/corp-ed/deploys.log`;
  - cron: `/var/log/corp-ed/cron.log`.
- **База:** `docker compose -f compose.yaml exec db psql -U corp_ed corp_ed`.

---

## 5. Правила стенда

- **Только тестовые данные**, никаких документов клиентов, и вот почему:
  - Yandex AI Studio по умолчанию сохраняет запросы (RISKS №48), пока в
    код не добавлен `x-data-logging-enabled: false`;
  - сервер — обычный, не в аттестованном сегменте по 152-ФЗ;
  - бэкапы лежат только на самом сервере.
- **Приём заявок** на созвон выключен (`LEADS_ENABLED` по умолчанию `false`).
- **Telegram.** Стенд — то место, где стоит проверить
  `curl -m 10 https://api.telegram.org` с российского сервера (RISKS №50)
  до того, как включать `TEAM_NOTIFY_TELEGRAM_*`.
- **Доступ по SSH** — ключи администраторов (root) и ключ выкатки
  (`deploy`, только выкатка). Пароль для SSH на стенде лучше выключить
  (`PasswordAuthentication no`), когда ключ администратора проверен.

---

## 6. Что проверено (29.09, в среде сессии)

**Выкатка целиком, на имитации сервера:**

- образы собраны по коммиту `99bb17d` и положены в registry;
- `ssh-entry.sh` → `deploy.sh`: `compose.yaml` в боевой форме, nginx с
  нашим `kronto.conf` и самоподписанным TLS, миграции на пустой базе,
  `api`, `worker` и `web` в статусе healthy, проверка через nginx;
- повторная выкатка того же коммита;
- `create-tenant`, затем `stand check` против стенда по HTTPS с настоящим
  Yandex Cloud — **9 из 9**;
- `purge`, `gaps --all`, `backup.sh`;
- точка входа отклоняет пустую и чужую команду, дописанную после sha
  команду, короткий sha и коммит не из веток origin.

Найдено и исправлено: локальная проверка через nginx шла через
`HTTPS_PROXY` из окружения — теперь `--noproxy '*'`. На сервере за
egress-прокси (DEPLOY §9a) выкатка иначе падала бы на последнем шаге.

**Статический анализ:** `shellcheck` по `deploy/stage/*.sh`, `actionlint`
по `.github/workflows/deploy.yaml`. `make-env.sh` проверен загрузкой всех
настроек приложения в `ENVIRONMENT=production`.

**Не проверено — нужен настоящий сервер и настройки GitHub:**

- `bootstrap.sh` на чистой Ubuntu 24.04: версии `docker.io` и
  `docker-compose-v2`, ufw, выпуск сертификата Let's Encrypt;
- запуск workflow `Deploy` в GitHub и публикация в GHCR;
- доступность Docker Hub, GHCR, Yandex Cloud и Telegram из сети Timeweb.
