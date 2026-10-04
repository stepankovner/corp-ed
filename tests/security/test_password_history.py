"""Прежний пароль вернуть нельзя (services/passwords.py, стенд 04.10)."""

import pytest

from corp_ed.core.exceptions import WeakPasswordError
from corp_ed.core.security import hash_password, verify_password
from corp_ed.domain.models import Account
from corp_ed.services.passwords import PASSWORD_HISTORY, set_password

PASSWORDS = [f"длинная фраза номер {n} 2026" for n in range(5)]


def _account() -> Account:
    return Account(email="history@acme.ru", hashed_password=hash_password(PASSWORDS[0]))


def test_current_and_three_previous_are_rejected_older_is_allowed() -> None:
    account = _account()
    for password in PASSWORDS[1:]:
        set_password(account, password)

    for reused in reversed(PASSWORDS[1:]):  # текущий и три прежних
        with pytest.raises(WeakPasswordError, match="уже был"):
            set_password(account, reused)
    assert verify_password(PASSWORDS[4], account.hashed_password)

    set_password(account, PASSWORDS[0])  # пятый с конца — можно
    assert verify_password(PASSWORDS[0], account.hashed_password)


def test_history_keeps_three_newest_first() -> None:
    account = _account()
    for password in PASSWORDS[1:]:
        set_password(account, password)

    history = account.previous_password_hashes
    assert len(history) == PASSWORD_HISTORY
    assert [
        next(p for p in PASSWORDS if verify_password(p, hashed)) for hashed in history
    ] == [PASSWORDS[3], PASSWORDS[2], PASSWORDS[1]]


def test_rejected_password_changes_nothing() -> None:
    account = _account()
    before = account.hashed_password
    with pytest.raises(WeakPasswordError):
        set_password(account, PASSWORDS[0])
    assert account.hashed_password == before
    assert not account.previous_password_hashes
