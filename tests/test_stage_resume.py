"""deploy/stage/resume.py: возобновление стенда и сообщения в Telegram на смену
состояния (решение 12.10). Сеть подменяется: скрипт ходит только через
_request."""

import importlib.util
from pathlib import Path
from typing import Any

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "stage" / "resume.py"
spec = importlib.util.spec_from_file_location("stage_resume", SCRIPT)
assert spec and spec.loader
resume = importlib.util.module_from_spec(spec)
spec.loader.exec_module(resume)

TOKEN = "123456:secret-bot-token"
CHAT = "-1009876543210"


class FakeNet:
    """Ответы по началу адреса; запоминает, куда ходили."""

    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, str, Any]] = []

    def __call__(self, url: str, *, method: str = "GET", body: Any = None, **_: Any):
        self.calls.append((method, url, body))
        for prefix, answer in self.routes.items():
            if url.startswith(prefix):
                if isinstance(answer, Exception):
                    raise answer
                return answer
        raise AssertionError(f"неожиданный запрос: {method} {url}")

    def sent(self) -> list[str]:
        return [
            body["text"]
            for method, url, body in self.calls
            if url.startswith(resume.TELEGRAM_API)
        ]


def github_routes(previous: str | None) -> dict[str, Any]:
    """previous: conclusion задания-отметки в прошлом запуске (None — запусков нет)."""
    runs = [{"id": 7}] if previous else []
    jobs = [{"name": "Resume the preemptible stand", "conclusion": "success"}]
    if previous:
        jobs.append({"name": resume.MARKER_JOB, "conclusion": previous})
    return {
        f"{resume.GITHUB_API}/repos/o/r/actions/workflows/stage-resume.yaml/runs": (
            200,
            {},
            {"workflow_runs": [{"id": 9}, *runs]},
        ),
        f"{resume.GITHUB_API}/repos/o/r/actions/runs/7/jobs": (200, {}, {"jobs": jobs}),
    }


def env(tmp_path: Path, **extra: str) -> dict[str, str]:
    return {
        "STAGE_DOMAIN": "stage.example.ru",
        "TEAM_NOTIFY_TELEGRAM_BOT_TOKEN": TOKEN,
        "TEAM_NOTIFY_TELEGRAM_CHAT_ID": CHAT,
        "GITHUB_TOKEN": "ghs_x",
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_WORKFLOW_REF": (
            "o/r/.github/workflows/stage-resume.yaml@refs/heads/main"
        ),
        "GITHUB_RUN_ID": "9",
        "GITHUB_OUTPUT": str(tmp_path / "out"),
        **extra,
    }


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(resume, "PROBE_PAUSE", 0.0)


@pytest.mark.parametrize(
    ("previous", "health", "expected"),
    [
        (
            "skipped",
            (503, {}, None),
            "🔴 stage.example.ru не отвечает: /health/ready — HTTP 503.",
        ),
        ("success", (503, {}, None), None),
        ("success", (200, {}, {"status": "ok"}), "🟢 stage.example.ru снова работает."),
        ("skipped", (200, {}, {"status": "ok"}), None),
        (
            None,
            (503, {}, None),
            "🔴 stage.example.ru не отвечает: /health/ready — HTTP 503.",
        ),
        (None, (200, {}, {"status": "ok"}), None),
    ],
)
def test_message_only_on_state_change(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    previous: str | None,
    health: Any,
    expected: str | None,
) -> None:
    net = FakeNet(
        {
            **github_routes(previous),
            "https://stage.example.ru/health/ready": health,
            resume.TELEGRAM_API: (200, {}, {"ok": True}),
        }
    )
    monkeypatch.setattr(resume, "_request", net)

    assert resume.main(env(tmp_path)) == 0

    assert net.sent() == ([expected] if expected else [])
    down = health[0] != 200
    assert (tmp_path / "out").read_text() == f"down={'true' if down else 'false'}\n"
    # Журнал запусков публичный.
    out = capsys.readouterr()
    assert TOKEN not in out.out + out.err
    assert CHAT not in out.out + out.err


def test_retries_before_alerting(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    answers = iter([(502, {}, None), (502, {}, None), (200, {}, {"status": "ok"})])
    routes = {**github_routes("skipped"), resume.TELEGRAM_API: (200, {}, {})}

    def net(url: str, **kwargs: Any) -> Any:
        if url.endswith("/health/ready"):
            return next(answers)
        return FakeNet(routes)(url, **kwargs)

    monkeypatch.setattr(resume, "_request", net)
    assert resume.main(env(tmp_path)) == 0
    assert (tmp_path / "out").read_text() == "down=false\n"


def test_unreachable_stand(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    net = FakeNet(
        {
            **github_routes("skipped"),
            "https://stage.example.ru/": resume.urllib.error.URLError("refused"),
            resume.TELEGRAM_API: (200, {}, {}),
        }
    )
    monkeypatch.setattr(resume, "_request", net)
    assert resume.main(env(tmp_path)) == 0
    assert net.sent() == [
        "🔴 stage.example.ru не отвечает: /health/ready — нет соединения."
    ]


SELECTEL = {
    "SELECTEL_USER": "u",
    "SELECTEL_PASSWORD": "p",
    "SELECTEL_ACCOUNT_ID": "1",
    "SELECTEL_PROJECT": "kronto-stage",
    "SELECTEL_REGION": "ru-7",
    "STAGE_SERVER_ID": "srv",
}
CATALOG = {
    "token": {
        "catalog": [
            {
                "type": "compute",
                "endpoints": [
                    {
                        "interface": "public",
                        "region": "ru-7",
                        "url": "https://compute/v2.1/",
                    }
                ],
            }
        ]
    }
}


def selectel(status: str) -> dict[str, Any]:
    return {
        resume.IDENTITY_URL: (201, {"X-Subject-Token": "t"}, CATALOG),
        "https://compute/v2.1/servers/srv/action": (202, {}, None),
        "https://compute/v2.1/servers/srv": (200, {}, {"server": {"status": status}}),
    }


def test_shelved_server_is_resumed_and_reported_once(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    net = FakeNet(
        {
            **selectel("SHELVED_OFFLOADED"),
            **github_routes("skipped"),
            resume.TELEGRAM_API: (200, {}, {}),
        }
    )
    monkeypatch.setattr(resume, "_request", net)
    assert resume.main(env(tmp_path, **SELECTEL)) == 0
    assert (
        "POST",
        "https://compute/v2.1/servers/srv/action",
        {"unshelve": None},
    ) in net.calls
    assert len(net.sent()) == 1 and "Selectel остановил" in net.sent()[0]
    # Стенд лежит — снаружи не проверяем, отметка ставится.
    assert not any(url.endswith("/health/ready") for _, url, _ in net.calls)
    assert (tmp_path / "out").read_text() == "down=true\n"


def test_shut_off_server_keeps_state_silently(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    net = FakeNet({**selectel("SHUTOFF"), **github_routes("skipped")})
    monkeypatch.setattr(resume, "_request", net)
    assert resume.main(env(tmp_path, **SELECTEL)) == 0
    assert net.sent() == []
    assert (tmp_path / "out").read_text() == "down=false\n"


def test_selectel_error_still_checks_and_marks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    net = FakeNet(
        {
            resume.IDENTITY_URL: (401, {}, None),
            **github_routes("skipped"),
            "https://stage.example.ru/health/ready": (200, {}, {"status": "ok"}),
        }
    )
    monkeypatch.setattr(resume, "_request", net)
    assert resume.main(env(tmp_path, **SELECTEL)) == 1
    assert (tmp_path / "out").read_text() == "down=false\n"


def test_without_telegram_only_logs(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    net = FakeNet(
        {
            **github_routes("skipped"),
            "https://stage.example.ru/health/ready": (503, {}, None),
        }
    )
    monkeypatch.setattr(resume, "_request", net)
    settings = env(tmp_path)
    del (
        settings["TEAM_NOTIFY_TELEGRAM_BOT_TOKEN"],
        settings["TEAM_NOTIFY_TELEGRAM_CHAT_ID"],
    )
    assert resume.main(settings) == 0
    assert "Telegram не настроен" in capsys.readouterr().out


def test_bad_domain_is_rejected(tmp_path: Path) -> None:
    assert resume.main(env(tmp_path, STAGE_DOMAIN="evil.ru/x?")) == 64
