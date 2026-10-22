"""Цепочка поставщиков шага решения в eval (BH-46, «Поставщики решения»,
п. 7 handoff) — на подделках, без сети и без реальных сбоев поставщиков."""

from typing import Any

import httpx
import pytest

from corp_ed.domain.decision import Route
from corp_ed.llm.types import Message, Role
from eval.decision_loop import run_decided
from eval.providers import (
    CallTimeoutError,
    ChainExhaustedError,
    ChainLink,
    CircuitBreaker,
    DecisionChain,
    OpenAICompatibleClient,
    ProviderError,
    classify_failure,
)
from eval.yandex import Completion

MESSAGES = [Message(role=Role.USER, content="вопрос")]


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def ok(text: str = "{}", ms: float = 1000.0) -> Completion:
    return Completion(
        text=text, input_tokens=10, output_tokens=5, latency_ms=ms, model="m"
    )


class Fake:
    """Звено: по очереди отдаёт заготовки; исключение — бросает. spend —
    сколько секунд «идёт» вызов по часам clock."""

    def __init__(
        self, *outcomes: Any, clock: Clock | None = None, spend: float = 0.0
    ) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict[str, Any]] = []
        self.clock, self.spend = clock, spend

    def complete(self, messages: Any, **kwargs: Any) -> Completion:
        self.calls.append(kwargs)
        if self.clock is not None:
            self.clock.now += self.spend
        outcome = self.outcomes.pop(0) if len(self.outcomes) > 1 else self.outcomes[0]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def chain(
    first: Fake,
    second: Fake,
    *,
    clock: Clock | None = None,
    breaker: tuple[int, float, float] = (5, 60.0, 120.0),
    **kw: Any,
) -> DecisionChain:
    clock = clock or Clock()
    return DecisionChain(
        [
            ChainLink(
                "cloudru",
                first,
                "openai/gpt-oss-120b",
                CircuitBreaker(*breaker, clock=clock),
            ),
            ChainLink(
                "mws", second, "gpt-oss-120b", CircuitBreaker(*breaker, clock=clock)
            ),
        ],
        clock=clock,
        **kw,
    )


def server(provider: str, status: int, body: str = "") -> ProviderError:
    return ProviderError(provider, status, body)


@pytest.mark.parametrize(
    "error, kind, status",
    [
        (server("cloudru", 500), "server", 500),
        (server("cloudru", 503), "server", 503),
        (server("cloudru", 429), "overload", 429),
        (httpx.ConnectError("обрыв"), "transport", None),
        (httpx.RemoteProtocolError("обрыв посреди ответа"), "transport", None),
    ],
)
def test_fast_error_at_first_goes_to_second(
    error: BaseException, kind: str, status: int | None
) -> None:
    first, second = Fake(error), Fake(ok("решение MWS"))
    c = chain(first, second)
    completion = c.complete(MESSAGES, model="не используется", temperature=0)
    assert completion.text == "решение MWS"
    # У каждого звена своя модель (у MWS — идентификатор деплоймента).
    assert first.calls[0]["model"] == "openai/gpt-oss-120b"
    assert second.calls[0]["model"] == "gpt-oss-120b"
    assert second.calls[0]["temperature"] == 0
    events = c.drain()
    assert [(e.provider, e.outcome, e.status, e.breaker, e.alert) for e in events] == [
        ("cloudru", kind, status, "closed", False),
        ("mws", "ok", None, "closed", False),
    ]
    assert c.drain() == []


def test_real_client_with_one_attempt_does_not_retry_before_switch() -> None:
    hits = {"cloudru": 0, "mws": 0}

    def make(provider: str, status: int) -> OpenAICompatibleClient:
        def handler(request: httpx.Request) -> httpx.Response:
            hits[provider] += 1
            if status != 200:
                return httpx.Response(status, text="Service Unavailable")
            return httpx.Response(
                200,
                json={
                    "choices": [
                        {"message": {"content": "{}"}, "finish_reason": "stop"}
                    ],
                    "usage": {},
                },
            )

        return OpenAICompatibleClient(
            provider,
            "https://api.example/v1",
            "key",
            http=httpx.Client(transport=httpx.MockTransport(handler)),
            sleep=lambda _: None,
            max_attempts=1,
        )

    c = DecisionChain(
        [
            ChainLink("cloudru", make("cloudru", 503), "openai/gpt-oss-120b"),
            ChainLink("mws", make("mws", 200), "gpt-oss-120b"),
        ]
    )
    assert c.complete(MESSAGES, api="openai").text == "{}"
    assert hits == {"cloudru": 1, "mws": 1}
    assert [e.outcome for e in c.events] == ["server", "ok"]


def test_timeout_of_first_leaves_the_rest_of_budget_to_second() -> None:
    clock = Clock()
    first = Fake(CallTimeoutError("cloudru", 4000.0), clock=clock, spend=4.0)
    second = Fake(ok("решение MWS"), clock=clock, spend=1.0)
    c = chain(first, second, clock=clock, link_timeout=4.0)
    assert c.complete(MESSAGES, timeout=7.0).text == "решение MWS"
    assert first.calls[0]["timeout"] == 4.0
    assert second.calls[0]["timeout"] == pytest.approx(3.0)
    assert [(e.outcome, e.ms) for e in c.events] == [
        ("timeout", 4000.0),
        ("ok", 1000.0),
    ]


def test_budget_spent_by_first_is_a_timeout_for_the_chain() -> None:
    clock = Clock()
    first = Fake(CallTimeoutError("cloudru", 7000.0), clock=clock, spend=7.0)
    second = Fake(ok())
    c = chain(first, second, clock=clock)
    with pytest.raises(CallTimeoutError):
        c.complete(MESSAGES, timeout=7.0)
    assert second.calls == []


def test_breaker_opens_after_n_failures_skips_first_then_one_probe() -> None:
    clock = Clock()
    first = Fake(server("cloudru", 503))
    second = Fake(ok())
    c = chain(first, second, clock=clock, breaker=(3, 60.0, 120.0))
    for _ in range(3):
        c.complete(MESSAGES)
        clock.now += 1.0
    assert len(first.calls) == 3
    assert c.links[0].breaker.state() == "open"
    c.drain()
    # Открыт — первое звено не вызывается M секунд, вопросы сразу ко второму.
    clock.now += 100.0
    c.complete(MESSAGES)
    assert len(first.calls) == 3
    assert [(e.provider, e.outcome, e.breaker) for e in c.drain()] == [
        ("cloudru", "skipped", "open"),
        ("mws", "ok", "closed"),
    ]
    # Прошло M — одна пробная попытка (полуоткрыто); удачная — закрыт.
    clock.now += 30.0
    first.outcomes = [ok("снова живой")]
    assert c.links[0].breaker.state() == "half_open"
    assert c.complete(MESSAGES).text == "снова живой"
    assert len(first.calls) == 4
    assert c.links[0].breaker.state() == "closed"
    assert [(e.outcome, e.breaker) for e in c.drain()] == [("ok", "half_open")]


def test_failed_probe_opens_breaker_for_another_cooldown() -> None:
    clock = Clock()
    first, second = Fake(server("cloudru", 503)), Fake(ok())
    c = chain(first, second, clock=clock, breaker=(2, 60.0, 120.0))
    c.complete(MESSAGES)
    c.complete(MESSAGES)
    clock.now += 121.0
    c.complete(MESSAGES)  # проба — снова 503
    assert len(first.calls) == 3
    assert c.links[0].breaker.state() == "open"
    clock.now += 119.0
    c.complete(MESSAGES)
    assert len(first.calls) == 3  # ещё открыт


def test_half_open_lets_only_one_probe_through() -> None:
    clock = Clock()
    breaker = CircuitBreaker(1, 60.0, 120.0, clock=clock)
    breaker.failure()
    clock.now += 121.0
    assert breaker.allow() is True
    assert breaker.allow() is False  # проба ещё идёт
    breaker.success()
    assert breaker.allow() is True and breaker.state() == "closed"


def test_failures_outside_window_do_not_open_breaker() -> None:
    clock = Clock()
    breaker = CircuitBreaker(3, 60.0, 120.0, clock=clock)
    for _ in range(5):
        breaker.failure()
        clock.now += 40.0  # в окне 60 с — не больше двух
    assert breaker.state() == "closed"


def test_success_resets_failure_count() -> None:
    breaker = CircuitBreaker(2, 60.0, 120.0, clock=Clock())
    breaker.failure()
    breaker.success()
    breaker.failure()
    assert breaker.state() == "closed"


@pytest.mark.parametrize(
    "provider, status",
    [
        ("cloudru", 400),
        ("cloudru", 401),
        ("cloudru", 402),
        ("cloudru", 404),
        ("mws", 403),
        ("mws", 404),
    ],
)
def test_config_errors_switch_and_alert(provider: str, status: int) -> None:
    assert classify_failure(provider, server(provider, status)) == ("config", status)
    first, second = Fake(server("cloudru", status)), Fake(ok("решение MWS"))
    c = chain(first, second)
    assert c.complete(MESSAGES).text == "решение MWS"
    event = c.events[0]
    assert (event.provider, event.outcome, event.status, event.alert) == (
        "cloudru",
        "config",
        status,
        True,
    )


def test_mws_500_is_error_and_possible_config_without_alert() -> None:
    assert classify_failure("mws", server("mws", 500, "INTERNAL")) == (
        "server_or_config",
        500,
    )
    assert classify_failure("cloudru", server("cloudru", 500)) == ("server", 500)
    assert classify_failure("cloudru", server("cloudru", 422)) == ("request", 422)
    assert classify_failure("mws", CallTimeoutError("mws", 7000.0)) == ("timeout", None)
    assert classify_failure("mws", httpx.ReadTimeout("t")) == ("timeout", None)
    # Порядок звеньев обратный: MWS первым, 500 — переход и запись кода.
    c = DecisionChain(
        [
            ChainLink("mws", Fake(server("mws", 500, "INTERNAL")), "gpt-oss-120b"),
            ChainLink("cloudru", Fake(ok("решение Cloud.ru")), "openai/gpt-oss-120b"),
        ]
    )
    assert c.complete(MESSAGES).text == "решение Cloud.ru"
    assert [(e.provider, e.outcome, e.status, e.alert) for e in c.events][0] == (
        "mws",
        "server_or_config",
        500,
        False,
    )


def test_both_links_down_raise_exhausted() -> None:
    c = chain(Fake(server("cloudru", 503)), Fake(httpx.ConnectError("обрыв")))
    with pytest.raises(ChainExhaustedError):
        c.complete(MESSAGES)
    assert [(e.provider, e.outcome) for e in c.events] == [
        ("cloudru", "server"),
        ("mws", "transport"),
    ]


def test_both_links_down_falls_back_to_plain_answer_in_loop() -> None:
    from corp_ed.prompts.faq import build_faq_messages

    class Chunk:
        title = "Регламент"
        heading_path = ("Регламент",)
        content = "Отпуск — 28 дней."

    c = chain(Fake(server("cloudru", 402)), Fake(server("mws", 500, "INTERNAL")))
    asked: list[Any] = []

    def answer(messages: list[Message]) -> Completion:
        asked.append(messages)
        return ok("28 дней [1].", 900.0)

    def decide(messages: list[Message]) -> Completion:
        return c.complete(messages)

    chunks = [Chunk()]
    run = run_decided(
        "Сколько дней отпуска?",
        chunks,
        decide=decide,
        decide_fast=decide,
        answer=answer,
        search=lambda _q: [],
    )
    # Решения нет — обычный путь ответа по выдержкам, как при таймауте.
    assert run.route is Route.ANSWER and run.text == "28 дней [1]."
    assert asked == [build_faq_messages("Сколько дней отпуска?", chunks)]
    assert run.timeout_reason == "fast" and run.decision_calls == []
    assert run.decisions == [{"timeout": "fast", "unavailable": True}]
    assert [label for label, _ in run.steps] == ["fast!unavailable", "answer"]
    assert [(e.provider, e.status, e.alert) for e in c.events] == [
        ("cloudru", 402, True),
        ("mws", 500, False),
    ]


def test_chain_needs_links() -> None:
    with pytest.raises(ValueError):
        DecisionChain([])
