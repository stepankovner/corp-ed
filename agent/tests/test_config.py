"""Файл настроек: секреты только из окружения/файлов, TLS для LDAP, пути UNC."""

from pathlib import Path

import pytest

from kronto_agent.config import DEFAULT_EXCLUDE, ConfigError, load_config, parse_unc

GOOD = r"""
interval_minutes = 30
max_file_mb = 10
upn_fallback = true

[kronto]
url = "https://kronto.example.ru/"
key_env = "KRONTO_AGENT_KEY"

[ldap]
url = "ldaps://dc1.corp.example"
base_dn = "DC=corp,DC=example"
bind_dn = "svc-kronto@corp.example"
password_file = "{secret}"
ca_file = "/etc/kronto-agent/ad-ca.pem"

[local_groups]
"s-1-5-32-545" = ["Domain Users"]

[[resources]]
path = '\\fs01\docs\Кадры'
username = 'CORP\svc-kronto'
password_env = "KRONTO_SMB_PASSWORD"
share_readers = "File-Readers"
exclude = ["Архив\\*"]

[[resources]]
path = "//fs02/public"
username = 'CORP\svc-kronto'
password_env = "KRONTO_SMB_PASSWORD"
port = 4445
encrypt = true
"""


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KRONTO_AGENT_KEY", "kag_t_c_secret")
    monkeypatch.setenv("KRONTO_SMB_PASSWORD", "smb-pass")


def write(tmp_path: Path, text: str) -> Path:
    secret = tmp_path / "ldap.pass"
    secret.write_text("ldap-pass\n", encoding="utf-8")
    path = tmp_path / "agent.toml"
    path.write_text(text.replace("{secret}", str(secret)), encoding="utf-8")
    return path


def test_full_config(tmp_path: Path, env: None) -> None:
    cfg = load_config(write(tmp_path, GOOD))
    assert cfg.kronto.url == "https://kronto.example.ru"
    assert cfg.kronto.key == "kag_t_c_secret"
    assert (cfg.ldap.host, cfg.ldap.port, cfg.ldap.use_ssl) == (
        "dc1.corp.example",
        636,
        True,
    )
    assert cfg.ldap.password == "ldap-pass"
    assert cfg.interval_minutes == 30
    assert cfg.max_file_bytes == 10 * 1024 * 1024
    assert cfg.upn_fallback is True
    assert cfg.exclude == DEFAULT_EXCLUDE
    assert cfg.local_groups == {"S-1-5-32-545": ("Domain Users",)}
    first, second = cfg.resources
    assert (first.server, first.share, first.subpath) == ("fs01", "docs", "Кадры")
    assert first.unc == r"\\fs01\docs\Кадры"
    assert first.share_readers == "File-Readers"
    assert first.exclude == ("Архив\\*",)
    assert (second.unc, second.port, second.encrypt) == (r"\\fs02\public", 4445, True)
    # Секреты не попадают в repr (журнал, трассировки).
    assert "smb-pass" not in repr(cfg) and "ldap-pass" not in repr(cfg)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('key_env = "KRONTO_AGENT_KEY"', 'key = "kag_x"', "открытым текстом"),
        (
            'password_env = "KRONTO_SMB_PASSWORD"\nshare',
            'password = "p"\nshare',
            "открытым",
        ),
        ('key_env = "KRONTO_AGENT_KEY"', 'key_env = "NO_SUCH_VAR"', "не задана"),
        ('"https://kronto.example.ru/"', '"http://kronto.example.ru/"', "https"),
        ('"ldaps://dc1.corp.example"', '"ldap://dc1.corp.example"', "без TLS"),
        ("path = '\\\\fs01", "path = 'fs01", "\\\\сервер"),
        ("interval_minutes = 30", "interval_minutes = 0", "interval_minutes"),
    ],
)
def test_rejected(tmp_path: Path, env: None, old: str, new: str, message: str) -> None:
    text = GOOD.replace(old, new, 1)
    assert text != GOOD
    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text))


def test_start_tls_and_key_prefix(
    tmp_path: Path, env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    text = GOOD.replace('"ldaps://dc1.corp.example"', '"ldap://dc1.corp.example"')
    text = text.replace('ca_file = "/etc', 'start_tls = true\nca_file = "/etc')
    cfg = load_config(write(tmp_path, text))
    assert (cfg.ldap.port, cfg.ldap.use_ssl, cfg.ldap.start_tls) == (389, False, True)
    monkeypatch.setenv("KRONTO_AGENT_KEY", "not-an-agent-key")
    with pytest.raises(ConfigError, match="kag_"):
        load_config(write(tmp_path, GOOD))


def test_missing_sections(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="kronto"):
        load_config(write(tmp_path, "interval_minutes = 5\n"))
    with pytest.raises(ConfigError):
        load_config(write(tmp_path, "not toml ["))
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")


@pytest.mark.parametrize(
    ("path", "parts"),
    [
        (r"\\fs01\docs", ("fs01", "docs", "")),
        (r"\\fs01\docs\a\b\\", ("fs01", "docs", r"a\b")),
        ("//fs01.corp/docs/Отдел кадров", ("fs01.corp", "docs", "Отдел кадров")),
    ],
)
def test_parse_unc(path: str, parts: tuple[str, str, str]) -> None:
    assert parse_unc(path) == parts


def test_parse_unc_without_share() -> None:
    with pytest.raises(ConfigError):
        parse_unc(r"\\fs01")
