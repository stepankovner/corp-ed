"""Песочница разбора: без сети, без чужих дескрипторов, без рабочего каталога.

Фильтр seccomp проверяется по-настоящему: в отдельном процессе, на ядре
этой машины. Тесты с фильтром — только для Linux x86_64 и aarch64.
"""

import json
import platform
import subprocess
import sys
from types import SimpleNamespace

import pytest
from structlog.testing import capture_logs

from corp_ed.ingest import sandbox
from corp_ed.ingest.extract import ERROR_MESSAGES, ExtractionError, SourceFormat
from corp_ed.ingest.sandbox import extract_isolated
from tests.ingest import samples

needs_seccomp = pytest.mark.skipif(
    sys.platform != "linux" or platform.machine() not in ("x86_64", "aarch64"),
    reason="фильтр seccomp собран для Linux x86_64 и aarch64",
)


def _run_child(script: str) -> dict[str, object]:
    """Скрипт в отдельном процессе, как дочерний процесс песочницы; ответ —
    JSON из stdout."""
    done = subprocess.run(
        [sys.executable, "-I", "-c", script],
        capture_output=True,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"},
        check=False,
    )
    assert done.returncode == 0, done.stderr.decode()
    answer: dict[str, object] = json.loads(done.stdout)
    return answer


_TRY_SOCKETS = """
import errno, json, socket
result = {}
for name in ("AF_INET", "AF_INET6", "AF_UNIX"):
    try:
        socket.socket(getattr(socket, name), socket.SOCK_STREAM).close()
        result[name] = "open"
    except OSError as exc:
        result[name] = errno.errorcode.get(exc.errno, str(exc.errno))
try:
    socket.socketpair()
    result["socketpair"] = "open"
except OSError as exc:
    result["socketpair"] = errno.errorcode.get(exc.errno, str(exc.errno))
with open("/proc/self/status") as status:
    result["seccomp"] = [
        line.split()[1] for line in status if line.startswith("Seccomp:")
    ][0]
print(json.dumps(result))
"""


@needs_seccomp
def test_child_under_filter_cannot_open_sockets() -> None:
    result = _run_child(
        "from corp_ed.ingest.no_network import deny_network\n"
        "deny_network()\n" + _TRY_SOCKETS
    )
    assert result == {
        "AF_INET": "EPERM",
        "AF_INET6": "EPERM",
        "AF_UNIX": "EPERM",
        "socketpair": "EPERM",
        "seccomp": "2",
    }


def test_without_filter_sockets_open() -> None:
    """Контроль к тесту выше: без фильтра те же вызовы проходят."""
    result = _run_child(_TRY_SOCKETS)
    assert result["AF_INET"] == "open"
    assert result["seccomp"] == "0"


@needs_seccomp
def test_worker_confines_itself_before_parsing() -> None:
    result = _run_child(
        "import os\n"
        "from corp_ed.ingest import extract_worker\n"
        "assert extract_worker._confine() is True\n"
        "cwd_listing = os.listdir('.')\n"
        "try:\n"
        "    open('probe.txt', 'w')\n"
        "    cwd_writable = True\n"
        "except OSError:\n"
        "    cwd_writable = False\n"
        + _TRY_SOCKETS.replace(
            "print(json.dumps(result))",
            "result['cwd'] = cwd_listing\n"
            "result['cwd_writable'] = cwd_writable\n"
            "print(json.dumps(result))",
        )
    )
    assert result["AF_INET"] == "EPERM"
    assert result["seccomp"] == "2"
    # Рабочий каталог — пустой и уже удалённый: относительные пути
    # никуда не ведут, и на диске после процесса ничего не остаётся.
    assert result["cwd"] == []
    assert result["cwd_writable"] is False


def test_unknown_architecture_is_reported(monkeypatch: pytest.MonkeyPatch) -> None:
    from corp_ed.ingest import no_network

    monkeypatch.setattr(no_network.platform, "machine", lambda: "riscv64")
    with pytest.raises(no_network.NetworkFilterError):
        no_network.deny_network()


def _verdict(program: list[tuple[int, int, int, int]], arch: int, nr: int) -> int:
    """Мини-интерпретатор BPF на тех инструкциях, что строит no_network."""
    pc, acc = 0, 0
    while True:
        code, jt, jf, k = program[pc]
        if code == 0x20:
            acc = {0: nr, 4: arch}[k]
            pc += 1
        elif code in (0x15, 0x35):
            hit = acc == k if code == 0x15 else acc >= k
            pc += 1 + (jt if hit else jf)
        else:
            assert code == 0x06
            return k


@pytest.mark.parametrize(
    ("machine", "arch", "sockets", "harmless_nr"),
    [
        # socket, socketpair; безобидный — openat (257 и 56).
        ("x86_64", 0xC000003E, (41, 53), 257),
        ("aarch64", 0xC00000B7, (198, 199), 56),
    ],
)
def test_filter_program_logic(
    machine: str, arch: int, sockets: tuple[int, int], harmless_nr: int
) -> None:
    """Логика программы — и для aarch64, которого на этой машине нет."""
    from corp_ed.ingest import no_network

    own_arch, denied = no_network._ARCHES[machine]
    assert own_arch == arch
    program = no_network._program(own_arch, denied)
    allow, eperm = 0x7FFF0000, 0x00050001
    assert _verdict(program, arch, harmless_nr) == allow
    for nr in sockets:
        assert _verdict(program, arch, nr) == eperm
    # Остальные сетевые вызовы (accept, bind) не трогаем: сокета нет.
    assert _verdict(program, arch, sockets[0] + 2) == allow
    for io_uring in (425, 426, 427):
        assert _verdict(program, arch, io_uring) == eperm
    # x32 на x86_64 и чужая ABI (i386, arm32) — отказ целиком.
    assert _verdict(program, arch, 0x40000000 | harmless_nr) == eperm
    assert _verdict(program, 0x40000003, harmless_nr) == eperm
    assert _verdict(program, 0x40000028, harmless_nr) == eperm


# --- разбор под фильтром ------------------------------------------------------


@needs_seccomp
async def test_documents_are_parsed_under_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Строгая политика: без фильтра ребёнок отказался бы разбирать.
    _environment(monkeypatch, "production")
    docx = samples.docx([("Раздел", "Heading1"), ("Текст раздела.", None)])
    pdf = samples.pdf([[("Vacation policy", 18), ("Vacation lasts 28 days.", 11)]])
    with capture_logs() as logs:
        assert "# Раздел" in await extract_isolated(SourceFormat.DOCX, docx)
        assert "28 days" in await extract_isolated(SourceFormat.PDF, pdf)
    assert not [entry for entry in logs if entry["event"].startswith("sandbox_")]


async def test_child_gets_only_standard_descriptors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Дескрипторы API (сокеты базы, Redis, клиентов) ребёнку не
    достаются: у него только stdin, stdout и stderr."""
    monkeypatch.setattr(
        sandbox,
        "_worker_command",
        lambda mode, cpu_seconds: [
            sys.executable,
            "-I",
            "-c",
            "import json, os\n"
            "fds = sorted(int(fd) for fd in os.listdir('/proc/self/fd'))\n"
            "print(json.dumps({'ok': True, 'markdown': repr(fds)}))\n",
        ],
    )
    # Последний — каталог, который открыл сам listdir.
    assert (await extract_isolated(SourceFormat.TXT, b"x"))[:9] == "[0, 1, 2,"


# --- политика: production — отказ, разработка — предупреждение ---------------


def _worker_without_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """Настоящий extract_worker, у которого фильтр не ставится."""
    monkeypatch.setattr(
        sandbox,
        "_worker_command",
        lambda mode, cpu_seconds: [
            sys.executable,
            "-I",
            "-c",
            "import sys\n"
            "from corp_ed.ingest import extract_worker, no_network\n"
            "def broken():\n"
            "    raise no_network.NetworkFilterError('test: no seccomp')\n"
            "no_network.deny_network = broken\n"
            f"sys.argv = ['extract_worker', {mode!r}, {str(cpu_seconds)!r},"
            f" {sandbox._network_policy()!r}]\n"
            "sys.exit(extract_worker.main())\n",
        ],
    )


def _environment(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    monkeypatch.setattr(
        sandbox,
        "get_settings",
        lambda: SimpleNamespace(is_production=name == "production"),
    )


def test_policy_follows_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _environment(monkeypatch, "production")
    assert sandbox._worker_command("pdf", 10)[-1] == "strict"
    _environment(monkeypatch, "development")
    assert sandbox._worker_command("pdf", 10)[-1] == "lenient"


async def test_production_child_refuses_without_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(monkeypatch, "production")
    _worker_without_filter(monkeypatch)
    with capture_logs() as logs, pytest.raises(ExtractionError) as info:
        await extract_isolated(SourceFormat.TXT, "текст".encode())
    assert info.value.code == "sandbox_unavailable"
    assert "sandbox_unavailable" in ERROR_MESSAGES
    assert [
        entry["log_level"]
        for entry in logs
        if entry["event"] == "sandbox_network_filter_unavailable"
    ] == ["error"]


async def test_development_child_warns_once_and_parses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _environment(monkeypatch, "development")
    _worker_without_filter(monkeypatch)
    monkeypatch.setattr(sandbox, "_network_warning_logged", False)
    with capture_logs() as logs:
        assert await extract_isolated(SourceFormat.TXT, "раз".encode()) == "раз"
        assert await extract_isolated(SourceFormat.TXT, "два".encode()) == "два"
    warnings = [
        entry for entry in logs if entry["event"] == "sandbox_network_not_blocked"
    ]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
