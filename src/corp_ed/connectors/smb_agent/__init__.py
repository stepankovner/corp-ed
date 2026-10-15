"""Сетевые папки через агента в сети клиента (docs/SMB-AGENT.md).

Вид без адаптера: файловый сервер из интернета не виден, поэтому
документы и права присылает агент (`agent/`) по ручкам
`/api/v1/agent/...` (api/v1/endpoints/agent.py,
services/agent_ingest_service.py). Расписание сервера этот вид не
запускает (KindSpec.push). Учётные данные подключения — хеш ключа агента;
выпускает ключ `cli agent-token`.
"""

from collections.abc import Mapping

from corp_ed.connectors.base import AdapterConfigError, AdapterOptions, SourceAdapter
from corp_ed.connectors.registry import AdapterRegistry, KindSpec, ModuleSpec
from corp_ed.core.config import ConnectorSettings
from corp_ed.core.outbound import OutboundClient
from corp_ed.domain.types import ConnectorMode

KIND = "smb_agent"

SMB_AGENT_SPEC = KindSpec(
    kind=KIND,
    title="Сетевые папки (агент в сети компании)",
    mode=ConnectorMode.ORGANIZATION,
    modules=(ModuleSpec("files", "Файлы и права NTFS"),),
    extra={
        "agent": "Документы присылает агент kronto, установленный в сети "
        "компании; ключ агента выпускает команда kronto"
    },
    base=False,
    preview=True,
    push=True,
)


def _no_pull(
    spec: KindSpec,
    config: Mapping[str, str],
    credentials: Mapping[str, str],
    http: OutboundClient,
    options: AdapterOptions,
) -> SourceAdapter:
    # Сюда не приходят: синхронизация пропускает виды push. Если всё же
    # пришли (проверка из CLI) — понятный код вместо падения.
    raise AdapterConfigError("push_only")


def register(registry: AdapterRegistry, settings: ConnectorSettings) -> None:
    registry.register(SMB_AGENT_SPEC, _no_pull)


__all__ = ["KIND", "SMB_AGENT_SPEC", "register"]
