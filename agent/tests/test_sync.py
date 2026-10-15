"""Обход целиком: ресурс в памяти, каталог в памяти, поддельный kronto.

Проверяет то, что видит сервер: какие файлы и с какими почтами пришли,
когда используется /access вместо повторной отправки, отзыв прав,
удаление в kronto только после полного обхода.
"""

import threading
from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from kronto_agent.config import ResourceConfig
from kronto_agent.kronto import KrontoClient, KrontoForbiddenError
from kronto_agent.sync import RunReport, Syncer
from tests.fake_kronto import KEY, FakeKronto
from tests.helpers import (
    ANNA,
    BORIS,
    DOMAIN_USERS,
    FOREIGN,
    GLEB,
    HR,
    INH,
    VERA,
    FakeDirectory,
    FakeFile,
    FakeShare,
    agent_cfg,
    allow,
    deny,
    factory,
    resource_cfg,
    sd,
)

ROOT = r"\\fs01\docs"
EVERYONE_MAILS = [
    "anna@example.com",
    "boris@example.com",
    "gleb@example.com",
    "vera@example.com",
]


def ext(rel: str) -> str:
    return rf"{ROOT}\{rel}".lower()


def make_share() -> FakeShare:
    return FakeShare(
        files={
            r"Общая\Регламент.docx": FakeFile(
                b"reg", sd(allow(DOMAIN_USERS, flags=INH))
            ),
            r"Кадры\Отпуска.xlsx": FakeFile(b"hr", sd(allow(HR, flags=INH))),
            r"Проекты\План.txt": FakeFile(
                b"plan", sd(deny(VERA, flags=INH), allow(DOMAIN_USERS, flags=INH))
            ),
            r"Общая\~$Регламент.docx": FakeFile(b"lock", sd(allow(DOMAIN_USERS))),
            r"Общая\Архив.zip": FakeFile(b"x" * 2048, sd(allow(DOMAIN_USERS))),
            r"Общая\Пусто.txt": FakeFile(b"none", sd(allow(FOREIGN))),
        }
    )


class Harness:
    def __init__(
        self, share: FakeShare | None = None, resources: tuple[ResourceConfig, ...] = ()
    ) -> None:
        self.share = share or make_share()
        self.fake = FakeKronto()
        self.directory = FakeDirectory()
        self.sleeps: list[float] = []
        self.cfg = replace(agent_cfg(*resources), max_file_bytes=1024)

    def run(self, stop: threading.Event | None = None) -> RunReport:
        client = KrontoClient(
            "https://kronto.test",
            KEY,
            transport=self.fake.transport(),
            sleep=self.sleeps.append,
        )
        syncer = Syncer(
            self.cfg, client, self.directory, factory({ROOT: self.share}), stop
        )
        return syncer.run()


def test_first_run_uploads_with_rights() -> None:
    h = Harness()
    report = h.run()
    assert report.complete
    docs = h.fake.docs
    assert set(docs) == {
        ext(r"Общая\Регламент.docx"),
        ext(r"Кадры\Отпуска.xlsx"),
        ext(r"Проекты\План.txt"),
    }
    assert docs[ext(r"Общая\Регламент.docx")].allowed_emails == EVERYONE_MAILS
    assert docs[ext(r"Кадры\Отпуска.xlsx")].allowed_emails == [
        "anna@example.com",
        "boris@example.com",
    ]
    assert docs[ext(r"Проекты\План.txt")].allowed_emails == [
        "anna@example.com",
        "boris@example.com",
        "gleb@example.com",
    ]
    meta = docs[ext(r"Кадры\Отпуска.xlsx")].meta
    assert meta["title"] == "Отпуска"
    assert meta["filename"] == "Отпуска.xlsx"
    assert meta["path"] == rf"{ROOT}\Кадры\Отпуска.xlsx"
    assert (
        meta["version"]
        == f"{int(datetime(2026, 10, 1, tzinfo=UTC).timestamp()) * 10**9}-2"
    )
    assert meta["modified_at"] == "2026-10-01T00:00:00Z"
    assert docs[ext(r"Кадры\Отпуска.xlsx")].content == b"hr"
    stats = report.stats
    assert stats["excluded"] == 1  # ~$Регламент.docx
    assert stats["too_large"] == 1  # Архив.zip > 1 КБ
    assert stats["no_readers"] == 1  # Пусто.txt: SID чужого домена
    assert stats["unresolved_allow"] == 1
    assert stats["users_without_email"] == 1  # egor
    assert stats["documents_added"] == 3
    finished = h.fake.runs[str(report.run_id)]
    assert finished["complete"] is True and finished["stats"]["documents_added"] == 3
    # Сотрудник без доступа документ не видит.
    assert ext(r"Кадры\Отпуска.xlsx") not in h.fake.visible_to("vera@example.com")
    assert ext(r"Проекты\План.txt") not in h.fake.visible_to("vera@example.com")


def test_unchanged_files_go_through_access() -> None:
    h = Harness()
    h.run()
    h.share.reads.clear()
    puts = len(h.fake.puts)
    # Права изменились без изменения файла: gleb добавлен в hr.
    h.directory.groups[HR] = frozenset({ANNA, BORIS, GLEB})
    report = h.run()
    assert report.complete
    assert len(h.fake.puts) == puts  # содержимое не отправлялось
    assert h.share.reads == []
    assert report.stats["access_sent"] == 3
    assert h.fake.docs[ext(r"Кадры\Отпуска.xlsx")].allowed_emails == [
        "anna@example.com",
        "boris@example.com",
        "gleb@example.com",
    ]
    assert len(h.fake.docs) == 3  # всё тронуто — ничего не удалено


def test_changed_file_is_uploaded_again() -> None:
    h = Harness()
    h.run()
    f = h.share.files[r"Общая\Регламент.docx"]
    f.content = b"reg v2"
    f.modified = datetime(2026, 10, 14, tzinfo=UTC)
    report = h.run()
    assert report.stats["documents_updated"] == 1
    assert h.fake.docs[ext(r"Общая\Регламент.docx")].content == b"reg v2"


def test_deleted_file_disappears_after_complete_run() -> None:
    h = Harness()
    h.run()
    del h.share.files[r"Проекты\План.txt"]
    assert h.run().complete
    assert ext(r"Проекты\План.txt") not in h.fake.docs


def test_lost_readers_are_revoked_via_access() -> None:
    h = Harness()
    h.run()
    h.directory.groups[HR] = frozenset()
    report = h.run()
    assert report.stats["access_revoked"] == 1
    assert h.fake.docs[ext(r"Кадры\Отпуска.xlsx")].allowed_emails == []


def test_unknown_in_access_is_uploaded() -> None:
    h = Harness()
    h.run()
    # kronto потерял документ, но /state его ещё показывал.
    stale = h.fake.docs.pop(ext(r"Кадры\Отпуска.xlsx"))
    h.fake.seed("phantom", "v", [])

    real_state = h.fake._state

    def state_with_stale(request: httpx.Request) -> httpx.Response:
        resp = real_state(request)
        data = resp.json()
        data["documents"].append(
            {"external_id": ext(r"Кадры\Отпуска.xlsx"), "version": stale.version}
        )
        return httpx.Response(200, json=data)

    h.fake._state = state_with_stale  # type: ignore[method-assign]
    report = h.run()
    assert report.stats["access_unknown"] == 1
    assert h.fake.docs[ext(r"Кадры\Отпуска.xlsx")].content == b"hr"
    assert "phantom" not in h.fake.docs


def test_network_error_in_folder_keeps_documents() -> None:
    h = Harness()
    h.run()
    h.share.broken_dirs.add("Кадры")
    report = h.run()
    assert not report.complete
    assert h.fake.runs[str(report.run_id)]["complete"] is False
    assert ext(r"Кадры\Отпуска.xlsx") in h.fake.docs  # не удалён


def test_unreachable_resource_is_incomplete() -> None:
    h = Harness()
    h.run()
    h.cfg = replace(h.cfg, resources=(resource_cfg(r"\\fs09\docs"),))
    report = h.run()
    assert not report.complete
    assert report.stats["resources_failed"] == 1
    assert len(h.fake.docs) == 3


def test_access_denied_is_skipped_and_removed() -> None:
    h = Harness()
    h.run()
    h.share.denied_sd.add(r"Кадры\Отпуска.xlsx")
    h.share.denied_dirs.add("Проекты")
    report = h.run()
    assert report.complete
    assert report.stats["sd_denied"] == 1
    assert report.stats["dirs_denied"] == 1
    assert set(h.fake.docs) == {ext(r"Общая\Регламент.docx")}


def test_sd_read_failure_is_incomplete() -> None:
    h = Harness()
    h.run()
    h.share.broken_sd.add(r"Кадры\Отпуска.xlsx")
    report = h.run()
    assert not report.complete
    assert ext(r"Кадры\Отпуска.xlsx") in h.fake.docs


def test_share_readers_intersection() -> None:
    h = Harness(resources=(resource_cfg(ROOT, share_readers="hr"),))
    h.run()
    assert h.fake.docs[ext(r"Общая\Регламент.docx")].allowed_emails == [
        "anna@example.com",
        "boris@example.com",
    ]
    assert ext(r"Проекты\План.txt") in h.fake.visible_to("anna@example.com")
    assert not h.fake.visible_to("gleb@example.com")


def test_unknown_share_readers_fails_resource() -> None:
    h = Harness(resources=(resource_cfg(ROOT, share_readers="nope"),))
    report = h.run()
    assert not report.complete
    assert h.fake.docs == {}


def test_server_rejections_are_counted() -> None:
    h = Harness()
    h.fake.reject_files = {"Регламент.docx": 415, "Отпуска.xlsx": 413}
    report = h.run()
    assert report.complete
    assert report.stats["documents_unsupported"] == 1
    assert report.stats["documents_too_large"] == 1
    assert set(h.fake.docs) == {ext(r"Проекты\План.txt")}


def test_forbidden_stops_without_finish() -> None:
    h = Harness()
    h.run()
    h.fake.forbidden = True
    with pytest.raises(KrontoForbiddenError):
        h.run()
    assert len(h.fake.docs) == 3


def test_kronto_outage_mid_run_finishes_incomplete() -> None:
    h = Harness()
    calls = {"n": 0}
    real = h.fake.handle

    def outage(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            calls["n"] += 1
            if calls["n"] >= 2:
                return httpx.Response(503)
        return real(request)

    h.fake.handle = outage  # type: ignore[method-assign]
    with pytest.raises(Exception, match="503"):
        h.run()
    (run,) = h.fake.runs.values()
    assert run["complete"] is False


def test_stop_event_makes_run_incomplete() -> None:
    h = Harness()
    stop = threading.Event()
    stop.set()
    report = h.run(stop)
    assert not report.complete
    assert h.fake.docs == {}


def test_many_unchanged_files_batched() -> None:
    share = FakeShare(
        files={f"f{i:04}.txt": FakeFile(b"1", sd(allow(ANNA))) for i in range(1100)}
    )
    h = Harness(share)
    h.run()
    report = h.run()
    assert report.stats["access_sent"] == 1100
    assert [len(b) for b in h.fake.access_calls] == [500, 500, 100]
