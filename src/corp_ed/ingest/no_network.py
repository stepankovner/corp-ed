"""Запрет сокетов в дочернем процессе разбора (seccomp).

Процесс, который читает присланный файл, сети не открывает: ни TCP/UDP
(AF_INET, AF_INET6) — в контейнере рядом база и Redis, — ни AF_UNIX:
парсерам он не нужен, а через него доступны локальные службы (сокет
Docker, если его смонтируют; абстрактные сокеты хоста при сетевом режиме
host). Поэтому `socket()` и `socketpair()` возвращают EPERM для любого
семейства, а заодно и io_uring: с ядра 5.19 он создаёт сокеты сам, мимо
`socket()`. Чужая ABI (32-битные вызовы через int 0x80 и x32 на x86_64,
arm32 на aarch64) запрещена целиком: Python её не использует, а номера
вызовов в ней другие.

Почему seccomp, а не unshare(CLONE_NEWNET): в контейнере без
CAP_SYS_ADMIN профиль seccomp Docker по умолчанию запрещает unshare,
а prctl и seccomp разрешает. Фильтр ставится через ctypes, без
зависимостей: PR_SET_NO_NEW_PRIVS, затем PR_SET_SECCOMP с программой BPF.
Фильтр действует на процесс и всех его потомков и не снимается.

Номера вызовов и AUDIT_ARCH — из заголовков ядра (asm/unistd_64.h,
asm-generic/unistd.h, linux/audit.h). Другая архитектура — NetworkFilterError:
что делать, решает extract_worker.
"""

import ctypes
import platform
import sys

# Ядро: linux/prctl.h, linux/seccomp.h, linux/filter.h.
_PR_SET_NO_NEW_PRIVS = 38
_PR_SET_SECCOMP = 22
_SECCOMP_MODE_FILTER = 2
_SECCOMP_RET_ALLOW = 0x7FFF0000
_SECCOMP_RET_ERRNO = 0x00050000
_EPERM = 1
_BPF_LD_W_ABS = 0x20  # BPF_LD | BPF_W | BPF_ABS
_BPF_JEQ_K = 0x15  # BPF_JMP | BPF_JEQ | BPF_K
_BPF_JGE_K = 0x35  # BPF_JMP | BPF_JGE | BPF_K
_BPF_RET_K = 0x06  # BPF_RET | BPF_K
# Смещения в struct seccomp_data.
_OFFSET_NR = 0
_OFFSET_ARCH = 4
_X32_SYSCALL_BIT = 0x40000000

_IO_URING = (425, 426, 427)  # setup, enter, register — общие номера
_ARCHES: dict[str, tuple[int, tuple[int, ...]]] = {
    # AUDIT_ARCH, запрещённые вызовы: socket, socketpair, io_uring_*.
    "x86_64": (0xC000003E, (41, 53, *_IO_URING)),
    "aarch64": (0xC00000B7, (198, 199, *_IO_URING)),
}


class NetworkFilterError(Exception):
    """Фильтр не поставлен: не Linux, другая архитектура, ядро отказало."""


class _SockFilter(ctypes.Structure):
    _fields_ = [
        ("code", ctypes.c_ushort),
        ("jt", ctypes.c_ubyte),
        ("jf", ctypes.c_ubyte),
        ("k", ctypes.c_uint32),
    ]


class _SockFprog(ctypes.Structure):
    _fields_ = [
        ("len", ctypes.c_ushort),
        ("filter", ctypes.POINTER(_SockFilter)),
    ]


def _program(arch: int, denied: tuple[int, ...]) -> list[tuple[int, int, int, int]]:
    """Программа BPF: (code, jt, jf, k). Последняя инструкция — отказ,
    переходы «запретить» ведут на неё."""
    deny = (_BPF_RET_K, 0, 0, _SECCOMP_RET_ERRNO | _EPERM)
    head: list[tuple[int, int, int, int]] = [
        (_BPF_LD_W_ABS, 0, 0, _OFFSET_ARCH),
        # Своя ABI — дальше, чужая — сразу отказ.
        (_BPF_JEQ_K, 1, 0, arch),
        deny,
        (_BPF_LD_W_ABS, 0, 0, _OFFSET_NR),
    ]
    checks = [(_BPF_JGE_K, _X32_SYSCALL_BIT), *((_BPF_JEQ_K, nr) for nr in denied)]
    body: list[tuple[int, int, int, int]] = []
    for index, (code, value) in enumerate(checks):
        # До отказа: оставшиеся проверки и «разрешить».
        body.append((code, len(checks) - index, 0, value))
    return [*head, *body, (_BPF_RET_K, 0, 0, _SECCOMP_RET_ALLOW), deny]


def deny_network() -> None:
    """Поставить фильтр на текущий процесс. Вызывать, пока он однопоточный:
    фильтр prctl действует на вызвавший поток и его будущих потомков."""
    if sys.platform != "linux":
        raise NetworkFilterError(f"unsupported platform {sys.platform}")
    machine = platform.machine()
    if machine not in _ARCHES:
        raise NetworkFilterError(f"unsupported architecture {machine}")
    arch, denied = _ARCHES[machine]
    program = _program(arch, denied)
    filters = (_SockFilter * len(program))(*(_SockFilter(*op) for op in program))
    fprog = _SockFprog(len(program), filters)
    try:
        libc = ctypes.CDLL(None, use_errno=True)
    except OSError as exc:
        raise NetworkFilterError(f"libc: {exc}") from exc
    prctl = libc.prctl
    # Аргументы prctl — unsigned long: без argtypes ctypes передал бы int.
    prctl.argtypes = [ctypes.c_int, *[ctypes.c_ulong] * 4]
    prctl.restype = ctypes.c_int
    # NO_NEW_PRIVS обязателен без CAP_SYS_ADMIN; заодно setuid-программы
    # не получат прав больше, чем у процесса.
    if prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise NetworkFilterError(f"PR_SET_NO_NEW_PRIVS: errno {ctypes.get_errno()}")
    if prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.addressof(fprog), 0, 0):
        raise NetworkFilterError(f"PR_SET_SECCOMP: errno {ctypes.get_errno()}")
