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

from corp_ed.connectors.base import SourceAdapter
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


@dataclass(frozen=True)
class KindSpec:
    kind: str
    title: str
    mode: ConnectorMode
    modules: tuple[ModuleSpec, ...]
    config_fields: tuple[FieldSpec, ...] = ()
    # Учётные данные подключения (режим organization) или сотрудника
    # (режим per_user): что именно кладётся в SecretBox.
    credential_fields: tuple[FieldSpec, ...] = ()
    # Поле config с адресом системы: проверяется validate_outbound_url.
    url_field: str | None = None
    extra: Mapping[str, str] = field(default_factory=dict)

    @property
    def module_names(self) -> frozenset[str]:
        return frozenset(module.name for module in self.modules)


AdapterFactory = Callable[
    [KindSpec, Mapping[str, str], Mapping[str, str], OutboundClient], SourceAdapter
]
"""(spec, config, credentials, http) → адаптер. credentials — уже
расшифрованные; фабрика вызывается только в момент работы с источником."""


class UnknownKindError(KeyError):
    pass


class AdapterRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, KindSpec] = {}
        self._factories: dict[str, AdapterFactory] = {}

    def register(self, spec: KindSpec, factory: AdapterFactory) -> None:
        if spec.kind in self._specs:
            raise ValueError(f"connector kind already registered: {spec.kind}")
        self._specs[spec.kind] = spec
        self._factories[spec.kind] = factory

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


def default_registry() -> AdapterRegistry:
    """Реестр с адаптерами, которые есть в этой сборке.

    Пополняется по мере этапов MVP: Битрикс24 → Confluence → Яндекс 360.
    """
    return AdapterRegistry()
