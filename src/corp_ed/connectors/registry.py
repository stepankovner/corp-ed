"""Каталог видов коннекторов: что бывает, какие поля и какой режим.

Спецификация (KindSpec) нужна дважды: фронту — чтобы построить форму
подключения без знания о системах, ядру — чтобы проверить config и
credentials до записи в базу (лишних и незнакомых полей быть не должно,
адрес портала проходит проверку SSRF).

Адаптеры регистрируются при импорте своего модуля; тесты собирают свой
реестр с поддельным адаптером и подставляют его через зависимость —
поэтому реестр — объект, а не глобальный словарь.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum

from corp_ed.connectors.base import OAuthFlow, SourceAdapter
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode


@dataclass(frozen=True)
class FieldSpec:
    name: str
    title: str
    required: bool = True
    # Секретные поля живут только в credentials, шифруются и не
    # возвращаются; несекретные — в config, видны админу.
    secret: bool = False


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    title: str


class UserAuth(StrEnum):
    """Как сотрудник авторизуется в режиме per_user.

    FIELDS — вводит значения credential_fields руками (постоянный токен);
    OAUTH — проходит редирект на систему, токены приходят обменом кода
    (ручки /connectors/{id}/oauth/start и /connectors/oauth/callback),
    credential_fields пусты.
    """

    FIELDS = "fields"
    OAUTH = "oauth"


@dataclass(frozen=True)
class KindSpec:
    kind: str
    title: str
    mode: ConnectorMode
    modules: tuple[ModuleSpec, ...]
    config_fields: tuple[FieldSpec, ...] = ()
    # Учётные данные подключения (режим organization) или сотрудника
    # (режим per_user с UserAuth.FIELDS): что именно кладётся в SecretBox.
    credential_fields: tuple[FieldSpec, ...] = ()
    # Режим per_user: секреты самого приложения (client_secret), которые
    # задаёт админ через PUT /connectors/{id}/credentials. Хранятся в
    # connectors.credentials, при работе с источником складываются с
    # учётными данными сотрудника.
    app_credential_fields: tuple[FieldSpec, ...] = ()
    user_auth: UserAuth = UserAuth.FIELDS
    # Поле config с адресом системы: проверяется validate_outbound_url.
    url_field: str | None = None
    # Подсказки фронту: нужные scope приложения, путь обратного вызова.
    extra: Mapping[str, str] = field(default_factory=dict)

    @property
    def module_names(self) -> frozenset[str]:
        return frozenset(module.name for module in self.modules)

    @property
    def oauth(self) -> bool:
        return self.user_auth is UserAuth.OAUTH


AdapterFactory = Callable[
    [KindSpec, Mapping[str, str], Mapping[str, str], OutboundClient], SourceAdapter
]
"""(spec, config, credentials, http) → адаптер. credentials — уже
расшифрованные (в режиме per_user — секреты приложения плюс токены
сотрудника одним словарём); фабрика вызывается только в момент работы
с источником."""

OAuthFactory = Callable[
    [KindSpec, Mapping[str, str], Mapping[str, str], OutboundClient], OAuthFlow
]
"""(spec, config, app_credentials, http) → OAuth-обмен вида."""


class UnknownKindError(KeyError):
    pass


class OAuthNotSupportedError(KeyError):
    """Вид без OAuth: сотрудник вводит учётные данные руками."""


class AdapterRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, KindSpec] = {}
        self._factories: dict[str, AdapterFactory] = {}
        self._oauth: dict[str, OAuthFactory] = {}

    def register(
        self,
        spec: KindSpec,
        factory: AdapterFactory,
        oauth: OAuthFactory | None = None,
    ) -> None:
        if spec.kind in self._specs:
            raise ValueError(f"connector kind already registered: {spec.kind}")
        if spec.oauth and oauth is None:
            raise ValueError(f"connector kind {spec.kind} declares OAuth without flow")
        self._specs[spec.kind] = spec
        self._factories[spec.kind] = factory
        if oauth is not None:
            self._oauth[spec.kind] = oauth

    def kinds(self) -> list[KindSpec]:
        return sorted(self._specs.values(), key=lambda spec: spec.kind)

    def spec(self, kind: str) -> KindSpec:
        try:
            return self._specs[kind]
        except KeyError as exc:
            raise UnknownKindError(kind) from exc

    def build(
        self,
        kind: str,
        config: Mapping[str, str],
        credentials: Mapping[str, str],
        http: OutboundClient,
    ) -> SourceAdapter:
        spec = self.spec(kind)
        return self._factories[kind](spec, config, credentials, http)

    def build_oauth(
        self,
        kind: str,
        config: Mapping[str, str],
        app_credentials: Mapping[str, str],
        http: OutboundClient,
    ) -> OAuthFlow:
        spec = self.spec(kind)
        factory = self._oauth.get(kind)
        if factory is None:
            raise OAuthNotSupportedError(kind)
        return factory(spec, config, app_credentials, http)


def default_registry(settings: ConnectorSettings) -> AdapterRegistry:
    """Реестр с адаптерами, которые есть в этой сборке.

    Пополняется по мере этапов MVP: Битрикс24 → Confluence → Яндекс 360.
    settings — лимит размера документа и адрес сервера авторизации:
    адаптеры получают их отсюда, а не читают окружение сами.
    """
    from corp_ed.connectors.bitrix24 import register as register_bitrix24
    from corp_ed.connectors.confluence import register as register_confluence
    from corp_ed.connectors.yandex import register as register_yandex

    registry = AdapterRegistry()
    register_bitrix24(registry, settings)
    register_confluence(registry, settings)
    register_yandex(registry, settings)
    return registry
