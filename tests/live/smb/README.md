# Живой стенд агента сетевых папок (Samba AD)

Стенд для проверки агента `agent/` (вид `smb_agent`, `docs/SMB-AGENT.md`):
**Samba 4 в роли контроллера домена Active Directory** в Docker — домен
`KRONTO.TEST`, общий ресурс `docs` с правами NTFS (дескрипторы
безопасности в xattr через `vfs_acl_xattr`), LDAPS с самоподписанным
сертификатом, выдуманные пользователи и группы. Тесты —
`agent/tests/live/test_smb_live.py`; запускаются в контейнере `tester`
внутри сети стенда (образ агента + pytest, файловая система только для
чтения). Без стенда (`SMB_STAND=1`) они пропускаются.

**Это Samba, а не Windows Server.** Протоколы те же — SMB 3 (подпись,
NTLM), QUERY_INFO с дескриптором безопасности, LDAP AD с
`LDAP_MATCHING_RULE_IN_CHAIN`, — но поведение настоящего Windows Server с
GPO, DFS, доверием между доменами, локальными группами рядового
файлового сервера и запретом NTLM здесь не проверено: это часть живой
проверки у первого клиента. Файлы лежат прямо на контроллере домена
(так устроены и общие ресурсы Samba AD «из коробки»), рядового
файлового сервера нет.

Проверено 15.10.2026 на **Samba 4.22.11** (Debian 13 trixie,
`samba-ad-dc 2:4.22.11+dfsg-0+deb13u1`), агент — Python 3.12.15,
smbprotocol 1.17.0, pyspnego 0.12.4, ldap3 2.9.1, httpx 0.28.1 — итоги
внизу.

## Что засеяно (`seed.sh`)

| Учётка | Почта (`mail`) | Группы | Особенность |
|---|---|---|---|
| anna | anna@example.com | hr | |
| boris | boris@example.com | hr-leads (вложена в hr) | |
| vera | vera@example.com | — | явный запрет на «Проекты» |
| gleb | gleb@example.com | — | |
| dmitry | dmitry@example.com | hr | **отключён** |
| egor | — (UPN egor@kronto.test) | — | без почты |
| svc-kronto | — | kronto-readers | учётка агента, только чтение |

Все пароли — `Kronto-Stand-User-1`, администратор —
`Kronto-Stand-Admin-1` (выдуманные, только для стенда).

| Путь на `\\dc1.kronto.test\docs` | Права | Ожидаемые почты |
|---|---|---|
| (корень) | защищён: SYSTEM, Domain Admins — полный; kronto-readers — чтение (наследуется) | — |
| Общая\Регламент.txt | Domain Users — чтение, унаследовано с папки | anna, boris, gleb, vera |
| Общая\Секрет директора.txt | наследование отключено (P), явно: gleb, Domain Admins, kronto-readers | gleb |
| Кадры\Отпуска.txt | группа hr, унаследовано | anna, boris (через hr-leads) |
| Кадры\Оценка\Итоги.txt | hr, унаследовано через две папки | anna, boris |
| Проекты\План.txt | запрет vera + Domain Users, оба унаследованы | anna, boris, gleb |
| Проекты\Для Веры.txt | явное разрешение vera + унаследованный запрет vera | anna, boris, gleb, vera |
| Общая\~$Регламент.txt, Общая\Thumbs.db | — | исключены масками |
| Общая\Архив.bin (2 МБ) | — | пропущен лимитом (в тесте 1 МБ) |

Права папок заданы через `samba-tool ntacl set` в том виде, как их хранит
Windows (флаг AI, унаследованные ACE с ID), файлы созданы по SMB уже
после этого — унаследованные ACE у них вычислил сервер. Правки
отдельных файлов — `smbcacls` по SMB.

## Что нужно

- Docker с BuildKit (compose ≥ 2.20), ~650 МБ диска на образы, ~300 МБ
  памяти. Порты наружу не публикуются (выделены 8480–8489 — не
  понадобились): тесты идут в сети стенда, контроллер доступен там как
  `dc1.kronto.test`.
- Контейнеру контроллера нужен `CAP_SYS_ADMIN` (xattr `security.NTACL`
  с дескриптором безопасности пишется только с ним); `privileged` не
  нужен.
- За HTTPS-прокси со своим сертификатом (песочница Claude) сборке нужны
  `SMB_APT_PROXY` (apt ходит по https через прокси) и `SMB_BUILD_CA`
  (сертификат прокси для apt и pip); образы `debian:trixie-slim` и
  `python:3.12.15-slim-trixie` при 429 от Docker Hub — с
  `mirror.gcr.io/library/…` и `docker tag`.

## Как поднять (≈1 минута после сборки)

Все команды — из корня репозитория.

1. Собрать образы (контроллер и `tester`):

   ```bash
   docker compose -f tests/live/smb/compose.yaml --profile test build
   # в песочнице:
   SMB_APT_PROXY=http://127.0.0.1:44007 SMB_BUILD_CA=/root/.ccr/ca-bundle.crt \
     docker compose -f tests/live/smb/compose.yaml --profile test build
   ```

2. Запустить контроллер (провизия домена при первом старте, ~15 с):

   ```bash
   docker compose -f tests/live/smb/compose.yaml up -d --wait dc
   ```

3. Засеять (только на чистом стенде):

   ```bash
   docker compose -f tests/live/smb/compose.yaml exec dc seed.sh
   ```

4. Тесты агента в сети стенда:

   ```bash
   docker compose -f tests/live/smb/compose.yaml run --rm tester
   ```

   Права файла глазами Samba:
   `docker compose -f tests/live/smb/compose.yaml exec dc samba-tool ntacl get --as-sddl "/srv/docs/Проекты/План.txt"`.

5. Убрать стенд и образы:

   ```bash
   docker compose -f tests/live/smb/compose.yaml --profile test down -v
   docker image rm kronto-live-smb-dc:dev kronto-agent:test
   ```

## Что проверяют тесты

| Тест | Что доказывает |
|---|---|
| `directory_over_ldaps` | Привязка по LDAPS с проверкой сертификата; включённые пользователи с почтой — ровно anna, boris, vera, gleb (dmitry отключён, egor без почты); hr через `IN_CHAIN` — anna и boris (вложенная hr-leads, без отключённого); Domain Users — все включённые |
| `walk_compute_rights_and_send` | Обход ресурса по SMB 3 под svc-kronto, дескриптор каждого файла (QUERY_INFO OWNER\|GROUP\|DACL), вычисленные почты по каждому файлу = таблица выше; в поддельный kronto пришли ровно 6 файлов с содержимым и путём; маски и лимит размера сработали; второй обход — без отправки содержимого, только `/access`, ничего не удалено |
| `upn_fallback` | С `upn_fallback` добавляются egor@kronto.test (там, где читают Domain Users) и svc-kronto@kronto.test (группа агента читает всё) — почему этот флаг выключен по умолчанию |
| `protected_descriptor_from_server` | Сервер отдаёт флаг DACL_PROTECTED и только явные ACE у файла с отключённым наследованием, AI и только унаследованные ACE — у файла двумя папками ниже |
| `ldap_certificate_name_is_checked` | LDAPS к тому же контроллеру по имени `dc` (сертификат на `dc1.kronto.test`) — отказ |
| `ldap_start_tls` | StartTLS на 389 работает и тоже проверяет имя в сертификате |
| `wrong_smb_password` | Неверный пароль ресурса — ошибка подключения, а не пустой обход |

Кроме тестов, на стенде запускался сам образ агента (`kronto-agent:dev`,
`--read-only`, uid 10001): `kronto-agent check` — LDAP ok (7 включённых,
4 с почтой), SMB ok (листинг и дескриптор первого файла), kronto — ошибка
имени (адрес выдуманный, так и задумано).

## Итоги проверки 15.10.2026

Стенд поднят с нуля по шагам выше: **7 passed** за 1,9 с
(`docker compose … run --rm tester`). Модульные тесты агента — 104
passed.

Что выяснилось на стенде и учтено в агенте:

- Samba записывает ACL, выставленный через smbcacls, с ревизией **3**
  (`ACL_REVISION3`); [MS-DTYP] называет только 2 и 4. Агент принимает
  2–4; настоящий дескриптор с ревизией 3 — в
  `agent/tests/fixtures/samba-public-director-secret.sd`.
- Флаг INHERITED_ACE у ACE файла сервер ставит, только если у папки есть
  флаг AI (SE_DACL_AUTO_INHERITED) — так же, как Windows. Права папки,
  записанные без AI (например, старым инструментом), дают файлам те же
  ACE, но как явные; агент тогда считает их явными (результат для
  канонического порядка тот же).
- Права, заданные на папке после создания вложенных файлов, на файлы не
  распространяются, пока их не распространить явно (`smbcacls
  --propagate-inheritance`, в Windows — «Применить к вложенным»). Агент
  читает права каждого файла, а не папки, поэтому видит то, что
  действительно применяется.
- ldap3 2.9.1 на Python 3.12 работает (LDAPS, StartTLS, постраничный
  поиск, правило IN_CHAIN); единственный шум — DeprecationWarning pyasn1.
