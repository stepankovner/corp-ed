# Тестовый стенд

**Стенд** — отдельный сервер, где приложение работает так же, как будет
работать у клиентов: та же сборка, `ENVIRONMENT=production`, HTTPS через
nginx по `deploy/nginx/kronto.conf`, cron, бэкап, настоящая модель
Яндекса. Отличие от боя одно: на стенде только тестовые данные и нет
клиентов.

**Зачем он нужен.**

- Каждое изменение сначала попадает сюда, и проверяем его мы, а не
  клиенты.
- Сейчас на нём отлаживаем деплой, CI/CD, pgvector, миграции и коннекторы.
- Когда появится боевой сервер, стенд останется местом, где проверяют
  каждое обновление.

Боевой деплой и его чек-лист — `DEPLOY.md`. Выбор боевого хостинга —
`RESILIENCE-RESEARCH.md` §3.

---

## 1. Адреса

| Адрес | Что там |
|---|---|
| `krontoai.ru`, `www.krontoai.ru` | лендинг — GitHub Pages (A-записи 185.199.108–111.153). Не трогаем |
| **`stage.krontoai.ru`** | **этот стенд** |
| `app.krontoai.ru` | боевое приложение — позже, на своём сервере (имя уже заложено в `kronto.conf` и `DEPLOY.md`) |

DNS домена ведётся в Яндекс 360 для бизнеса (NS: `dns1.yandex.net`,
`dns2.yandex.net`).

---

## 2. Сервер

**Timeweb Cloud, Москва, Ubuntu 24.04, конфигурация Cloud-80: 4 vCPU,
8 ГБ RAM, 80 ГБ NVMe** — 1 800 ₽/мес. Это цена со страницы тарифов, где
указана скидка 10 % за оплату на 12 месяцев; почасовая оплата тоже есть
([Timeweb](https://timeweb.cloud/services/cloud-servers)).

**Почему такой размер.**

- На сервере работают PostgreSQL, `api`, `worker`, Redis, `web` и nginx.
- Самое тяжёлое — разбор PDF: песочница берёт до 1,5 ГБ
  (`ingest/extract_worker.py`), модель разметки занимает все ядра
  (WORKLOG, №33).
- Cloud-50 (2 vCPU / 4 ГБ, 1 080 ₽) формально вместит систему, но без
  запаса.

**Почему Timeweb, хотя для боя мы советуем Selectel.**

- Стенду не нужна отказоустойчивость, клиентских данных на нём нет.
- Он втрое дешевле.
- Переезд на другой хостинг — это `bootstrap.sh` на новом сервере.
- Если бой будет в Selectel, стенд стоит перенести туда до пилота.

---

## 3. Как устроено

```
push в main ─► CI (тесты, e2e, образы) ─► Deploy
                                           ├─ images: сборка по проверенному коммиту
                                           │   → ghcr.io/stepankovner/corp-ed:<sha>, kronto-web:<sha>
                                           ├─ deploy: ssh deploy@стенд "deploy <sha>"  (+ токен GHCR в stdin)
                                           └─ check:  ssh deploy@стенд "check"  → stand check 9 шагов

стенд:  corp-ed-deploy (deploy/stage/ssh-entry.sh)
          deploy: коммит есть в ветке origin? → git checkout → deploy.sh:
                  pull → compose up (migrate → api, worker, web; db, redis) → все healthy → https через nginx
          check:  check.sh → python -m corp_ed.stand check против https://stage.krontoai.ru
        nginx: TLS (Let's Encrypt); /api и /health → 127.0.0.1:8000, остальное → 127.0.0.1:8080
        cron: 03:10 purge, 03:30 gaps --all, 04:00 pg_dump (хранится 7 дней)
```

- **Образы.** Сервер ничего не собирает: образы строит workflow по
  коммиту, прошедшему CI. Скачивает их сервер токеном этого workflow —
  токен живёт до конца job и на диске не остаётся. Поэтому пакеты в GHCR
  могут оставаться приватными.
- **Ключ выкатки.** Ключ GitHub Actions на сервере умеет только две
  команды: `deploy <sha>` и `check` (`command=` в `authorized_keys`).
  Ни shell, ни проброса портов. Выкатить можно только коммит из ветки
  этого репозитория: коммиты форков GitHub отдаёт по SHA, поэтому они
  отсекаются отдельно.
- **Проверка после выкатки.** Это тот же `stand check`, что в
  `DEPLOY.md` §4, но запускает его сам сервер:
  - при первом запуске он заводит компанию `stand-check`;
  - её пароль хранится только на сервере (`/var/lib/corp-ed`, права 600);
  - итог проверки виден в логе workflow.

---

## 4. Первый запуск

Порядок: 1 → 2 → (ждать DNS) → 3 → 4 → 5 → 6.

### 4.1. Сервер в Timeweb Cloud

1. `timeweb.cloud` → регистрация (юрлицо или физлицо) → пополнить баланс
   (хватит суммы за месяц).
2. Панель → **Облачные серверы** → **Создать**:
   - **Операционная система:** Ubuntu 24.04;
   - **Регион:** Москва;
   - **Конфигурация:** Cloud-80 (4 × 3,3 ГГц, 8 ГБ, 80 ГБ NVMe);
   - **SSH-ключ:** добавить свой открытый ключ (как создать — ниже);
   - **Имя:** `corp-ed-stage`.
3. После создания — скопировать **IPv4-адрес** сервера.

Если своего SSH-ключа нет, создайте его на своём компьютере. Windows 10/11
(PowerShell), macOS или Linux:

```bash
ssh-keygen -t ed25519 -C "имя@krontoai.ru"
# Enter на все вопросы; открытый ключ — файл ~/.ssh/id_ed25519.pub
# (Windows: C:\Users\<имя>\.ssh\id_ed25519.pub) — его текст и вставить в Timeweb
```

Проверка входа: `ssh root@<IP>`.

### 4.2. Запись DNS в Яндекс 360

1. `admin.yandex.ru` → **Общие настройки** → **Домены** → в блоке
   `krontoai.ru` → **Управлять DNS-записями**
   ([справка](https://yandex.ru/support/yandex-360/business/admin/ru/domains/dns/index.html)).
2. **Добавить запись:**
   - **Тип** — A;
   - **Хост** — `stage`;
   - **Значение** — IP сервера;
   - **TTL** — по умолчанию.
3. Проверить, что запись видна. Обычно это минуты, в худшем случае — до
   72 часов:
   ```bash
   nslookup stage.krontoai.ru 8.8.8.8     # должен вернуть IP сервера
   ```
   Пока запись не видна, шаг 4.4 не пройдёт: Let's Encrypt не сможет
   выпустить сертификат.

### 4.3. Ключ выкатки для GitHub

Отдельный ключ, не ваш личный. Создаётся на своём компьютере:

```bash
ssh-keygen -t ed25519 -N "" -C corp-ed-stage-deploy -f corp-ed-stage-deploy
```

Получатся два файла:

- `corp-ed-stage-deploy.pub` — открытый, пойдёт на сервер (шаг 4.4);
- `corp-ed-stage-deploy` — приватный, пойдёт в GitHub (шаг 4.6). После
  этого его можно удалить.

### 4.4. Настройка сервера (один раз, под root)

```bash
ssh root@<IP>
curl -fsSLO https://raw.githubusercontent.com/stepankovner/corp-ed/main/deploy/stage/bootstrap.sh
DOMAIN=stage.krontoai.ru LETSENCRYPT_EMAIL=<почта команды> \
DEPLOY_PUBKEY="<весь текст файла corp-ed-stage-deploy.pub>" bash bootstrap.sh
```

Скрипт работает 3–5 минут. Что он делает:

- ставит Docker и compose из архива Ubuntu с ротацией логов, swap 4 ГБ,
  nginx, certbot, файрвол (открыты только 22, 80, 443), автообновления
  безопасности;
- заводит пользователя `deploy` с ключом выкатки;
- кладёт код в `/opt/corp-ed` и генерирует `/opt/corp-ed/.env`;
- выпускает сертификат, ставит конфиг nginx и cron.

В конце он печатает строку для `STAGE_SSH_KNOWN_HOSTS`: её нужно
скопировать для шага 4.6.

Если с сервера не скачиваются образы с Docker Hub, перезапустите
скрипт, добавив в начало команды
`REGISTRY_MIRROR=https://dockerhub.timeweb.cloud` (зеркало Timeweb).
Повторный запуск безопасен.

### 4.5. Ключи Yandex Cloud в `/opt/corp-ed/.env`

`/opt/corp-ed/.env` — файл **на сервере**, не в репозитории и не на
вашем компьютере. Его создал `bootstrap.sh`: в нём все пароли и секреты
стенда, уже сгенерированные. Не хватает только двух значений.

1. **Взять ключи.** Лучше завести для стенда отдельный каталог: своя
   квота и видно, сколько тратит стенд.
   - `YC_FOLDER_ID` — идентификатор каталога: консоль Yandex Cloud →
     нужный каталог → строка вида `b1g…` под его названием.
   - `YC_API_KEY` — AI Studio (`aistudio.yandex.ru`) в этом каталоге →
     **Создать API-ключ** (справа вверху). AI Studio сама заведёт
     сервисный аккаунт с ролью `ai.editor`
     ([документация](https://aistudio.yandex.ru/docs/ru/ai-studio/operations/get-api-key.html)).
     Секрет показывается один раз.
2. **Вписать их** на сервере:
   ```bash
   ssh root@<IP>
   nano /opt/corp-ed/.env
   # найти строки YC_FOLDER_ID= и YC_API_KEY=, вписать значения после «=» без пробелов и кавычек
   # сохранить: Ctrl+O, Enter; выйти: Ctrl+X
   ```
3. **Если стенд уже выкачен** — применить новые значения:
   ```bash
   cd /opt/corp-ed && sudo -u deploy docker compose -f compose.yaml up -d
   ```

Остальное в файле менять не нужно. Права файла — 600, владелец
`deploy`.

### 4.6. Настройки GitHub

Всё делается на странице репозитория → **Settings**. Нужны права
администратора репозитория.

1. **Environments** → **New environment** → имя `stage` → **Configure
   environment**:
   - **Environment secrets** → **Add environment secret**:
     `STAGE_SSH_KEY` = весь текст приватного файла `corp-ed-stage-deploy`,
     включая строки `-----BEGIN…` и `-----END…`;
   - по желанию: **Required reviewers** — тогда каждую выкатку кто-то
     подтверждает кнопкой.
2. **Secrets and variables** → **Actions** → вкладка **Variables** →
   **New repository variable**, четыре штуки:

   | Имя | Значение |
   |---|---|
   | `STAGE_ENABLED` | `true` |
   | `STAGE_HOST` | `stage.krontoai.ru` |
   | `STAGE_DOMAIN` | `stage.krontoai.ru` |
   | `STAGE_SSH_KNOWN_HOSTS` | строка, которую напечатал `bootstrap.sh` (`stage.krontoai.ru ssh-ed25519 AAAA…`) |

### 4.7. Первая выкатка и проверка

**Actions** → **Deploy** → **Run workflow** → ветка `main` → **Run**.

Три шага должны стать зелёными:
- **Deploy** — выкатка;
- **Reachable from outside** — стенд открывается из интернета;
- **Stand check** — 9 шагов: вход, загрузка, индексация, ответ модели со
  ссылкой, кредиты.

После этого `https://stage.krontoai.ru` открывается в браузере.

**Своя компания, чтобы смотреть стенд глазами.** Проверочную компанию
`stand-check` workflow заводит сам. Для входа в браузере нужна своя:

```bash
ssh root@<IP>
cd /opt/corp-ed
sudo -u deploy docker compose -f compose.yaml run --rm --no-deps api \
    python -m corp_ed.cli create-tenant --code demo --name "Демо" --seats 10 \
    --admin-email <ваша почта> --not-found-mode general
# временный пароль команда спросит дважды (скрытый ввод); при первом входе система попросит его сменить
```

---

## 5. Каждый день

- **`main`** выкатывается сам после зелёного CI, проверка стенда — сразу
  следом.
- **Ветка:** Actions → Deploy → Run workflow → выбрать ветку.
  - Осторожно с миграциями: если ветка добавила ревизию, а потом
    выкатывают `main` без неё, `migrate` упадёт, потому что база стоит на
    неизвестной `main` ревизии.
  - Перед возвратом на `main`: `docker compose -f compose.yaml run --rm
    migrate alembic downgrade <ревизия main>`.
- **Откат:** Actions → Deploy → Run workflow → в поле `sha` — коммит, на
  который откатываемся. Миграции сами не откатываются (см. предыдущий
  пункт).
- **Проверка без выкатки** (на сервере):
  `sudo -u deploy SSH_ORIGINAL_COMMAND=check /usr/local/bin/corp-ed-deploy`.
- **Логи:**
  - сервисы: `cd /opt/corp-ed && docker compose -f compose.yaml logs -f
    api worker`;
  - история выкаток: `/var/log/corp-ed/deploys.log`;
  - cron: `/var/log/corp-ed/cron.log`.
- **База:** `docker compose -f compose.yaml exec db psql -U corp_ed corp_ed`.
- **Изменились `kronto.conf`, cron или `bootstrap.sh`** — перезапустить
  `bootstrap.sh` с теми же переменными. Выкатка их не трогает.

---

## 6. Правила стенда

- **Только тестовые данные**, никаких документов клиентов, и вот почему:
  - Yandex AI Studio по умолчанию сохраняет запросы (RISKS №48), пока в
    код не добавлен `x-data-logging-enabled: false`;
  - сервер — обычный, не в аттестованном сегменте по 152-ФЗ;
  - бэкапы лежат только на самом сервере.
- **Приём заявок** на созвон выключен (`LEADS_ENABLED` по умолчанию `false`).
- **Telegram.** Стенд — то место, где стоит проверить
  `curl -m 10 https://api.telegram.org` с российского сервера (RISKS №50)
  до того, как включать `TEAM_NOTIFY_TELEGRAM_*`.
- **SSH.** Вход — только по ключам. Когда свой ключ проверен, пароль
  для SSH лучше выключить (`PasswordAuthentication no` в
  `/etc/ssh/sshd_config`, затем `systemctl reload ssh`).

---

## 7. Что проверено (29.09, в среде сессии)

**Выкатка целиком, на имитации сервера:**

- образы собраны по коммиту и положены в registry, закрытый паролем, —
  как приватный GHCR;
- `ssh-entry.sh` → `deploy.sh` с токеном через stdin: `compose.yaml` в
  боевой форме, nginx с нашим `kronto.conf` и TLS, миграции на пустой
  базе, `api`, `worker` и `web` в статусе healthy, проверка через nginx;
- без токена выкатка останавливается на скачивании образов, работающий
  стенд не трогается; токен после выкатки на диске не остаётся;
- `check.sh`:
  - первый запуск завёл компанию и сменил временный пароль на
    постоянный (в состоянии остался только постоянный);
  - второй запуск — `stand check` против стенда по HTTPS с настоящим
    Yandex Cloud, **9 из 9**;
  - при недоступной модели проверка честно падает с кодом 1;
- `purge`, `gaps --all`, `backup.sh` (дамп с правами 600);
- точка входа отклоняет пустую и любую чужую команду, дописанную после
  `deploy` или `check` команду, короткий sha и коммит не из веток
  origin.

**Найдено и исправлено:** локальная проверка через nginx шла через
`HTTPS_PROXY` из окружения — теперь `--noproxy '*'`.

**Статический анализ:** `shellcheck` по `deploy/stage/*.sh`, `actionlint`
по `.github/workflows/deploy.yaml`. `make-env.sh` проверен загрузкой всех
настроек приложения в `ENVIRONMENT=production`.

**Не проверено — нужен настоящий сервер и настройки GitHub:**

- `bootstrap.sh` на чистой Ubuntu 24.04: версии `docker.io` и
  `docker-compose-v2`, ufw, выпуск сертификата;
- запуск workflow в GitHub и публикация в GHCR;
- доступ к Docker Hub, GHCR, Yandex Cloud и Telegram из сети Timeweb.
