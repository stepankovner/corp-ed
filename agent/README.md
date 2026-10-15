# Агент сетевых папок kronto

Программа, которая ставится **в сети клиента** рядом с файловым сервером:
обходит общие папки по SMB 2/3, читает права NTFS каждого файла,
переводит их через Active Directory в список почт сотрудников и
отправляет в kronto изменённые файлы и права. Файловый сервер из
интернета не виден, поэтому не kronto ходит к нему, а агент приходит в
kronto — только исходящие соединения по HTTPS, входящих портов не нужно.
Пароли от файлового сервера и каталога остаются у клиента.

Устройство и протокол — `docs/SMB-AGENT.md`. Живая проверка на стенде
Samba AD — `tests/live/smb/README.md`.

## Что нужно от администратора клиента

1. **Учётная запись домена для агента**, например `svc-kronto`:
   - на общем ресурсе — разрешение «Чтение» (share permissions);
   - в NTFS — «Чтение» (чтение данных и атрибутов, **чтение
     разрешений**) на всё, что нужно отдавать в kronto. Удобно завести
     группу `kronto-readers` с этим правом на корне ресурса с
     наследованием; у папок и файлов с отключённым наследованием право
     нужно дать отдельно — иначе агент их не увидит (это не ошибка: такой
     файл просто не попадёт в kronto).
   - Записи и изменения прав агенту не нужно и давать не надо.
2. **Чтение каталога AD** той же или отдельной учёткой. Обычный
   пользователь домена по умолчанию может читать пользователей, группы,
   `member`, `mail` — дополнительных прав не требуется. Подключение —
   только по TLS: LDAPS (636) или StartTLS (389); нужен сертификат
   корневого ЦС, которым подписан сертификат контроллера.
3. **Сеть**: агент → файловый сервер 445/tcp, агент → контроллер домена
   636/tcp (или 389 со StartTLS), агент → kronto 443/tcp.
4. **Ключ агента** из kronto (`kag_…`). Его выпускает администратор
   компании в kronto (сейчас — командой `cli agent-token`); перевыпуск
   отзывает старый ключ.
5. Docker (или Python 3.12 — тогда `pip install .` из этой папки).

## Настройки

Файл TOML, например `/etc/kronto-agent/agent.toml`. Секреты в файле
открытым текстом не принимаются: только `*_env` (имя переменной
окружения) или `*_file` (путь к файлу, например секрет Docker).

```toml
# Раз в сколько минут обходить ресурсы.
interval_minutes = 60
# Файлы больше — не отправляются (у kronto свой предел, обычно 50 МБ).
max_file_mb = 50
# Маски исключений: без «\» — по имени файла/папки, с «\» — по пути от
# корня ресурса; регистр не важен. Если не задано — временные файлы
# Office (~$*), *.tmp, Thumbs.db, desktop.ini и скрытые (.*).
exclude = ["~$*", "*.tmp", "Thumbs.db", "desktop.ini", ".*", "Архив\\*"]
# Если у пользователя нет mail — брать userPrincipalName. Включайте,
# только если UPN совпадает с почтой в kronto.
upn_fallback = false

[kronto]
url = "https://kronto.example.ru"
key_file = "/run/secrets/kronto_agent_key"   # или key_env = "KRONTO_AGENT_KEY"
# ca_file = "/etc/kronto-agent/kronto-ca.pem" # если kronto за своим ЦС

[ldap]
url = "ldaps://dc1.corp.example"             # или ldap://… и start_tls = true
base_dn = "DC=corp,DC=example"
bind_dn = "svc-kronto@corp.example"          # UPN, DN или CORP\svc-kronto
password_file = "/run/secrets/kronto_ldap_password"
ca_file = "/etc/kronto-agent/ad-ca.pem"      # ЦС сертификата контроллера

# Локальные группы файлового сервера (BUILTIN\…) в AD не видны: их
# состав на рядовом сервере свой. Если в правах папок встречается,
# например, BUILTIN\Users — сопоставьте её с группами домена, иначе
# разрешения по ней не засчитываются, а запреты считаются запретом для всех.
[local_groups]
"S-1-5-32-545" = ["Domain Users"]            # BUILTIN\Users

[[resources]]
path = '\\fs01\docs\Кадры'                   # \\сервер\ресурс[\папка]
username = 'CORP\svc-kronto'
password_file = "/run/secrets/kronto_smb_password"
# Права на сам ресурс (share permissions) агент прочитать не может и
# считает, что ресурс открыт на чтение всем, кто есть в правах NTFS.
# Если на ресурсе ограничение уже — укажите группу: каждый список почт
# будет пересечён с её участниками.
# share_readers = "FS-Docs-Readers"
# encrypt = true                             # шифрование SMB 3
# exclude = ["Черновики\\*"]                 # дополнительно к общим

[[resources]]
path = '\\fs02\public'
username = 'CORP\svc-kronto'
password_file = "/run/secrets/kronto_smb_password"
```

## Запуск в Docker

```bash
docker build -t kronto-agent agent/

# Проверка: LDAP, каждый ресурс (листинг и чтение прав первого файла),
# ключ в kronto.
docker run --rm --read-only \
  -v /etc/kronto-agent:/etc/kronto-agent:ro \
  -v /etc/kronto-agent/secrets:/run/secrets:ro \
  kronto-agent check --config /etc/kronto-agent/agent.toml

# Работа: обход раз в interval_minutes; журнал — в stderr контейнера.
docker run -d --name kronto-agent --restart unless-stopped --read-only \
  -v /etc/kronto-agent:/etc/kronto-agent:ro \
  -v /etc/kronto-agent/secrets:/run/secrets:ro \
  kronto-agent
```

Контейнер работает от непривилегированного пользователя (uid 10001) с
файловой системой только для чтения: агент ничего не хранит на диске,
состояние берёт из kronto (`GET /state`) в начале каждого обхода.
Файлы секретов должны читаться uid 10001. `kronto-agent run --once` —
один обход и выход (для планировщика вместо встроенного интервала).
SIGTERM прерывает обход между файлами; прерванный обход в kronto ничего не
удаляет.

## Как агент считает права

Подробно — `docs/SMB-AGENT.md`, §2, и докстроки `rights.py`,
`directory.py`. Коротко:

- Для каждого файла берётся его дескриптор безопасности (вместе с
  унаследованными ACE) и считается, кто может **читать содержимое**
  (`FILE_READ_DATA`, в том числе через `GENERIC_READ`/`GENERIC_ALL`).
- Группы раскрываются транзитивно (`LDAP_MATCHING_RULE_IN_CHAIN`),
  плюс основная группа пользователя (`primaryGroupID`). Everyone,
  Authenticated Users, Domain Users — список **всех включённых**
  пользователей домена, а не «видно всем».
- Запрет сильнее разрешения того же уровня; явные ACE — раньше
  унаследованных. Если порядок ACE неканонический — берётся более
  строгий из двух вариантов.
- Отключённые учётки не попадают; учётки без почты — тоже (если не
  включён `upn_fallback`).
- Что нельзя вычислить однозначно (SID чужого домена, локальная группа
  сервера без сопоставления, условные ACE), разрешением не считается, а
  запрет по такому SID — запрет для всех. Документ без единого читателя
  в kronto не отправляется (если он там был — права отзываются).

## Чего агент не делает

- Не читает права на сам ресурс (share permissions) — см.
  `share_readers`.
- Не ходит по ссылкам и точкам повторной обработки (симлинки, junction,
  ссылки DFS внутри ресурса) — их содержимое добавляйте отдельным
  ресурсом.
- Не использует Kerberos: вход по NTLM (внутри Negotiate). В домене, где
  NTLM запрещён, агент не подключится — это часть живой проверки у
  клиента.
- Изменения состава групп видит при следующем обходе (кэш каталога
  живёт один обход).

## Счётчики обхода

В `finish` и в журнале: `files`, `documents_added/updated/unchanged`,
`documents_too_large/unsupported/rejected` (отказ kronto), `too_large`
(лимит агента), `excluded`, `no_readers`, `access_sent`,
`access_revoked`, `access_unknown`, `sd_denied`/`read_denied`/
`dirs_denied` (агенту нет доступа), `sd_invalid`, `reparse_points`,
`resources_failed`, `users_without_email` и причины пропусков прав:
`unresolved_allow`, `unresolved_deny`, `conditional_allow`,
`unknown_ace`, `non_canonical`.

## Лицензии зависимостей

| Пакет | Лицензия |
|---|---|
| smbprotocol, pyspnego | MIT |
| cryptography | Apache-2.0 или BSD-3-Clause |
| cffi | MIT-0 |
| httpx, httpcore, idna, pycparser | BSD-3-Clause |
| anyio, h11 | MIT |
| certifi | MPL-2.0 |
| pyasn1 | BSD-2-Clause |
| typing_extensions | PSF-2.0 |
| **ldap3** | **LGPL-3.0** |

(Список — по образу `kronto-agent` на 15.10.2026; GPL/AGPL нет.)

Почему ldap3 допустим в поставляемом клиенту образе. LGPL-3.0 требует
от того, кто распространяет программу с библиотекой: дать текст
лицензии, исходный код библиотеки и возможность заменить её своей
версией. В образе ldap3 стоит **без изменений** обычным пакетом pip
(исходный код .py и файлы лицензии — в `site-packages/ldap3*`),
подключается импортом во время работы и заменяется `pip install` другой
версии — ничего не связано статически. Код агента при этом остаётся под
своей лицензией: LGPL, в отличие от GPL/AGPL, не распространяется на
программу, которая только использует библиотеку. Риск другого рода:
последний выпуск ldap3 — 2.9.1 (2021); на стенде он работает с Python
3.12 и Samba 4.22 (LDAPS, постраничный поиск). Если понадобится замена —
весь LDAP агента в одной функции `ldap3_search` (`directory.py`).

## Разработка

```bash
python3.12 -m venv agent/.venv
agent/.venv/bin/pip install -e 'agent[dev]'
cd agent
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/python -m pytest -q        # живые тесты без стенда пропускаются
```

Модульные тесты: разбор дескрипторов на байтах (ручная сборка и
настоящие дескрипторы со стенда в `tests/fixtures`), вычисление прав,
каталог на поддельном LDAP, протокол и обход целиком на поддельном
kronto (`tests/fake_kronto.py`, httpx.MockTransport).
