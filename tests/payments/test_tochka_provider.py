"""Клиент API Точки против поддельного сервера: формы запросов по
swagger, разбор ответов, ошибки; подпись вебхука — настоящим ключом
Точки на примере из документации и ключом поддельного сервера."""

import base64
import hmac
import json
import ssl
from datetime import date, datetime
from hashlib import sha256
from pathlib import Path

import httpx
import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from corp_ed.core.config import TOCHKA_WEBHOOK_KEY, TochkaSettings
from corp_ed.services.payments.provider import (
    ActRequest,
    BillRequest,
    BillStatus,
    InvalidWebhookError,
    Line,
    LinkRequest,
    Party,
    PaymentProviderError,
)
from corp_ed.services.payments.tochka import TochkaProvider, tochka_ssl_context
from tests.payments.fake_tochka import (
    ACCOUNT,
    CLIENT_ID,
    CUSTOMER,
    FakeTochka,
    sign,
)

EXAMPLE = Path(__file__).parent / "fixtures" / "tochka_incoming_example.jwt"
PARTY = Party(
    name="ООО «Ромашка»",
    inn="7707083893",
    kpp="773601001",
    address="Москва, ул. Тверская, 1",
    type="company",
)
LINES = [
    Line(name="kronto, тариф «Базовый», 30 мест, 1 месяц", amount_kopecks=29_700_00)
]


def _bill() -> BillRequest:
    return BillRequest(
        number="KR-00042",
        issued=date(2026, 10, 9),
        party=PARTY,
        lines=LINES,
        total_kopecks=29_700_00,
    )


def _link() -> LinkRequest:
    return LinkRequest(
        order_id="KR-00043",
        purpose="Оплата по счёту № KR-00043 от 09.10.2026 за доступ к сервису "
        "kronto. Без НДС " + "x" * 200,
        email="buh@romashka.ru",
        payer_name="ООО «Ромашка»",
        lines=[Line(name="Пакет 500 кредитов", amount_kopecks=1_490_00)],
        total_kopecks=1_490_00,
        return_url="https://krontoai.ru/admin/tariff",
    )


@pytest.fixture
def bank() -> FakeTochka:
    return FakeTochka()


# --- счета ----------------------------------------------------------------------------


async def test_bill_is_created_with_buyer_and_number(bank: FakeTochka) -> None:
    ref = await bank.provider().create_bill(_bill())

    data = bank.bills[ref]["Data"]
    assert data["accountId"] == ACCOUNT
    assert data["customerCode"] == CUSTOMER
    assert data["SecondSide"] == {
        "taxCode": "7707083893",
        "type": "company",
        "secondSideName": "ООО «Ромашка»",
        "legalAddress": "Москва, ул. Тверская, 1",
        "kpp": "773601001",
    }
    invoice = data["Content"]["Invoice"]
    assert invoice["number"] == "KR-00042"
    assert invoice["date"] == "2026-10-09"
    assert invoice["totalAmount"] == 29700.0
    [position] = invoice["Positions"]
    assert position["ndsKind"] == "without_nds"
    assert position["price"] == position["totalAmount"] == 29700.0
    assert position["quantity"] == 1


async def test_bill_status_and_pdf(bank: FakeTochka) -> None:
    provider = bank.provider()
    ref = await provider.create_bill(_bill())

    assert await provider.bill_status(ref) == BillStatus.WAITING
    bank.pay_bill(ref)
    assert await provider.bill_status(ref) == BillStatus.PAID
    assert (await provider.bill_pdf(ref) or b"").startswith(b"%PDF")
    # Вместо PDF пришло что-то другое — своего шаблона лучше, чем мусор.
    bank.pdf_bytes = b"<html>error</html>"
    assert await provider.bill_pdf(ref) is None


async def test_sole_trader_bill_has_no_kpp(bank: FakeTochka) -> None:
    ip = Party(
        name="ИП Петров П. П.",
        inn="500100732259",
        kpp=None,
        address="Тверь",
        type="ip",
    )
    request = BillRequest(
        number="KR-1", issued=date(2026, 10, 9), party=ip, lines=LINES, total_kopecks=1
    )
    ref = await bank.provider().create_bill(request)
    side = bank.bills[ref]["Data"]["SecondSide"]
    assert side["type"] == "ip"
    assert "kpp" not in side


async def test_errors_tell_whether_to_retry(bank: FakeTochka) -> None:
    provider = bank.provider()
    bank.fail["invoice/v1.0/bills"] = 503
    with pytest.raises(PaymentProviderError) as error:
        await provider.create_bill(_bill())
    assert error.value.retryable
    bank.fail["invoice/v1.0/bills"] = 403
    with pytest.raises(PaymentProviderError) as error:
        await provider.create_bill(_bill())
    assert not error.value.retryable
    assert error.value.code == "tochka_http_403"


async def test_wrong_key_is_an_error_without_the_key_in_it(bank: FakeTochka) -> None:
    provider = bank.provider(jwt="not-the-key")
    with pytest.raises(PaymentProviderError) as error:
        await provider.create_bill(_bill())
    assert error.value.code == "tochka_http_401"
    assert "not-the-key" not in str(error.value)


async def test_network_failure_is_retryable() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    provider = TochkaProvider(
        httpx.AsyncClient(transport=httpx.MockTransport(broken)),
        FakeTochka().settings(),
    )
    with pytest.raises(PaymentProviderError) as error:
        await provider.bill_status("x")
    assert error.value.retryable


def test_provider_needs_key_customer_and_account() -> None:
    client = httpx.AsyncClient()
    with pytest.raises(RuntimeError) as error:
        TochkaProvider(
            client, TochkaSettings(jwt=None, customer_code="", account_id="")
        )
    assert "TOCHKA_JWT" in str(error.value)
    assert "TOCHKA_ACCOUNT_ID" in str(error.value)


# --- ссылки и подписки ----------------------------------------------------------------


async def test_payment_link_carries_a_receipt(bank: FakeTochka) -> None:
    link = await bank.provider().create_link(_link())

    assert link.url.startswith("https://")
    data = bank.links[link.ref]["Data"]
    assert bank.links[link.ref]["kind"] == "payments_with_receipt"
    assert data["paymentMode"] == ["card", "sbp"]
    assert data["taxSystemCode"] == "usn_income"
    assert data["Client"] == {"email": "buh@romashka.ru", "name": "ООО «Ромашка»"}
    assert data["paymentLinkId"] == "KR-00043"
    assert len(data["purpose"]) <= 140
    assert data["amount"] == 1490.0
    [item] = data["Items"]
    assert item == {
        "name": "Пакет 500 кредитов",
        "amount": 1490.0,
        "quantity": 1,
        "vatType": "none",
        "paymentMethod": "full_prepayment",
        "paymentObject": "service",
    }
    assert data["merchantId"] == "200000000001234"


async def test_card_subscription_charges_monthly(bank: FakeTochka) -> None:
    provider = bank.provider()
    link = await provider.create_card_subscription(_link())

    stored = bank.links[link.ref]
    assert stored["kind"] == "subscriptions_with_receipt"
    assert stored["Data"]["Options"] == {"period": "Month", "trancheCount": 84}
    assert "paymentMode" not in stored["Data"]

    info = await provider.payment_info(link.ref)
    assert not info.approved and info.charges == []
    first = bank.approve(link.ref)
    second = bank.approve(link.ref)
    info = await provider.payment_info(link.ref)
    assert info.approved
    assert info.amount_kopecks == 1_490_00
    assert info.charges == [first, second]

    await provider.cancel_card_subscription(link.ref)
    assert bank.links[link.ref]["status"] == "Cancelled"


# --- акты и вебхук --------------------------------------------------------------------


async def test_act_is_linked_to_the_bill_and_mailed(bank: FakeTochka) -> None:
    provider = bank.provider()
    act = ActRequest(
        number="А-3",
        issued=date(2026, 11, 1),
        party=PARTY,
        lines=LINES,
        total_kopecks=29_700_00,
        bill_ref="bill-1",
    )
    ref = await provider.create_act(act)

    assert ref is not None
    body = bank.acts[ref]["Data"]
    assert body["documentId"] == "bill-1"
    assert body["Content"]["Act"]["number"] == "А-3"
    assert body["Content"]["Act"]["Positions"][0]["ndsKind"] == "without_nds"
    assert (await provider.act_pdf(ref) or b"").startswith(b"%PDF")
    await provider.send_act(ref, "buh@romashka.ru")
    assert bank.emails == [(ref, "buh@romashka.ru")]


async def test_webhook_is_registered_for_both_events(bank: FakeTochka) -> None:
    await bank.provider().register_webhook("https://krontoai.ru/api/v1/payments/tochka")
    assert bank.webhooks == [
        {
            "webhooksList": ["incomingPayment", "acquiringInternetPayment"],
            "url": "https://krontoai.ru/api/v1/payments/tochka",
        }
    ]
    assert ("PUT", f"webhook/v1.0/{CLIENT_ID}") in bank.calls


# --- подпись вебхука ------------------------------------------------------------------


def test_example_from_tochka_docs_verifies_with_the_real_key() -> None:
    """Пример тела вебхука со страницы incomingPayment подписан настоящим
    ключом Точки — тем, что стоит по умолчанию в TOCHKA_WEBHOOK_PUBLIC_KEY."""
    provider = TochkaProvider(
        httpx.AsyncClient(),
        FakeTochka().settings(webhook_public_key=TOCHKA_WEBHOOK_KEY),
    )
    event = provider.parse_webhook(EXAMPLE.read_bytes())

    assert event.kind == "incoming"
    assert event.payment_id == "0000000000"
    assert event.amount_kopecks == 40_00
    assert event.payer_inn == "0000000000"
    assert event.purpose == "Тестовое назначение платежа"
    assert event.customer_code == "300123123"


def test_signed_webhooks_are_parsed(bank: FakeTochka) -> None:
    provider = bank.provider()
    event = provider.parse_webhook(
        bank.incoming_webhook(
            amount="29700.00", purpose="По счёту KR-00042", inn="7707083893"
        )
    )
    assert (event.kind, event.amount_kopecks, event.payer_inn) == (
        "incoming",
        29_700_00,
        "7707083893",
    )


def _tamper(token: bytes) -> bytes:
    header, payload, signature = token.split(b".")
    data = json.loads(base64.urlsafe_b64decode(payload + b"=" * (-len(payload) % 4)))
    data["SidePayer"]["amount"] = "1.0"
    forged = base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=")
    return b".".join([header, forged, signature])


def test_webhooks_with_a_bad_signature_are_rejected(bank: FakeTochka) -> None:
    provider = bank.provider()
    good = bank.incoming_webhook(amount="10.0", purpose="KR-1", inn="7707083893")
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    payload = {"webhookType": "incomingPayment", "paymentId": "1"}
    public_pem = _public_pem(bank)
    unsigned = (
        base64.urlsafe_b64encode(b'{"alg":"none","typ":"JWT"}').rstrip(b"=")
        + b"."
        + base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=")
        + b"."
    )
    # HS256 с публичным ключом как секретом — подмена алгоритма.
    confused = _hs256(payload, public_pem)
    for body in (
        _tamper(good),
        sign(payload, other_key),
        unsigned,
        confused,
        b"not a jwt",
        b"",
        "кириллица".encode(),
        good + b"x" * 70_000,
    ):
        with pytest.raises(InvalidWebhookError):
            provider.parse_webhook(body)


def _hs256(payload: dict[str, object], secret: bytes) -> bytes:
    def b64(raw: bytes) -> bytes:
        return base64.urlsafe_b64encode(raw).rstrip(b"=")

    signing = (
        b64(b'{"alg":"HS256","typ":"JWT"}') + b"." + b64(json.dumps(payload).encode())
    )
    signature = hmac.new(secret, signing, sha256).digest()
    return signing + b"." + b64(signature)


def _public_pem(bank: FakeTochka) -> bytes:
    key = jwt.algorithms.RSAAlgorithm.from_jwk(bank.settings().webhook_public_key)
    assert isinstance(key, rsa.RSAPublicKey)
    return key.public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    )


def test_pem_key_is_accepted(bank: FakeTochka) -> None:
    pem = _public_pem(bank).decode()
    provider = bank.provider(webhook_public_key=pem)
    body = bank.incoming_webhook(amount="1.0", purpose="KR-1", inn="7707083893")
    assert provider.parse_webhook(body).kind == "incoming"
    with pytest.raises(RuntimeError):
        bank.provider(webhook_public_key="-----BEGIN PUBLIC KEY-----\nxx\n")


# --- сертификат Минцифры --------------------------------------------------------------


def _self_signed(path: Path) -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Test Trusted Root CA")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(1)
        .not_valid_before(datetime(2026, 1, 1))
        .not_valid_after(datetime(2036, 1, 1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def test_tochka_trusts_the_extra_root_only_in_its_own_context(tmp_path: Path) -> None:
    ca = tmp_path / "root.pem"
    _self_signed(ca)

    context = tochka_ssl_context(str(ca))

    subjects = [dict(item[0] for item in c["subject"]) for c in context.get_ca_certs()]
    assert {"commonName": "Test Trusted Root CA"} in subjects
    # Обычные корни тоже на месте: это дополнение, а не замена.
    assert len(subjects) > 1
    assert context.verify_mode == ssl.CERT_REQUIRED
    default = ssl.create_default_context()
    assert {"commonName": "Test Trusted Root CA"} not in [
        dict(item[0] for item in c["subject"]) for c in default.get_ca_certs()
    ]


def test_missing_root_certificate_stops_the_start(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError) as error:
        tochka_ssl_context(str(tmp_path / "absent.pem"))
    assert "TOCHKA_CA_FILE" in str(error.value)
