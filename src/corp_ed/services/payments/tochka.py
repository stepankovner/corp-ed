"""API Точки: счета, ссылки с чеком, подписки по карте, акты, вебхуки.

Формы запросов и ответов — по swagger Точки
(https://enter.tochka.com/doc/openapi/swagger.json, проверено 09.10.2026)
и документации developers.tochka.com:

- авторизация — JWT-ключ из интернет-банка в заголовке
  Authorization: Bearer; тело запросов и ответов — {"Data": {...}};
- счёт — POST invoice/v1.0/bills, статус — …/payment-status
  (payment_waiting / payment_paid / payment_expired), PDF — …/file;
- ссылка с чеком — POST acquiring/v1.0/payments_with_receipt, подписка
  с чеком — POST acquiring/v1.0/subscriptions_with_receipt (только карта,
  период Month), отмена — …/subscriptions/{operationId}/status
  Cancelled, состояние — GET acquiring/v1.0/payments/{operationId};
- акт — POST invoice/v1.0/closing-documents (Content.Act, без НДС), PDF
  и отправка на почту — …/file и …/email;
- вебхук — POST с Content-Type: text/plain, в теле голая строка JWT,
  подписанная RS256 ключом Точки; повтор 30 раз через 10 с, пока не
  ответим 200. Регистрируется PUT webhook/v1.0/{client_id} (только
  HTTPS на 443).

Сервер Точки подписан корневым сертификатом НУЦ Минцифры: клиент
(tochka_http_client) доверяет ему и обычным корням, остальные исходящие
клиенты процесса — нет.
"""

import json
import ssl
from typing import Any
from urllib.parse import quote

import certifi
import httpx
import jwt
import structlog
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from corp_ed.core.config import TochkaSettings
from corp_ed.domain.billing import kopecks_from_rubles, rubles
from corp_ed.services.payments.provider import (
    ActRequest,
    BillRequest,
    BillStatus,
    InvalidWebhookError,
    LinkRequest,
    Party,
    PaymentInfo,
    PaymentProviderError,
    ProviderLink,
    WebhookEvent,
)

logger = structlog.get_logger()

WEBHOOK_TYPES = ("incomingPayment", "acquiringInternetPayment")
MAX_WEBHOOK_BYTES = 64 * 1024
MAX_PDF_BYTES = 10 * 1024 * 1024
SUBSCRIPTION_TRANCHES = 84
"""Максимум списаний для периода Month (документация Точки). Продлить
подписку нельзя — после последнего списания нужна новая."""
UNIT = "усл.ед."
PURPOSE_LIMIT = 140
"""Назначение у ссылки — не длиннее 140 знаков (swagger)."""

_BILL_STATUSES = {
    "payment_waiting": BillStatus.WAITING,
    "payment_paid": BillStatus.PAID,
    "payment_expired": BillStatus.EXPIRED,
}


def tochka_ssl_context(ca_file: str) -> ssl.SSLContext:
    """Обычные корни (certifi) плюс корневой сертификат Минцифры. Нет
    файла — ошибка старта: без него ни один запрос к банку не пройдёт, а
    узнать об этом лучше при выкатке, чем на первом счёте."""
    context = ssl.create_default_context(cafile=certifi.where())
    try:
        context.load_verify_locations(cafile=ca_file)
    except (OSError, ssl.SSLError) as exc:
        raise RuntimeError(
            f"TOCHKA_CA_FILE {ca_file} is missing or not a certificate "
            "(Russian Trusted Root CA, see Dockerfile)"
        ) from exc
    return context


def tochka_http_client(
    settings: TochkaSettings, *, via_proxy: bool
) -> httpx.AsyncClient:
    """Клиент только для API Точки (tochka_ssl_context).

    trust_env — как у остальных исходящих (CONNECTOR_OUTBOUND_VIA_PROXY):
    прокси из окружения — только в этом режиме. Явный verify делает
    SSL_CERT_FILE ненужным: корень Минцифры не попадает в общие клиенты
    (core/outbound.py) — им по-прежнему доверяют только обычные корни.
    """
    return httpx.AsyncClient(
        verify=tochka_ssl_context(settings.ca_file),
        trust_env=via_proxy,
        timeout=settings.timeout_seconds,
    )


def load_webhook_key(value: str) -> Any:
    """Публичный ключ Точки: JWK (JSON, как на странице банка) или PEM."""
    text = value.strip()
    try:
        if text.startswith("{"):
            return jwt.PyJWK(json.loads(text), algorithm="RS256").key
        key = serialization.load_pem_public_key(text.encode())
    except (ValueError, TypeError, jwt.PyJWKError) as exc:
        raise RuntimeError("TOCHKA_WEBHOOK_PUBLIC_KEY is not a valid RSA key") from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise RuntimeError("TOCHKA_WEBHOOK_PUBLIC_KEY is not a valid RSA key")
    return key


def _party(party: Party) -> dict[str, Any]:
    side: dict[str, Any] = {
        "taxCode": party.inn,
        "type": party.type,
        "secondSideName": party.name,
        "legalAddress": party.address,
    }
    if party.kpp:
        side["kpp"] = party.kpp
    return side


def _positions(lines: list[Any]) -> list[dict[str, Any]]:
    return [
        {
            "positionName": line.name[:500],
            "unitCode": UNIT,
            "ndsKind": "without_nds",
            "price": rubles(line.amount_kopecks),
            "quantity": 1,
            "totalAmount": rubles(line.amount_kopecks),
            "totalNds": 0,
        }
        for line in lines
    ]


def _receipt_items(lines: list[Any]) -> list[dict[str, Any]]:
    """Позиции чека (54-ФЗ): услуга, полная предоплата, без НДС (УСН)."""
    return [
        {
            "name": line.name[:256],
            "amount": rubles(line.amount_kopecks),
            "quantity": 1,
            "vatType": "none",
            "paymentMethod": "full_prepayment",
            "paymentObject": "service",
        }
        for line in lines
    ]


class TochkaProvider:
    name = "tochka"

    def __init__(self, client: httpx.AsyncClient, settings: TochkaSettings) -> None:
        missing = settings.missing()
        if missing:
            raise RuntimeError(
                "PAYMENTS_PROVIDER=tochka requires " + ", ".join(missing)
            )
        self.client = client
        self.settings = settings
        self.base = settings.api_url
        self.customer = settings.customer_code
        self.webhook_key = load_webhook_key(settings.webhook_public_key)

    # --- HTTP -------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        assert self.settings.jwt is not None  # noqa: S101 — проверено в __init__
        return {
            "Authorization": f"Bearer {self.settings.jwt.get_secret_value()}",
            "Accept": "application/json",
        }

    async def _send(
        self, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> httpx.Response:
        try:
            response = await self.client.request(
                method,
                self.base + path,
                headers=self._headers(),
                json=body,
                timeout=self.settings.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise PaymentProviderError("tochka_timeout", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise PaymentProviderError("tochka_unavailable", retryable=True) from exc
        if response.status_code >= 400:
            # Тело ошибки — в лог кодом и путём, без заголовков: в них ключ.
            logger.warning(
                "tochka_request_failed",
                method=method,
                path=path.split("/")[0:3],
                status=response.status_code,
            )
            raise PaymentProviderError(
                f"tochka_http_{response.status_code}",
                retryable=response.status_code >= 500 or response.status_code == 429,
            )
        return response

    async def _data(
        self, method: str, path: str, *, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        response = await self._send(method, path, body=body)
        try:
            data = response.json()["Data"]
        except (ValueError, KeyError, TypeError) as exc:
            raise PaymentProviderError("tochka_bad_response", retryable=True) from exc
        if not isinstance(data, dict):
            raise PaymentProviderError("tochka_bad_response", retryable=True)
        return data

    async def _pdf(self, path: str) -> bytes | None:
        response = await self._send("GET", path)
        content = response.content
        if not content.startswith(b"%PDF") or len(content) > MAX_PDF_BYTES:
            logger.warning("tochka_pdf_unexpected", size=len(content))
            return None
        return content

    def _doc(self, kind: str, ref: str) -> str:
        return f"invoice/v1.0/{kind}/{quote(self.customer)}/{quote(ref, safe='')}"

    # --- счета ------------------------------------------------------------------------

    async def create_bill(self, request: BillRequest) -> str:
        invoice: dict[str, Any] = {
            "Positions": _positions(request.lines),
            "date": request.issued.isoformat(),
            "totalAmount": rubles(request.total_kopecks),
            "totalNds": 0,
            "number": request.number,
        }
        if request.comment:
            invoice["comment"] = request.comment[:500]
        data = await self._data(
            "POST",
            "invoice/v1.0/bills",
            body={
                "Data": {
                    "accountId": self.settings.account_id,
                    "customerCode": self.customer,
                    "SecondSide": _party(request.party),
                    "Content": {"Invoice": invoice},
                }
            },
        )
        ref = data.get("documentId")
        if not isinstance(ref, str) or not ref:
            raise PaymentProviderError("tochka_bad_response", retryable=True)
        return ref

    async def bill_status(self, ref: str) -> BillStatus:
        data = await self._data("GET", self._doc("bills", ref) + "/payment-status")
        status = _BILL_STATUSES.get(str(data.get("paymentStatus")))
        if status is None:
            raise PaymentProviderError("tochka_bad_response", retryable=True)
        return status

    async def bill_pdf(self, ref: str) -> bytes | None:
        return await self._pdf(self._doc("bills", ref) + "/file")

    async def delete_bill(self, ref: str) -> None:
        await self._send("DELETE", self._doc("bills", ref))

    # --- ссылки и подписки ------------------------------------------------------------

    def _link_body(self, request: LinkRequest) -> dict[str, Any]:
        client: dict[str, Any] = {"email": request.email}
        if request.payer_name:
            client["name"] = request.payer_name[:256]
        data: dict[str, Any] = {
            "customerCode": self.customer,
            "amount": rubles(request.total_kopecks),
            "purpose": request.purpose[:PURPOSE_LIMIT],
            "paymentLinkId": request.order_id[:45],
            "taxSystemCode": self.settings.tax_system_code,
            "Client": client,
            "Items": _receipt_items(request.lines),
        }
        if request.return_url:
            data["redirectUrl"] = request.return_url
            data["failRedirectUrl"] = request.return_url
        if self.settings.merchant_id:
            data["merchantId"] = self.settings.merchant_id
        return data

    @staticmethod
    def _link(data: dict[str, Any]) -> ProviderLink:
        ref, url = data.get("operationId"), data.get("paymentLink")
        if not (isinstance(ref, str) and ref and isinstance(url, str)):
            raise PaymentProviderError("tochka_bad_response", retryable=True)
        if not url.startswith("https://"):
            raise PaymentProviderError("tochka_bad_response", retryable=False)
        return ProviderLink(ref=ref, url=url)

    async def create_link(self, request: LinkRequest) -> ProviderLink:
        body = self._link_body(request)
        body["paymentMode"] = ["card", "sbp"]
        data = await self._data(
            "POST", "acquiring/v1.0/payments_with_receipt", body={"Data": body}
        )
        return self._link(data)

    async def create_card_subscription(self, request: LinkRequest) -> ProviderLink:
        body = self._link_body(request)
        body["Options"] = {"period": "Month", "trancheCount": SUBSCRIPTION_TRANCHES}
        data = await self._data(
            "POST", "acquiring/v1.0/subscriptions_with_receipt", body={"Data": body}
        )
        return self._link(data)

    async def cancel_card_subscription(self, ref: str) -> None:
        await self._send(
            "POST",
            f"acquiring/v1.0/subscriptions/{quote(ref, safe='')}/status",
            body={"Data": {"status": "Cancelled"}},
        )

    async def payment_info(self, ref: str) -> PaymentInfo:
        data = await self._data("GET", f"acquiring/v1.0/payments/{quote(ref, safe='')}")
        operations = data.get("Operation")
        if not isinstance(operations, list) or not operations:
            raise PaymentProviderError("tochka_bad_response", retryable=True)
        operation = operations[0]
        try:
            amount = kopecks_from_rubles(operation.get("amount"))
        except ValueError:
            amount = None
        charges = [
            str(order.get("orderId"))
            for order in operation.get("Order") or []
            if isinstance(order, dict)
            and order.get("type") == "approval"
            and order.get("orderId")
        ]
        return PaymentInfo(
            approved=operation.get("status") == "APPROVED",
            amount_kopecks=amount,
            charges=charges,
        )

    # --- акты -------------------------------------------------------------------------

    async def create_act(self, request: ActRequest) -> str | None:
        body: dict[str, Any] = {
            "accountId": self.settings.account_id,
            "customerCode": self.customer,
            "SecondSide": _party(request.party),
            "Content": {
                "Act": {
                    "Positions": _positions(request.lines),
                    "date": request.issued.isoformat(),
                    "totalAmount": rubles(request.total_kopecks),
                    "totalNds": 0,
                    "number": request.number,
                }
            },
        }
        if request.bill_ref:
            body["documentId"] = request.bill_ref
        data = await self._data(
            "POST", "invoice/v1.0/closing-documents", body={"Data": body}
        )
        ref = data.get("documentId")
        return ref if isinstance(ref, str) and ref else None

    async def act_pdf(self, ref: str) -> bytes | None:
        return await self._pdf(self._doc("closing-documents", ref) + "/file")

    async def send_act(self, ref: str, email: str) -> None:
        await self._send(
            "POST",
            self._doc("closing-documents", ref) + "/email",
            body={"Data": {"email": email}},
        )

    # --- вебхуки ----------------------------------------------------------------------

    async def register_webhook(self, url: str) -> None:
        """Подписать client_id ключа на входящие платежи и оплату по
        ссылкам. Банк сразу шлёт тестовый вебхук на каждое событие и ждёт
        200 — сервер с этим адресом должен уже работать."""
        if not self.settings.client_id:
            raise RuntimeError("TOCHKA_CLIENT_ID is required to register a webhook")
        await self._send(
            "PUT",
            f"webhook/v1.0/{quote(self.settings.client_id, safe='')}",
            body={"webhooksList": list(WEBHOOK_TYPES), "url": url},
        )

    def parse_webhook(self, body: bytes) -> WebhookEvent:
        if len(body) > MAX_WEBHOOK_BYTES:
            raise InvalidWebhookError("body too large")
        try:
            token = body.decode("ascii").strip()
            payload = jwt.decode(
                token,
                self.webhook_key,
                algorithms=["RS256"],
                options={"verify_aud": False},
            )
        except (UnicodeDecodeError, jwt.InvalidTokenError) as exc:
            raise InvalidWebhookError(str(exc)) from exc
        if not isinstance(payload, dict):
            raise InvalidWebhookError("payload is not an object")
        return _event(payload)


def _text(value: object, limit: int) -> str | None:
    return str(value)[:limit] if value not in (None, "") else None


def _amount(value: object) -> int | None:
    try:
        return kopecks_from_rubles(value)
    except ValueError:
        return None


def _event(payload: dict[str, Any]) -> WebhookEvent:
    kind = payload.get("webhookType")
    customer = _text(payload.get("customerCode"), 32)
    if kind == "incomingPayment":
        payer = payload.get("SidePayer")
        payer = payer if isinstance(payer, dict) else {}
        amount = _amount(payer.get("amount", payload.get("amount")))
        return WebhookEvent(
            kind="incoming",
            payment_id=_text(payload.get("paymentId"), 64),
            amount_kopecks=amount,
            payer_inn=_text(payer.get("inn"), 12),
            payer_name=_text(payer.get("name"), 300),
            purpose=_text(payload.get("purpose"), 500),
            customer_code=customer,
            raw=payload,
        )
    if kind == "acquiringInternetPayment":
        return WebhookEvent(
            kind="acquiring",
            payment_id=_text(payload.get("operationId"), 64),
            amount_kopecks=_amount(payload.get("amount")),
            payer_name=_text(payload.get("payerName"), 300),
            purpose=_text(payload.get("purpose"), 500),
            status=_text(payload.get("status"), 32),
            link_id=_text(payload.get("paymentLinkId"), 64),
            customer_code=customer,
            raw=payload,
        )
    return WebhookEvent(kind="other", customer_code=customer, raw=payload)
