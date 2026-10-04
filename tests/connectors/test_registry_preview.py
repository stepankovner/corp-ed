"""Непроверенные модули скрыты, пока их не включили (решение 28.09);
виды целиком скрываются CONNECTOR_HIDDEN_KINDS."""

from corp_ed.connectors.registry import default_registry
from corp_ed.core.config import ConnectorSettings


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
    assert kinds == ["bitrix24", "confluence", "yandex360"]


def test_hidden_kinds_leave_the_catalog_but_keep_working() -> None:
    settings = ConnectorSettings(hidden_kinds=" yandex360 , bitrix24 ")
    registry = default_registry(settings)

    assert [spec.kind for spec in registry.kinds()] == ["confluence"]
    assert not registry.offered("yandex360")
    assert registry.offered("confluence")
    assert not registry.offered("salesforce")
    # Заведённые раньше подключения: спецификация на месте.
    assert registry.spec("yandex360").kind == "yandex360"


def test_misspelt_hidden_kind_is_reported() -> None:
    registry = default_registry(ConnectorSettings(hidden_kinds="yandex,confluence"))
    assert registry.unknown_hidden() == frozenset({"yandex"})
    assert [spec.kind for spec in registry.kinds()] == ["bitrix24", "yandex360"]
