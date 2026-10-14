"""Тревоги мониторинга письмом (corp_ed/alert_mail.py, сервис alert-mail):
разбор webhook Alertmanager, повтор при сбое почты, лимиты."""

from typing import Any

import httpx
import pytest

from corp_ed import alert_mail
from corp_ed.core.mail import MailDeliveryError, MemorySender, OutgoingEmail

TO = "ops@krontoai.ru"

# Webhook Alertmanager, version 4 (prometheus.io/docs/alerting/latest/
# configuration/#webhook_config): как его шлёт v0.34 с нашими правилами.
FIRING: dict[str, Any] = {
    "version": "4",
    "groupKey": '{}/{severity="critical"}:{alertname="NotReady"}',
    "truncatedAlerts": 0,
    "status": "firing",
    "receiver": "team",
    "groupLabels": {"alertname": "NotReady"},
    "commonLabels": {"alertname": "NotReady", "severity": "critical", "stand": "stage"},
    "commonAnnotations": {},
    "externalURL": "http://alertmanager:9093",
    "alerts": [
        {
            "status": "firing",
            "labels": {
                "alertname": "NotReady",
                "severity": "critical",
                "stand": "stage",
                "job": "api",
            },
            "annotations": {
                "summary": "Стенд не готов отвечать сотрудникам (api)",
                "description": "/health/ready не 200 дольше 2 минут.",
            },
            "startsAt": "2026-10-14T19:30:05.123Z",
            "endsAt": "0001-01-01T00:00:00Z",
            "generatorURL": "http://prometheus:9090/graph?g0.expr=...",
            "fingerprint": "a1b2c3d4e5f60718",
        }
    ],
}


def resolved() -> dict[str, Any]:
    payload = {**FIRING, "status": "resolved"}
    payload["alerts"] = [
        {**FIRING["alerts"][0], "status": "resolved", "endsAt": "2026-10-14T19:41Z"}
    ]
    return payload


class FailingSender:
    def __init__(self, error: MailDeliveryError) -> None:
        self.error = error

    async def send(self, email: OutgoingEmail) -> None:
        raise self.error


async def post(relay: alert_mail.Relay | None, body: Any) -> httpx.Response:
    app = alert_mail.create_app(lambda: relay)
    transport = httpx.ASGITransport(app=app)
    async with (
        app.router.lifespan_context(app),
        httpx.AsyncClient(transport=transport, base_url="http://alert-mail") as client,
    ):
        if isinstance(body, bytes):
            return await client.post("/alerts", content=body)
        return await client.post("/alerts", json=body)


async def test_firing_alert_becomes_an_email() -> None:
    sender = MemorySender()
    response = await post(alert_mail.Relay(sender, TO), FIRING)

    assert response.status_code == 200
    assert response.json() == {"status": "sent"}
    [email] = sender.sent
    assert email.to == TO
    assert email.subject == "🔴 stage: ‼️ Стенд не готов отвечать сотрудникам (api)"
    assert email.text.startswith(
        "‼️ Стенд не готов отвечать сотрудникам (api)\n"
        "/health/ready не 200 дольше 2 минут.\n"
        "с 14.10 22:30 МСК"
    )


async def test_resolved_alert_says_so() -> None:
    sender = MemorySender()
    await post(alert_mail.Relay(sender, TO), resolved())

    [email] = sender.sent
    assert email.subject == (
        "✅ stage — прошло: Стенд не готов отвечать сотрудникам (api)"
    )
    assert "— прошло" in email.text


def test_several_alerts_in_one_letter_and_html_is_escaped() -> None:
    payload = {**FIRING}
    payload["alerts"] = [
        FIRING["alerts"][0],
        {
            "status": "firing",
            "labels": {"alertname": "DiskFull", "severity": "warning"},
            "annotations": {"summary": "Диск <b>почти</b> полон"},
            "startsAt": "bad",
        },
    ]
    email = alert_mail.render(payload, TO)
    assert email is not None
    assert email.subject.endswith("(+1)")
    assert "Диск &lt;b&gt;почти&lt;/b&gt; полон" in email.html
    assert "<b>почти</b>" not in email.html


async def test_mail_failure_asks_alertmanager_to_retry() -> None:
    relay = alert_mail.Relay(
        FailingSender(MailDeliveryError("postbox_503", retryable=True)), TO
    )
    response = await post(relay, FIRING)
    assert response.status_code == 502
    assert response.json() == {"error": "postbox_503"}


async def test_rate_limit_drops_without_retry() -> None:
    clock = [0.0]
    sender = MemorySender()
    relay = alert_mail.Relay(sender, TO, now=lambda: clock[0])
    for _ in range(alert_mail.MAX_PER_HOUR):
        assert (await post(relay, FIRING)).json() == {"status": "sent"}
    assert (await post(relay, FIRING)).json() == {"status": "rate_limited"}
    clock[0] = 3601.0
    assert (await post(relay, FIRING)).json() == {"status": "sent"}
    assert len(sender.sent) == alert_mail.MAX_PER_HOUR + 1


@pytest.mark.parametrize(
    ("body", "status"),
    [
        (b"not json", 400),
        (b"[1, 2]", 400),
        (b"{" + b" " * (alert_mail.MAX_BODY + 1) + b"}", 413),
    ],
)
async def test_bad_bodies_are_refused(body: bytes, status: int) -> None:
    sender = MemorySender()
    assert (await post(alert_mail.Relay(sender, TO), body)).status_code == status
    assert sender.sent == []


async def test_without_address_nothing_is_sent() -> None:
    assert (await post(None, FIRING)).json() == {"status": "disabled"}
    sender = MemorySender()
    empty = {**FIRING, "alerts": []}
    assert (await post(alert_mail.Relay(sender, TO), empty)).json() == {
        "status": "empty"
    }
