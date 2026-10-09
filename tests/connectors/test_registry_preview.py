"""Непроверенные модули скрыты, пока их не включили (решение 28.09);
виды целиком скрываются CONNECTOR_HIDDEN_KINDS."""

from corp_ed.connectors.registry import (
    AdapterRegistry,
    KindSpec,
    ModuleSpec,
    default_registry,
)
from corp_ed.core.config import ConnectorSettings
from corp_ed.domain.types import ConnectorMode


def _modules(settings: ConnectorSettings) -> set[str]:
    return set(default_registry(settings).spec("bitrix24").module_names)


def test_knowledge_base_v2_is_hidden_by_default() -> None:
    assert "knowledge_base_v2" not in _modules(ConnectorSettings())
    assert {"disk", "disk_personal", "knowledge_base"} <= _modules(ConnectorSettings())


def test_flag_enables_preview_module() -> None:
    settings = ConnectorSettings(preview_modules=" knowledge_base_v2 , other ")
    assert settings.enabled_preview_modules == frozenset({"knowledge_base_v2", "other"})
    assert "knowledge_base_v2" in _modules(settings)


def test_nothing_is_hidden_by_default() -> None:
    kinds = [spec.kind for spec in default_registry(ConnectorSettings()).kinds()]
    assert kinds == [
        "bitrix24",
        "confluence",
        "nextcloud",
        "nextcloud_oauth",
        "outline",
        "owncloud",
        "seafile",
        "webdav",
        "yandex360",
    ]


def test_hidden_kinds_leave_the_catalog_but_keep_working() -> None:
    settings = ConnectorSettings(hidden_kinds=" yandex360 , bitrix24 ")
    registry = default_registry(settings)

    assert [spec.kind for spec in registry.kinds()] == [
        "confluence",
        "nextcloud",
        "nextcloud_oauth",
        "outline",
        "owncloud",
        "seafile",
        "webdav",
    ]
    assert not registry.offered("yandex360")
    assert registry.offered("confluence")
    assert not registry.offered("salesforce")
    # Заведённые раньше подключения: спецификация на месте.
    assert registry.spec("yandex360").kind == "yandex360"


def test_misspelt_hidden_kind_is_reported() -> None:
    registry = default_registry(ConnectorSettings(hidden_kinds="yandex,confluence"))
    assert registry.unknown_hidden() == frozenset({"yandex"})
    assert [spec.kind for spec in registry.kinds()] == [
        "bitrix24",
        "nextcloud",
        "nextcloud_oauth",
        "outline",
        "owncloud",
        "seafile",
        "webdav",
        "yandex360",
    ]


def _preview_kind_registry(enabled: frozenset[str] = frozenset()) -> AdapterRegistry:
    registry = AdapterRegistry(enabled_preview_kinds=enabled)
    for kind, preview in (("ready", False), ("fresh", True)):
        registry.register(
            KindSpec(
                kind=kind,
                title=kind,
                mode=ConnectorMode.ORGANIZATION,
                modules=(ModuleSpec("pages", "Страницы"),),
                preview=preview,
            ),
            lambda *_: None,  # type: ignore[arg-type,return-value]
        )
    return registry


def test_unverified_kind_is_not_offered_until_enabled() -> None:
    registry = _preview_kind_registry()
    assert [spec.kind for spec in registry.kinds()] == ["ready"]
    assert not registry.offered("fresh")
    # Живой проверке (`cli connector-check`) спецификация доступна.
    assert registry.spec("fresh").preview

    enabled = _preview_kind_registry(frozenset({"fresh"}))
    assert [spec.kind for spec in enabled.kinds()] == ["fresh", "ready"]


def test_preview_kinds_setting() -> None:
    settings = ConnectorSettings(preview_kinds=" webdav , gdrive ")
    assert settings.enabled_preview_kinds == frozenset({"webdav", "gdrive"})
