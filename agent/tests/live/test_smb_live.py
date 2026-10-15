"""Живая проверка агента на стенде tests/live/smb: Samba 4 в роли
контроллера домена AD, ресурс docs с правами NTFS (vfs_acl_xattr).

Агент обходит ресурс по SMB 3 под учёткой svc-kronto (только чтение),
читает дескрипторы безопасности, раскрывает группы через LDAPS и
отправляет файлы в поддельный kronto (httpx.MockTransport). Ожидания —
EXPECTED; засев — tests/live/smb/seed.sh. Запуск — в сети стенда:

    docker compose -f tests/live/smb/compose.yaml run --rm tester

Без SMB_STAND=1 тесты пропускаются. Пароли — выдуманные, со стенда.
"""

import os
from dataclasses import replace
from pathlib import Path

import pytest

from kronto_agent.config import AgentConfig, load_config
from kronto_agent.directory import AdDirectory, DirectoryError, ldap3_search
from kronto_agent.kronto import KrontoClient
from kronto_agent.security import Control, parse_security_descriptor
from kronto_agent.smb import SmbError, SmbResource
from kronto_agent.sync import RunReport, Syncer
from tests.fake_kronto import KEY, FakeKronto

SERVER = os.environ.get("SMB_STAND_SERVER", "dc1.kronto.test")
CA = os.environ.get("SMB_STAND_CA", "/export/ca.pem")
PASSWORD = "Kronto-Stand-User-1"

pytestmark = pytest.mark.skipif(
    os.environ.get("SMB_STAND") != "1",
    reason="нужен стенд tests/live/smb (SMB_STAND=1, запуск в его сети docker)",
)

ANNA, BORIS, VERA, GLEB = (
    f"{n}@example.com" for n in ("anna", "boris", "vera", "gleb")
)
EXPECTED = {
    # Domain Users, унаследовано с папки; отключённый dmitry и egor без
    # почты не попадают.
    r"Общая\Регламент.txt": [ANNA, BORIS, GLEB, VERA],
    # Наследование отключено, явное разрешение только gleb.
    r"Общая\Секрет директора.txt": [GLEB],
    # Группа hr: anna и boris через вложенную hr-leads.
    r"Кадры\Отпуска.txt": [ANNA, BORIS],
    r"Кадры\Оценка\Итоги.txt": [ANNA, BORIS],
    # Domain Users с явным запретом vera на папке.
    r"Проекты\План.txt": [ANNA, BORIS, GLEB],
    # Явное разрешение vera сильнее унаследованного запрета.
    r"Проекты\Для Веры.txt": [ANNA, BORIS, GLEB, VERA],
}
# С upn_fallback почту получают egor (Domain Users — там, где читают все)
# и сама учётка агента (её группа kronto-readers читает весь ресурс).
UPN_EXTRA = {r"Общая\Регламент.txt", r"Проекты\План.txt", r"Проекты\Для Веры.txt"}
AGENT_UPN = "svc-kronto@kronto.test"

CONFIG = f"""
max_file_mb = 1
upn_fallback = {{upn}}

[kronto]
url = "https://kronto.test"
key_env = "KRONTO_AGENT_KEY"

[ldap]
url = "ldaps://{{ldap_host}}"
base_dn = "DC=kronto,DC=test"
bind_dn = "svc-kronto@kronto.test"
password_env = "STAND_PASSWORD"
ca_file = "{CA}"

[[resources]]
path = '\\\\{SERVER}\\docs'
username = 'KRONTO\\svc-kronto'
password_env = "{{smb_password_env}}"
"""


def ext(rel: str) -> str:
    return rf"\\{SERVER}\docs\{rel}".lower()


@pytest.fixture
def make_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("KRONTO_AGENT_KEY", KEY)
    monkeypatch.setenv("STAND_PASSWORD", PASSWORD)
    monkeypatch.setenv("WRONG_PASSWORD", "Not-The-Password-1")

    def make(
        upn: bool = False,
        ldap_host: str = SERVER,
        smb_password_env: str = "STAND_PASSWORD",
    ) -> AgentConfig:
        path = tmp_path / "agent.toml"
        path.write_text(
            CONFIG.format(
                upn=str(upn).lower(),
                ldap_host=ldap_host,
                smb_password_env=smb_password_env,
            ),
            encoding="utf-8",
        )
        return load_config(path)

    return make


def run_agent(cfg: AgentConfig, fake: FakeKronto) -> RunReport:
    search, close = ldap3_search(cfg.ldap)
    client = KrontoClient(cfg.kronto.url, cfg.kronto.key, transport=fake.transport())
    try:
        directory = AdDirectory(search, cfg.ldap.base_dn, upn_fallback=cfg.upn_fallback)
        return Syncer(cfg, client, directory, SmbResource).run()
    finally:
        client.close()
        close()


def test_directory_over_ldaps(make_cfg) -> None:  # type: ignore[no-untyped-def]
    cfg = make_cfg()
    search, close = ldap3_search(cfg.ldap)
    try:
        directory = AdDirectory(search, cfg.ldap.base_dn)
        emails = {u.email for u in directory.users.values() if u.email}
        assert emails == {ANNA, BORIS, VERA, GLEB}  # без отключённого dmitry
        hr = directory.sid_by_name("hr")
        assert hr is not None
        members = directory.members(hr) or frozenset()
        assert {directory.email(s) for s in members} == {ANNA, BORIS}
        assert directory.members(f"{directory.domain_sid}-513") == directory.all_users()
    finally:
        close()


def test_walk_compute_rights_and_send(make_cfg) -> None:  # type: ignore[no-untyped-def]
    fake = FakeKronto()
    report = run_agent(make_cfg(), fake)
    assert report.complete, report.errors
    assert report.allowed == {ext(rel): emails for rel, emails in EXPECTED.items()}
    assert set(fake.docs) == {ext(rel) for rel in EXPECTED}
    for rel, emails in EXPECTED.items():
        doc = fake.docs[ext(rel)]
        assert doc.allowed_emails == emails, rel
        assert doc.content.decode() == f"Документ стенда: {rel}\n"
        assert doc.meta["path"] == rf"\\{SERVER}\docs\{rel}"
    stats = report.stats
    assert stats["excluded"] == 2  # ~$Регламент.txt, Thumbs.db
    assert stats["too_large"] == 1  # Архив.bin, 2 МБ при лимите 1 МБ
    assert stats["documents_added"] == len(EXPECTED)
    assert stats["unresolved_deny"] == 0
    # Второй обход: файлы не менялись — только /access, без содержимого.
    puts = len(fake.puts)
    again = run_agent(make_cfg(), fake)
    assert again.complete
    assert len(fake.puts) == puts
    assert again.stats["access_sent"] == len(EXPECTED)
    assert set(fake.docs) == {ext(rel) for rel in EXPECTED}


def test_upn_fallback(make_cfg) -> None:  # type: ignore[no-untyped-def]
    fake = FakeKronto()
    report = run_agent(make_cfg(upn=True), fake)
    assert report.complete
    for rel, emails in EXPECTED.items():
        extra = [AGENT_UPN] + (["egor@kronto.test"] if rel in UPN_EXTRA else [])
        expected = sorted(emails + extra)
        assert fake.docs[ext(rel)].allowed_emails == expected, rel


def test_protected_descriptor_from_server(make_cfg) -> None:  # type: ignore[no-untyped-def]
    cfg = make_cfg()
    with SmbResource(cfg.resources[0]) as resource:
        raw = resource.security_descriptor(
            rf"\\{SERVER}\docs\Общая\Секрет директора.txt"
        )
        inherited = resource.security_descriptor(
            rf"\\{SERVER}\docs\Кадры\Оценка\Итоги.txt"
        )
    sd = parse_security_descriptor(raw)
    assert sd.protected
    assert sd.dacl is not None and not any(a.inherited for a in sd.dacl)
    sd2 = parse_security_descriptor(inherited)
    assert sd2.control & Control.DACL_AUTO_INHERITED
    assert sd2.dacl is not None and all(a.inherited for a in sd2.dacl)


def test_ldap_certificate_name_is_checked(make_cfg) -> None:  # type: ignore[no-untyped-def]
    # «dc» — имя сервиса в той же сети, сертификат выписан на dc1.kronto.test.
    cfg = make_cfg(ldap_host="dc")
    with pytest.raises(DirectoryError):
        ldap3_search(cfg.ldap)


def test_wrong_smb_password(make_cfg) -> None:  # type: ignore[no-untyped-def]
    cfg = make_cfg(smb_password_env="WRONG_PASSWORD")
    with pytest.raises(SmbError), SmbResource(cfg.resources[0]):
        pass


def test_ldap_start_tls(make_cfg) -> None:  # type: ignore[no-untyped-def]
    ldaps = make_cfg().ldap
    cfg = replace(ldaps, port=389, use_ssl=False, start_tls=True)
    search, close = ldap3_search(cfg)
    try:
        assert AdDirectory(search, cfg.base_dn).users
    finally:
        close()
    with pytest.raises(DirectoryError):
        ldap3_search(replace(cfg, host="dc"))
