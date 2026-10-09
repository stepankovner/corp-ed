"""Поддельная Точка для контрактных тестов провайдера и биллинга.

Формы — по swagger Точки (09.10.2026): тело {"Data": {...}}, Bearer-ключ,
счёт → documentId, статус счёта payment_waiting/payment_paid/
payment_expired, ссылка и подписка → operationId и paymentLink, состояние
операции — Operation[0] со status и списаниями Order (type=approval),
акт → documentId, PDF — application/pdf. Вебхуки — строка JWT RS256,
подписанная ключом этого сервера (настоящий ключ Точки у нас только
публичный); тест подписи настоящим ключом — на примере из документации.
"""

import json
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import httpx
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from corp_ed.core.config import TochkaSettings
from corp_ed.services.payments.tochka import TochkaProvider

HOST = "enter.tochka.com"
TOKEN = "fake-tochka-jwt"  # noqa: S105 — поддельный сервер
CUSTOMER = "300123123"
ACCOUNT = "40802810000000000001/044525104"
CLIENT_ID = "fake-client-id"
PDF = b"%PDF-1.4\n% fake tochka document\n%%EOF\n"


class V:
    """Значения для сопоставления путей в match (имя без точки — захват)."""

    CUSTOMER = CUSTOMER
    CLIENT_ID = CLIENT_ID


_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def public_jwk() -> str:
    return json.dumps(
        jwt.algorithms.RSAAlgorithm.to_jwk(_KEY.public_key(), as_dict=True)
    )


def sign(payload: dict[str, Any], key: Any = None) -> bytes:
    return jwt.encode(payload, key or _KEY, algorithm="RS256").encode()


@dataclass
class FakeTochka:
    bills: dict[str, dict[str, Any]] = field(default_factory=dict)
    bill_status: dict[str, str] = field(default_factory=dict)
    links: dict[str, dict[str, Any]] = field(default_factory=dict)
    """operationId → тело ссылки или подписки и что с ней случилось."""
    acts: dict[str, dict[str, Any]] = field(default_factory=dict)
    emails: list[tuple[str, str]] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    webhooks: list[dict[str, Any]] = field(default_factory=list)
    calls: list[tuple[str, str]] = field(default_factory=list)
    fail: dict[str, int] = field(default_factory=dict)
    """Префикс пути → код ответа: «сломать» ручку."""
    pdf_bytes: bytes = PDF

    def settings(self, **overrides: Any) -> TochkaSettings:
        values: dict[str, Any] = {
            "jwt": TOKEN,
            "customer_code": CUSTOMER,
            "account_id": ACCOUNT,
            "client_id": CLIENT_ID,
            "merchant_id": "200000000001234",
            "webhook_public_key": public_jwk(),
            **overrides,
        }
        return TochkaSettings(**values)

    def provider(self, **overrides: Any) -> TochkaProvider:
        client = httpx.AsyncClient(transport=httpx.MockTransport(self.handle))
        return TochkaProvider(client, self.settings(**overrides))

    # --- что сделал бы банк -----------------------------------------------------------

    def pay_bill(self, ref: str) -> None:
        self.bill_status[ref] = "payment_paid"

    def approve(self, ref: str) -> str:
        """Списание по ссылке или очередное по подписке; номер списания."""
        link = self.links[ref]
        order_id = f"order-{len(link['orders']) + 1}-{ref[:8]}"
        link["orders"].append(order_id)
        link["status"] = "APPROVED"
        return order_id

    def incoming_webhook(
        self,
        *,
        amount: str,
        purpose: str,
        inn: str,
        payment_id: str | None = None,
        name: str = "ООО «Ромашка»",
        customer: str = CUSTOMER,
    ) -> bytes:
        side = {
            "bankCode": "044525104",
            "bankName": "ООО Банк Точка",
            "account": "40702810000000000002",
            "name": name,
            "amount": amount,
            "currency": "RUB",
            "inn": inn,
            "kpp": "773601001",
        }
        return sign(
            {
                "SidePayer": side,
                "SideRecipient": {**side, "inn": "500100732259", "name": "ИП Тест"},
                "purpose": purpose,
                "documentNumber": "123",
                "paymentId": payment_id or uuid4().hex,
                "date": "2026-10-09",
                "webhookType": "incomingPayment",
                "customerCode": customer,
            }
        )

    def acquiring_webhook(
        self, ref: str, *, status: str = "APPROVED", amount: str | None = None
    ) -> bytes:
        link = self.links[ref]["Data"]
        return sign(
            {
                "customerCode": CUSTOMER,
                "amount": amount or str(link["amount"]),
                "paymentType": "card",
                "operationId": ref,
                "purpose": link["purpose"],
                "webhookType": "acquiringInternetPayment",
                "merchantId": "200000000001234",
                "status": status,
                "paymentLinkId": link.get("paymentLinkId"),
            }
        )

    # --- обработка --------------------------------------------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != HOST:
            return httpx.Response(404, text="unknown host")
        if request.headers.get("authorization") != f"Bearer {TOKEN}":
            return httpx.Response(401, json={"message": "Unauthorized"})
        path = request.url.path.removeprefix("/uapi/")
        self.calls.append((request.method, path))
        for prefix, code in self.fail.items():
            if path.startswith(prefix):
                return httpx.Response(code, json={"message": "fail"})
        body = json.loads(request.content) if request.content else {}
        parts = path.split("/")
        match (request.method, parts):
            case ("POST", ["invoice", "v1.0", "bills"]):
                ref = f"bill-{uuid4().hex[:12]}"
                self.bills[ref] = body
                self.bill_status[ref] = "payment_waiting"
                return _data({"documentId": ref})
            case (
                "GET",
                ["invoice", "v1.0", "bills", V.CUSTOMER, ref, "payment-status"],
            ):
                if ref not in self.bills:
                    return httpx.Response(404, json={"message": "not found"})
                return _data({"paymentStatus": self.bill_status[ref]})
            case (
                "GET",
                ["invoice", "v1.0", "bills" | "closing-documents", _, ref, "file"],
            ):
                if ref not in self.bills and ref not in self.acts:
                    return httpx.Response(404, json={"message": "not found"})
                return httpx.Response(
                    200,
                    content=self.pdf_bytes,
                    headers={"content-type": "application/pdf"},
                )
            case ("DELETE", ["invoice", "v1.0", "bills", V.CUSTOMER, ref]):
                self.deleted.append(ref)
                return _data({"result": True})
            case ("POST", ["invoice", "v1.0", "closing-documents"]):
                ref = f"act-{uuid4().hex[:12]}"
                self.acts[ref] = body
                return _data({"documentId": ref})
            case (
                "POST",
                ["invoice", "v1.0", "closing-documents", V.CUSTOMER, ref, "email"],
            ):
                self.emails.append((ref, body["Data"]["email"]))
                return _data({"result": True})
            case ("POST", ["acquiring", "v1.0", kind]) if kind in (
                "payments_with_receipt",
                "subscriptions_with_receipt",
            ):
                ref = str(uuid4())
                link_id = body["Data"].get("paymentLinkId")
                if any(
                    item["Data"].get("paymentLinkId") == link_id
                    for item in self.links.values()
                ):
                    return httpx.Response(400, json={"message": "duplicate link id"})
                self.links[ref] = {
                    **body,
                    "kind": kind,
                    "status": "CREATED",
                    "orders": [],
                }
                return _data(
                    {
                        **body["Data"],
                        "operationId": ref,
                        "paymentLink": f"https://merch.tochka.com/order/?uuid={ref}",
                        "status": "CREATED",
                    }
                )
            case ("POST", ["acquiring", "v1.0", "subscriptions", ref, "status"]):
                self.links[ref]["status"] = body["Data"]["status"]
                return _data({"result": True})
            case ("GET", ["acquiring", "v1.0", "payments", ref]):
                link = self.links.get(ref)
                if link is None:
                    return httpx.Response(404, json={"message": "not found"})
                return _data(
                    {
                        "Operation": [
                            {
                                "customerCode": CUSTOMER,
                                "operationId": ref,
                                "amount": link["Data"]["amount"],
                                "status": link["status"],
                                "createdAt": "2026-10-09T10:00:00+03:00",
                                "paymentLink": "https://merch.tochka.com/order/",
                                "Order": [
                                    {
                                        "orderId": order_id,
                                        "type": "approval",
                                        "amount": link["Data"]["amount"],
                                        "time": "2026-10-09T10:00:00+03:00",
                                        "name": "approval",
                                    }
                                    for order_id in link["orders"]
                                ],
                            }
                        ]
                    }
                )
            case ("PUT", ["webhook", "v1.0", V.CLIENT_ID]):
                self.webhooks.append(body)
                return httpx.Response(200, json={"Data": {"result": True}})
        return httpx.Response(404, json={"message": f"no route {path}"})


def _data(data: dict[str, Any]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "Data": data,
            "Links": {"self": "https://enter.tochka.com/uapi/"},
            "Meta": {"totalPages": 1},
        },
    )
