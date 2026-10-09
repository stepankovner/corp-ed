"""Виды семейства WebDAV: формы подключения и чем они различаются.

Все — режим per_user: WebDAV не отдаёт, кто ещё видит файл (ACL общих
папок читается только отдельными API, а у NAS и Облака Mail.ru — никак),
поэтому каждый сотрудник подключает свою учётку и видит в ответах то,
что видит в своём дереве. Сделать «одну служебную учётку на всех» —
значило бы показать всем всё, что видит она.

Адреса WebDAV (открыты 09.10.2026):
- Nextcloud — /remote.php/dav/files/<uid>/
  (docs.nextcloud.com/server/latest/developer_manual/client_apis/WebDAV/basic.html);
  uid — не всегда логин (LDAP, вход по почте), поэтому узнаётся через
  current-user-principal (RFC 5397) на /remote.php/dav/. Общие папки,
  групповые папки и внешние хранилища видны в дереве сотрудника как
  обычные папки (nc:mount-type shared/group/external);
- ownCloud — /remote.php/webdav/ (ownCloud Server 10; в Infinite Scale тот
  же адрес ведёт в личное пространство — полученные общие папки и
  пространства проектов там лежат отдельно и этим видом не читаются);
- Seafile — расширение SeafDAV: https://<сервер>/seafdav/ (share_name в
  seafdav.conf, по умолчанию /seafdav; manual.seafile.com/latest/extension/webdav/).
  Вход — почта и пароль; при входе через SSO — отдельный «пароль WebDAV»
  (ENABLE_WEBDAV_SECRET, manual.seafile.com/latest/config/seahub_settings_py/);
- Диск VK WorkSpace — https://webdav.cloud.<почтовый домен>, логин —
  почта, пароль — пароль для внешнего приложения
  (workspace.vk.ru/docs/on-premises/disk/webdav-access/ — документация
  коробочной версии). В документации облачной версии WebDAV упомянут
  только в ограничениях (workspace.vk.ru/docs/saas/ru/disk/limitations:
  «размер файла, загружаемого по WebDAV»), адреса нет — поэтому адрес
  вводит админ, и облачный вариант требует живой проверки;
- Облако Mail.ru — https://webdav.cloud.mail.ru, логин — почта, с
  01.01.2022 — только пароль для внешнего приложения
  (help.mail.ru/cloud_web/app/webdav). По сторонним источникам WebDAV
  доступен только на платных тарифах — не подтверждено официально;
- просто WebDAV (Synology, QNAP, Apache mod_dav и т. п.) — адрес целиком.

id документа общий для сотрудников только там, где сервер даёт сквозной
номер файла (oc:fileid Nextcloud и ownCloud): общая папка — один
материал с доступом каждому, кто её видит (как у Битрикс24 per_user).
У остальных путь ничего не говорит о файле: «Документы/Отпуск.txt» двух
сотрудников NAS — разные файлы в их домашних папках, и общий id выдал
бы одному чужое. Там id — свой у каждого сотрудника.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

from corp_ed.connectors.common import OAUTH_CALLBACK_PATH
from corp_ed.connectors.registry import FieldSpec, KindSpec, ModuleSpec, UserAuth
from corp_ed.domain.types import ConnectorMode

MODULE_FILES = "files"
MAX_FOLDERS = 50
MAILRU_WEBDAV = "https://webdav.cloud.mail.ru/"
MAILRU_WEB = "https://cloud.mail.ru/home/"

Root = Literal["nextcloud", "owncloud", "address", "mailru"]
WebLink = Literal["fileid", "mailru", "file"]


@dataclass(frozen=True)
class Flavor:
    """Чем вид отличается от просто WebDAV."""

    root: Root
    """Как найти корень сотрудника: principal Nextcloud, адрес ownCloud,
    адрес из настроек или фиксированный адрес Облака Mail.ru."""
    stable_ids: bool
    """oc:fileid — сквозной номер файла: общий id для всех сотрудников."""
    web: WebLink
    """Ссылка на документ в ответе: /index.php/f/<fileid>, папка в
    веб-интерфейсе Облака или сам файл по WebDAV."""


def parse_folders(value: str) -> list[tuple[str, ...]]:
    """«Документы, Общие/Регламенты» → [("Документы",), ("Общие", "Регламенты")].

    Пути — от корня сотрудника. ValueError с кодом: `.` и `..` не
    принимаются (папки только внутри дерева), больше MAX_FOLDERS — тоже.
    """
    folders: list[tuple[str, ...]] = []
    for raw in value.split(","):
        parts = tuple(part.strip() for part in raw.strip().strip("/").split("/"))
        if parts == ("",):
            continue
        if any(part in ("", ".", "..") for part in parts):
            raise ValueError("folders_invalid")
        if parts not in folders:
            folders.append(parts)
    if len(folders) > MAX_FOLDERS:
        raise ValueError("folders_too_many")
    return folders


def _check_config(config: Mapping[str, str]) -> str | None:
    try:
        parse_folders(config.get("folders", ""))
    except ValueError as exc:
        return str(exc)
    return None


def _check_credentials(credentials: Mapping[str, str]) -> str | None:
    # Логин с двоеточием Basic-авторизация не передаёт: сервер увидит
    # другой логин и другой пароль.
    if ":" in credentials.get("login", ""):
        return "login_invalid"
    return None


_FOLDERS = FieldSpec(
    "folders", "Папки: пути через запятую, пусто — все", required=False
)
_MODULES = (ModuleSpec(MODULE_FILES, "Файлы и папки сотрудника, включая общие"),)


def _spec(
    kind: str,
    title: str,
    *,
    server: str | None,
    login_title: str,
    credential_title: str,
    preview: bool = True,
) -> KindSpec:
    config = (FieldSpec("server", server), _FOLDERS) if server else (_FOLDERS,)
    return KindSpec(
        kind=kind,
        title=title,
        mode=ConnectorMode.PER_USER,
        modules=_MODULES,
        config_fields=config,
        credential_fields=(
            FieldSpec("login", login_title),
            FieldSpec("password", credential_title, secret=True),
        ),
        url_field="server" if server else None,
        config_check=_check_config,
        credentials_check=_check_credentials,
        preview=preview,
        base=True,
    )


NEXTCLOUD = _spec(
    "nextcloud",
    "Nextcloud",
    server="Адрес Nextcloud (https://cloud.example.ru/)",
    login_title="Логин Nextcloud",
    credential_title=(
        "Пароль приложения (Настройки → Безопасность → «Создать новый пароль "
        "приложения»)"
    ),
    # Проверен живьём 09.10.2026: Nextcloud 34.0.4
    # (tests/live/test_nextcloud_live.py).
    preview=False,
)
NEXTCLOUD_OAUTH = KindSpec(
    kind="nextcloud_oauth",
    title="Nextcloud (вход через OAuth2)",
    mode=ConnectorMode.PER_USER,
    user_auth=UserAuth.OAUTH,
    modules=_MODULES,
    config_fields=(
        FieldSpec("server", "Адрес Nextcloud (https://cloud.example.ru/)"),
        FieldSpec("client_id", "Идентификатор клиента OAuth 2.0"),
        _FOLDERS,
    ),
    app_credential_fields=(
        FieldSpec("client_secret", "Секрет клиента OAuth 2.0", secret=True),
    ),
    url_field="server",
    config_check=_check_config,
    extra={
        "app_type": (
            "Администрирование → Безопасность → «Клиенты OAuth 2.0»: имя — "
            "любое, адрес перенаправления — ниже"
        ),
        "oauth_callback_path": OAUTH_CALLBACK_PATH,
    },
    # Проверен живьём 09.10.2026: Nextcloud 34.0.4, клиент из occ и вход
    # в браузере (tests/live/test_nextcloud_oauth_live.py).
    preview=False,
    base=True,
)
OWNCLOUD = _spec(
    "owncloud",
    "ownCloud",
    server="Адрес ownCloud (https://owncloud.example.ru/)",
    login_title="Логин ownCloud",
    credential_title=(
        "Пароль приложения (Настройки → Безопасность → «Новое приложение»)"
    ),
    # Проверен живьём 09.10.2026: ownCloud 10.16.6
    # (tests/live/test_owncloud_live.py).
    preview=False,
)
SEAFILE = _spec(
    "seafile",
    "Seafile",
    server="Адрес WebDAV Seafile (https://seafile.example.ru/seafdav/)",
    login_title="Почта (логин Seafile)",
    credential_title="Пароль (при входе через SSO — пароль WebDAV из настроек профиля)",
    # Проверен живьём 09.10.2026: Seafile 13.0.28 CE
    # (tests/live/test_seafile_live.py).
    preview=False,
)
VK_WORKSPACE = _spec(
    "vk_workspace_disk",
    "Диск VK WorkSpace",
    server="Адрес WebDAV (https://webdav.cloud.<почтовый домен>/)",
    login_title="Почта",
    credential_title="Пароль для внешнего приложения",
)
MAILRU = _spec(
    "mailru_cloud",
    "Облако Mail.ru",
    server=None,
    login_title="Почта",
    credential_title=(
        "Пароль для внешнего приложения (Безопасность → Пароли для внешних "
        "приложений, доступ «Облако»)"
    ),
)
WEBDAV = _spec(
    "webdav",
    "WebDAV (NAS: Synology, QNAP и другие)",
    server="Адрес WebDAV (https://nas.example.ru:5006/)",
    login_title="Логин",
    credential_title="Пароль",
    # Проверен живьём 09.10.2026: Apache httpd 2.4.69 с mod_dav
    # (tests/live/test_webdav_live.py).
    preview=False,
)

SPECS = (NEXTCLOUD, NEXTCLOUD_OAUTH, OWNCLOUD, SEAFILE, VK_WORKSPACE, MAILRU, WEBDAV)
FLAVORS: Mapping[str, Flavor] = {
    NEXTCLOUD.kind: Flavor("nextcloud", stable_ids=True, web="fileid"),
    NEXTCLOUD_OAUTH.kind: Flavor("nextcloud", stable_ids=True, web="fileid"),
    OWNCLOUD.kind: Flavor("owncloud", stable_ids=True, web="fileid"),
    SEAFILE.kind: Flavor("address", stable_ids=False, web="file"),
    VK_WORKSPACE.kind: Flavor("address", stable_ids=False, web="file"),
    MAILRU.kind: Flavor("mailru", stable_ids=False, web="mailru"),
    WEBDAV.kind: Flavor("address", stable_ids=False, web="file"),
}
