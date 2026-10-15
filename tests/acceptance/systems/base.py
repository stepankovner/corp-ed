"""Драйвер системы: как сценарий заводит подключение и меняет данные в ней.

Драйвер отвечает за три вещи:

- какое подключение kronto собрать (config, режим, учётные данные
  организации или каждого сотрудника);
- как разложить корпус и права из манифеста по системе (`seed`, только для
  Docker-стендов, у облачных кабинетов этим займётся владелец продукта);
- как выполнить шаги изменений: правка, удаление, отзыв доступа, перенос
  в закрытую папку.

Не умеет драйвер чего-то (например, облако без засева) — шаг получает
статус skip с причиной, а не fail.
"""

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from corp_ed.domain.types import ConnectorMode
from tests.acceptance.corpus.build import SUFFIX, read_source, render
from tests.acceptance.model import Document, Expected, Manifest, System


class NotSupportedError(Exception):
    """Драйвер не умеет эту операцию: шаг пропускается, не падает."""


@dataclass
class Creds:
    """Всё, что нужно знать о системе, чтобы к ней подключиться.

    config — несекретные поля подключения (адрес, папки); organization —
    учётные данные организации (режим organization); app — секреты
    приложения (OAuth client_secret); users — логин → учётные данные
    сотрудника (режим per_user: пароль приложения, токен, OAuth-токены).
    extra — служебное стенда (адрес, пароль администратора для засева).
    """

    config: dict[str, str] = field(default_factory=dict)
    organization: dict[str, str] | None = None
    app: dict[str, str] | None = None
    users: dict[str, dict[str, str]] = field(default_factory=dict)
    extra: dict[str, str] = field(default_factory=dict)

    def as_json(self) -> dict[str, Any]:
        return {
            "config": self.config,
            "organization": self.organization,
            "app": self.app,
            "users": self.users,
            "extra": self.extra,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Creds":
        return cls(
            config=dict(data.get("config") or {}),
            organization=data.get("organization"),
            app=data.get("app"),
            users={k: dict(v) for k, v in (data.get("users") or {}).items()},
            extra=dict(data.get("extra") or {}),
        )


ENV_FILE = "ACC_CREDENTIALS"


def credentials_path(explicit: str | None) -> Path | None:
    value = explicit or os.environ.get(ENV_FILE)
    return Path(value).expanduser() if value else None


def read_credentials(path: Path | None, system_id: str) -> Creds | None:
    if path is None or not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    entry = data.get(system_id)
    return Creds.from_json(entry) if entry else None


def write_credentials(path: Path, system_id: str, creds: Creds) -> None:
    """Дописать систему в файл учётных данных (вне репозитория, 0600)."""
    data: dict[str, Any] = {}
    if path.exists():
        data = json.loads(path.read_text(encoding="utf-8"))
    data[system_id] = creds.as_json()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    path.chmod(0o600)


def env_credentials(
    system_id: str,
    config_fields: list[str],
    org_fields: list[str],
    app_fields: list[str],
    user_fields: list[str],
    logins: list[str],
    environ: dict[str, str] | None = None,
) -> Creds | None:
    """Учётные данные из переменных окружения.

    Подключение: ACC_<СИСТЕМА>_<ПОЛЕ> (ACC_BOOKSTACK_BASE_URL,
    ACC_BOOKSTACK_TOKEN_ID); сотрудник: ACC_<СИСТЕМА>_<ЛОГИН>_<ПОЛЕ>
    (ACC_NEXTCLOUD_VETROV_PASSWORD). Система и поле — в верхнем регистре.
    """
    env = dict(os.environ if environ is None else environ)
    prefix = f"ACC_{system_id.upper()}_"

    def pick(names: list[str], base: str = prefix) -> dict[str, str]:
        return {
            name: env[base + name.upper()]
            for name in names
            if base + name.upper() in env
        }

    config = pick(config_fields)
    organization = pick(org_fields)
    app = pick(app_fields)
    users = {
        login: found
        for login in logins
        if (found := pick(user_fields, f"{prefix}{login.upper()}_"))
    }
    if not (config or organization or app or users):
        return None
    return Creds(
        config=config,
        organization=organization or None,
        app=app or None,
        users=users,
    )


def merge(base: Creds | None, over: Creds | None) -> Creds | None:
    """Переменные окружения перекрывают файл по полям."""
    if base is None or over is None:
        return over or base
    users = {k: dict(v) for k, v in base.users.items()}
    for login, fields in over.users.items():
        users.setdefault(login, {}).update(fields)
    return Creds(
        config={**base.config, **over.config},
        organization=(
            {**(base.organization or {}), **over.organization}
            if over.organization
            else base.organization
        ),
        app={**(base.app or {}), **over.app} if over.app else base.app,
        users=users,
        extra=base.extra,
    )


# --- файлы корпуса в системе ------------------------------------------------------


def file_name(document: Document, fmt: str) -> str:
    """Имя файла в системе: название документа и расширение формата."""
    cleaned = re.sub(r'[\\/:*?"<>|]', " ", document.title).strip()
    return f"{cleaned}{SUFFIX[fmt]}"


def render_document(
    document: Document, fmt: str, edits: list[str] | tuple[str, ...] = ()
) -> bytes:
    return render(read_source(document.file), fmt, edits)


@dataclass(frozen=True)
class Placement:
    """Документ в системе сейчас: где (зона, подпапка) и в каком виде."""

    document: Document
    zone: str
    fmt: str
    edits: tuple[str, ...] = ()

    @property
    def name(self) -> str:
        return file_name(self.document, self.fmt)

    def content(self) -> bytes:
        return render_document(self.document, self.fmt, self.edits)

    def text(self) -> str:
        """Исходник в Markdown с правками — для систем со страницами."""
        return render_document(self.document, "md", self.edits).decode()


def placements(expected: Expected, system: System) -> list[Placement]:
    fmt_of = (
        (lambda doc: "md") if system.storage == "pages" else (lambda doc: doc.format)
    )
    return [
        Placement(
            state.document, state.zone, fmt_of(state.document), tuple(state.edits)
        )
        for state in expected.docs.values()
        if state.present
    ]


# --- драйвер -----------------------------------------------------------------------


class Driver:
    """Базовый драйвер: подключение берётся из Creds, изменения не умеет."""

    mode: ConnectorMode = ConnectorMode.ORGANIZATION
    modules: tuple[str, ...] = ()
    seedable = False
    """Может разложить корпус по системе (Docker-стенд)."""
    default_port: int | None = None
    config_required = True
    """У вида есть обязательные поля config (адрес и т. п.)."""

    def __init__(self, system: System, manifest: Manifest, creds: Creds | None) -> None:
        self.system = system
        self.manifest = manifest
        self.creds = creds

    # подключение

    def stand_url(self) -> str | None:
        """Адрес Docker-стенда (для доверия к его сертификату и снятия
        проверки «только публичные адреса»); у облака — None."""
        if self.system.cloud or self.creds is None:
            return None
        return self.creds.extra.get("url")

    def config(self) -> dict[str, str]:
        assert self.creds is not None
        return dict(self.creds.config)

    def unavailable(self) -> str | None:
        """Причина, по которой систему нельзя проверить (нет учётных
        данных), или None."""
        if self.creds is None:
            return "нет учётных данных (файл ACC_CREDENTIALS или переменные ACC_…)"
        if self.config_required and not self.creds.config:
            return "в учётных данных нет config подключения"
        if self.mode is ConnectorMode.ORGANIZATION and not self.creds.organization:
            return "нет учётных данных организации"
        if self.mode is ConnectorMode.PER_USER and not self.creds.users:
            return "нет учётных данных ни одного сотрудника"
        return None

    # засев и шаги: по умолчанию не умеем

    def seed(self, expected: Expected) -> Creds:
        raise NotSupportedError(
            "засев не написан: раскладка по манифесту — вручную "
            "(python -m tests.acceptance.run --plan)"
        )

    def edit(self, placement: Placement) -> None:
        raise NotSupportedError("правка документа не поддержана драйвером")

    def delete(self, placement: Placement) -> None:
        raise NotSupportedError("удаление документа не поддержано драйвером")

    def revoke(self, login: str, zones: list[str]) -> None:
        raise NotSupportedError("отзыв доступа не поддержан драйвером")

    def move(self, placement: Placement, to_zone: str) -> None:
        raise NotSupportedError("перенос документа не поддержан драйвером")
