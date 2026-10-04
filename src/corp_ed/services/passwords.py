"""Новый пароль учётки — один путь для смены, сброса по ссылке и
временного пароля из CLI.

Прежний пароль вернуть нельзя: сбрасывают и меняют пароль чаще всего,
когда подозревают, что его узнал кто-то ещё, — и замена, после которой
можно поставить тот же пароль, ничего не защищает (стенд 04.10).
Сверяется с текущим и тремя прежними. Периодической смены пароля нет
(ASVS, NIST SP 800-63B), поэтому история не заставляет придумывать
Пароль1, Пароль2 — она срабатывает, только когда пароль меняют сами.

Проверка — только после всех остальных (второй фактор при сбросе): ответ
«этот пароль уже был» говорит о прежних паролях, его нельзя давать тому,
кто учёткой ещё не владеет.
"""

from corp_ed.core.exceptions import WeakPasswordError
from corp_ed.core.security import hash_password, verify_password
from corp_ed.domain.models import Account

PASSWORD_HISTORY = 3


def set_password(account: Account, new_password: str) -> None:
    """Сменить хеш пароля, а прежний — в историю. Политику пароля
    (validate_password) вызывающий проверяет сам и раньше."""
    hashes = [account.hashed_password, *(account.previous_password_hashes or [])]
    if any(verify_password(new_password, hashed) for hashed in hashes):
        raise WeakPasswordError("Этот пароль уже был у учётки — придумайте новый")
    # Новый список, а не append: ARRAY не отслеживает изменения на месте.
    account.previous_password_hashes = hashes[:PASSWORD_HISTORY]
    account.hashed_password = hash_password(new_password)
