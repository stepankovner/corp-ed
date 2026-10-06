"""Подпись запросов AWS Signature Version 4 — для Yandex Cloud Postbox.

Postbox совместим с API Amazon SES v2 и принимает статический ключ
сервисного аккаунта с подписью SigV4 (регион ru-central1, сервис ses).
Целиком boto3 ради одной подписи не тянем: алгоритм короткий и проверен
тестовыми векторами AWS (tests/test_mail.py).

https://docs.aws.amazon.com/IAM/latest/UserGuide/create-signed-request.html
"""

import hashlib
import hmac
from datetime import datetime
from urllib.parse import quote, urlsplit

ALGORITHM = "AWS4-HMAC-SHA256"


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def sign(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes,
    key_id: str,
    secret: str,
    region: str,
    service: str,
    now: datetime,
) -> dict[str, str]:
    """Заголовки запроса с подписью: переданные + host, x-amz-date и
    Authorization. now — время UTC; подпись живёт около 15 минут."""
    parts = urlsplit(url)
    amz_date = now.strftime("%Y%m%dT%H%M%SZ")
    day = amz_date[:8]
    signed = {
        **{name.lower(): value.strip() for name, value in headers.items()},
        "host": parts.netloc,
        "x-amz-date": amz_date,
    }
    names = sorted(signed)
    canonical = "\n".join(
        [
            method.upper(),
            quote(parts.path or "/", safe="/-_.~"),
            _canonical_query(parts.query),
            "".join(f"{name}:{signed[name]}\n" for name in names),
            ";".join(names),
            hashlib.sha256(body).hexdigest(),
        ]
    )
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(
        [ALGORITHM, amz_date, scope, hashlib.sha256(canonical.encode()).hexdigest()]
    )
    key = _hmac(
        _hmac(_hmac(_hmac(f"AWS4{secret}".encode(), day), region), service),
        "aws4_request",
    )
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        **headers,
        "Host": parts.netloc,
        "X-Amz-Date": amz_date,
        "Authorization": (
            f"{ALGORITHM} Credential={key_id}/{scope}, "
            f"SignedHeaders={';'.join(names)}, Signature={signature}"
        ),
    }


def _canonical_query(query: str) -> str:
    if not query:
        return ""
    pairs = []
    for item in query.split("&"):
        name, _, value = item.partition("=")
        pairs.append((quote(name, safe="-_.~"), quote(value, safe="-_.~")))
    return "&".join(f"{name}={value}" for name, value in sorted(pairs))
