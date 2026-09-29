"""Непроверенные модули скрыты, пока их не включили (решение 28.09)."""

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
