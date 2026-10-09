"""Корневой сертификат НУЦ Минцифры для клиента Точки — при сборке образа.

API Точки (enter.tochka.com) подписан «Russian Trusted Root CA», которого
нет в стандартных хранилищах. Сертификат лежит в репозитории
(deploy/certs/, скачан с Госуслуг и сверен по двум адресам 09.10): сборка
не зависит от доступности госсайта — с машин GitHub за рубежом он
соединения обрывает. Источник — этот файл или https-адрес (build-arg),
в обоих случаях проверяется SHA-256 отпечаток DER: не тот сертификат —
сборка падает. В образе он лежит отдельным файлом и
подключается только к клиенту Точки (TOCHKA_CA_FILE), а не в системное
хранилище: остальные исходящие запросы ему не доверяют.

Запуск: python fetch_root_ca.py <файл или https-адрес> <sha256> <путь>
"""

import hashlib
import ssl
import sys
import urllib.request
from pathlib import Path


def _read(source: str) -> str:
    if "://" not in source:
        return Path(source).read_text(encoding="ascii")
    if not source.startswith("https://"):
        raise SystemExit("root CA URL must be https://")
    with urllib.request.urlopen(source, timeout=30) as response:  # noqa: S310 — https выше
        return str(response.read(64 * 1024).decode("ascii"))


def main(source: str, expected: str, target: str) -> None:
    pem = _read(source)
    der = ssl.PEM_cert_to_DER_cert(pem)
    actual = hashlib.sha256(der).hexdigest()
    expected = expected.replace(":", "").lower()
    if actual != expected:
        raise SystemExit(f"root CA fingerprint mismatch: {actual} != {expected}")
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Нормализованный PEM из проверенного DER — без лишнего в файле.
    path.write_text(ssl.DER_cert_to_PEM_cert(der), encoding="ascii")
    print(f"root CA saved to {path}, sha256 {actual}")


if __name__ == "__main__":
    main(*sys.argv[1:4])
