"""Набор для глобальной проверки коннекторов: вымышленная компания,
документы во всех системах и сценарий, который проверяет полный путь каждой
системы через ядро kronto. См. README.md рядом."""

import os
from pathlib import Path

# Сертификат Docker-стендов приёмки (stand.py создаёт его при первом `up`).
# Модули живых стендов читают DAV_STAND_CA при импорте, поэтому значение
# по умолчанию выставляется здесь, до их загрузки.
_CA = Path("~/.kronto-acc/tls/cert.pem").expanduser()
if _CA.exists():
    os.environ.setdefault("DAV_STAND_CA", str(_CA))
