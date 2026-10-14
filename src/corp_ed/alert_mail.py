"""Тревоги мониторинга — письмом (deploy/monitoring, сервис alert-mail).

Alertmanager шлёт сюда webhook (формат version 4), отсюда — письмо на
TEAM_NOTIFY_EMAIL через Postbox по HTTPS. Отдельный процесс в проекте
мониторинга, без базы и без API: тревоги нужнее всего, когда база, API
или воркер лежат, и очередь outbox_emails тогда не поможет. Повторы —
у Alertmanager: при ошибке отправки отвечаем 502, он пришлёт снова.

Слушает только сеть Docker мониторинга: порт не публикуется, nginx сюда
не ведёт. Тело — не больше MAX_BODY, писем — не больше MAX_PER_HOUR:
дребезг тревоги не превратится в сотни писем.

Проверка с сервера (письмо уйдёт сразу, STAGE.md §4.5):
  docker exec kronto-monitoring-alert-mail-1 python -m corp_ed.alert_mail test
"""

import asyncio
import json
import os
import sys
import time
from collections import deque
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from datetime import datetime
from html import escape
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from corp_ed.core.config import MailSettings, TeamNotifySettings
from corp_ed.core.mail import (
    MailDeliveryError,
    MailSender,
    OutgoingEmail,
    build_sender,
)
from corp_ed.core.outbound import outbound_http_client

logger = structlog.get_logger()

MAX_BODY = 256 * 1024
MAX_PER_HOUR = 30
MSK = ZoneInfo("Europe/Moscow")
TEST_TEXT = "Это проверка: тревоги мониторинга будут приходить сюда."


def _drop_empty_env() -> None:
    """compose передаёт незаданную переменную пустой строкой (${X:-}), а
    пустая строка перекрыла бы значение по умолчанию (MAIL_FROM_ADDRESS)."""
    for key in [key for key, value in os.environ.items() if value == ""]:
        del os.environ[key]


def _via_proxy() -> bool:
    """Как у воркера (CONNECTOR_OUTBOUND_VIA_PROXY): запросы наружу — через
    прокси из окружения только в этом режиме (core/outbound.py)."""
    value = os.environ.get("CONNECTOR_OUTBOUND_VIA_PROXY", "")
    return value.strip().lower() in ("1", "true", "yes", "on")


class Relay:
    def __init__(
        self, sender: MailSender, to: str, *, now: Any = time.monotonic
    ) -> None:
        self.sender = sender
        self.to = to
        self._now = now
        self._sent: deque[float] = deque()

    def allowed(self) -> bool:
        now = self._now()
        while self._sent and now - self._sent[0] > 3600:
            self._sent.popleft()
        return len(self._sent) < MAX_PER_HOUR

    async def send(self, email: OutgoingEmail) -> None:
        await self.sender.send(email)
        self._sent.append(self._now())


def render(payload: Mapping[str, Any], to: str) -> OutgoingEmail | None:
    """Письмо по webhook Alertmanager; None — тревог в нём нет."""
    alerts = [a for a in payload.get("alerts") or [] if isinstance(a, dict)]
    if not alerts:
        return None
    firing = payload.get("status") == "firing"
    common = payload.get("commonLabels") or {}
    stand = str(common.get("stand") or "kronto")

    lines: list[tuple[str, str, str]] = []
    summaries: list[str] = []
    for alert in alerts:
        labels = alert.get("labels") or {}
        notes = alert.get("annotations") or {}
        summary = str(notes.get("summary") or labels.get("alertname") or "тревога")
        mark = "‼️ " if labels.get("severity") == "critical" else ""
        state = "" if alert.get("status") == "firing" else " — прошло"
        summaries.append(f"{mark}{summary}" if firing else summary)
        lines.append(
            (
                f"{mark}{summary}{state}",
                str(notes.get("description") or ""),
                _since(alert.get("startsAt")),
            )
        )

    head = "🔴" if firing else "✅"
    first = summaries[0]
    more = f" (+{len(lines) - 1})" if len(lines) > 1 else ""
    done = "" if firing else " — прошло"
    subject = f"{head} {stand}{done}: {first}{more}"

    text_parts = []
    html_parts = []
    for title, description, since in lines:
        text_parts.append("\n".join(x for x in (title, description, since) if x))
        html_parts.append(
            f"<p><b>{escape(title)}</b>"
            + (f"<br>{escape(description)}" if description else "")
            + (f'<br><span style="color:#5e5e66">{escape(since)}</span>' * bool(since))
            + "</p>"
        )
    footer = "Подробности — Grafana и Alertmanager на стенде (STAGE.md §7)."
    text = "\n\n".join(text_parts) + f"\n\n{footer}"
    html = (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f"<title>{escape(subject)}</title></head>"
        '<body style="font-family:Arial,sans-serif;font-size:15px;line-height:1.5">'
        + "".join(html_parts)
        + f'<p style="color:#5e5e66;font-size:13px">{escape(footer)}</p>'
        "</body></html>"
    )
    return OutgoingEmail(to=to, subject=subject, text=text, html=html)


def _since(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return ""
    if moment.year < 2000:  # нулевое время Go — «не задано»
        return ""
    return f"с {moment.astimezone(MSK):%d.%m %H:%M} МСК"


def create_app(relay_factory: Any = None) -> FastAPI:
    """relay_factory — для тестов: () -> Relay | None."""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if relay_factory is not None:
            app.state.relay = relay_factory()
            yield
            return
        _drop_empty_env()
        to = TeamNotifySettings().email
        mail = MailSettings()
        async with outbound_http_client(via_proxy=_via_proxy()) as client:
            app.state.relay = Relay(build_sender(mail, client), to) if to else None
            if not to:
                logger.warning("alert_mail_disabled", reason="TEAM_NOTIFY_EMAIL")
            yield

    app = FastAPI(
        title="kronto alert-mail", lifespan=lifespan, docs_url=None, redoc_url=None
    )
    app.openapi_url = None

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/alerts")
    async def alerts(request: Request) -> JSONResponse:
        relay: Relay | None = request.app.state.relay
        body = b""
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_BODY:
                return JSONResponse({"error": "too_large"}, status_code=413)
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse({"error": "bad_json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "bad_json"}, status_code=400)
        if relay is None:
            return JSONResponse({"status": "disabled"})
        email = render(payload, relay.to)
        if email is None:
            return JSONResponse({"status": "empty"})
        if not relay.allowed():
            # Повтор не поможет — 200, чтобы Alertmanager не долбил.
            logger.warning("alert_mail_rate_limited", limit=MAX_PER_HOUR)
            return JSONResponse({"status": "rate_limited"})
        try:
            await relay.send(email)
        except MailDeliveryError as exc:
            logger.warning("alert_mail_failed", error=exc.code)
            return JSONResponse({"error": exc.code}, status_code=502)
        logger.info("alert_mail_sent", alerts=len(payload.get("alerts") or []))
        return JSONResponse({"status": "sent"})

    return app


app = create_app()


async def _send_test() -> int:
    _drop_empty_env()
    to = TeamNotifySettings().email
    if not to:
        print("TEAM_NOTIFY_EMAIL не задан в .env — писать некуда", file=sys.stderr)
        return 1
    mail = MailSettings()
    async with outbound_http_client(via_proxy=_via_proxy()) as client:
        try:
            await build_sender(mail, client).send(
                OutgoingEmail(
                    to=to,
                    subject="kronto: проверка писем мониторинга",
                    text=TEST_TEXT,
                    html=f"<p>{TEST_TEXT}</p>",
                )
            )
        except MailDeliveryError as exc:
            print(f"не отправлено: {exc.code}", file=sys.stderr)
            return 1
    print("отправлено")
    return 0


if __name__ == "__main__":
    if sys.argv[1:] != ["test"]:
        print("usage: python -m corp_ed.alert_mail test", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(_send_test()))
