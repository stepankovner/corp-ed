"""Тексты писем (ТЗ §3). У каждого письма — текст и HTML с тем же смыслом.

Всё, что приходит от человека (имя, название компании, почта), в HTML
экранируется: письмо не должно стать способом вставить разметку или
ссылку от имени kronto. Стили — встроенные: почтовые клиенты не читают
<style> и внешние файлы.
"""

from dataclasses import dataclass
from datetime import date
from html import escape

BRAND = "kronto"
_ACCENT = "#3b34d6"
_INK = "#16161a"
_MUTED = "#5e5e66"
_SURFACE = "#f5f1e8"


@dataclass(frozen=True)
class RenderedEmail:
    kind: str
    subject: str
    text: str
    html: str


def _greeting(name: str | None) -> str:
    return f"Здравствуйте, {name}!" if name else "Здравствуйте!"


def _layout(title: str, blocks: list[str]) -> str:
    font = "-apple-system,'Segoe UI',Roboto,Arial,sans-serif"
    table = 'role="presentation" width="100%" cellpadding="0" cellspacing="0"'
    card = "max-width:520px;background:#ffffff;border-radius:16px;padding:32px;"
    footer = (
        f"font-size:12px;color:{_MUTED};max-width:520px;line-height:1.5;"
        "margin:16px auto 0;"
    )
    return (
        '<!doctype html><html lang="ru"><head><meta charset="utf-8">'
        f"<title>{escape(title)}</title></head>"
        f'<body style="margin:0;padding:24px 12px;background:{_SURFACE};'
        f'font-family:{font};color:{_INK};">'
        f'<table {table}><tr><td align="center">'
        f'<table {table} style="{card}">'
        '<tr><td style="font-size:22px;font-weight:600;letter-spacing:-0.02em;'
        f'padding-bottom:24px;">{BRAND}<span style="color:{_ACCENT};">.</span>'
        "</td></tr>"
        f'<tr><td style="font-size:16px;line-height:1.6;">{"".join(blocks)}'
        "</td></tr></table>"
        f'<p style="{footer}">Письмо отправлено автоматически, отвечать на него '
        "не нужно.</p>"
        "</td></tr></table></body></html>"
    )


def _p(text: str) -> str:
    return f'<p style="margin:0 0 16px;">{text}</p>'


def _muted(text: str) -> str:
    return f'<p style="margin:0 0 16px;font-size:14px;color:{_MUTED};">{text}</p>'


def _button(label: str, url: str) -> str:
    return (
        '<p style="margin:8px 0 24px;">'
        f'<a href="{escape(url, quote=True)}" style="display:inline-block;'
        f"background:{_INK};color:#ffffff;text-decoration:none;padding:12px 22px;"
        f'border-radius:999px;font-weight:500;">{escape(label)}</a></p>'
    )


def _code(code: str) -> str:
    return (
        f'<p style="margin:8px 0 24px;font-size:32px;letter-spacing:0.25em;'
        f'font-weight:600;font-family:Menlo,Consolas,monospace;">{escape(code)}</p>'
    )


def verify_email(
    *, name: str | None, code: str, url: str, minutes: int
) -> RenderedEmail:
    subject = f"Код подтверждения {BRAND}: {code}"
    text = (
        f"{_greeting(name)}\n\n"
        f"Код для подтверждения почты: {code}\n"
        f"Или откройте ссылку: {url}\n\n"
        f"Код и ссылка действуют {minutes} минут. Если вы не регистрировались "
        f"в {BRAND}, просто удалите это письмо."
    )
    html = _layout(
        subject,
        [
            _p(escape(_greeting(name))),
            _p("Введите этот код, чтобы подтвердить почту:"),
            _code(code),
            _p("Или нажмите кнопку:"),
            _button("Подтвердить почту", url),
            _muted(
                f"Код и ссылка действуют {minutes} минут. Если вы не "
                f"регистрировались в {BRAND}, просто удалите это письмо."
            ),
        ],
    )
    return RenderedEmail("verify_email", subject, text, html)


def account_exists(
    *, name: str | None, login_url: str, reset_url: str
) -> RenderedEmail:
    """Кто-то регистрируется на почту, у которой уже есть учётка.

    Ответ на регистрацию одинаковый в обоих случаях — иначе по нему
    перебирались бы чужие адреса; владельцу адреса — письмо.
    """
    subject = f"У вас уже есть учётная запись {BRAND}"
    text = (
        f"{_greeting(name)}\n\n"
        f"На этот адрес пытались зарегистрироваться в {BRAND}, но учётная запись "
        f"уже есть.\nВойти: {login_url}\nЗабыли пароль: {reset_url}\n\n"
        "Если это были не вы, ничего делать не нужно."
    )
    html = _layout(
        subject,
        [
            _p(escape(_greeting(name))),
            _p(
                f"На этот адрес пытались зарегистрироваться в {BRAND}, но учётная "
                "запись уже есть."
            ),
            _button("Войти", login_url),
            _p(
                f'Забыли пароль? <a href="{escape(reset_url, quote=True)}" '
                f'style="color:{_ACCENT};">Восстановить</a>'
            ),
            _muted("Если это были не вы, ничего делать не нужно."),
        ],
    )
    return RenderedEmail("account_exists", subject, text, html)


def reset_password(*, name: str | None, url: str, minutes: int) -> RenderedEmail:
    subject = f"Восстановление пароля {BRAND}"
    text = (
        f"{_greeting(name)}\n\n"
        f"Чтобы задать новый пароль, откройте ссылку: {url}\n"
        f"Ссылка действует {minutes} минут и срабатывает один раз.\n\n"
        "Если вы не просили восстановить пароль, ничего делать не нужно: "
        "старый пароль продолжит работать."
    )
    html = _layout(
        subject,
        [
            _p(escape(_greeting(name))),
            _p("Чтобы задать новый пароль, нажмите кнопку:"),
            _button("Задать новый пароль", url),
            _muted(
                f"Ссылка действует {minutes} минут и срабатывает один раз. Если вы "
                "не просили восстановить пароль, ничего делать не нужно: старый "
                "пароль продолжит работать."
            ),
        ],
    )
    return RenderedEmail("reset_password", subject, text, html)


def confirm_new_email(
    *, name: str | None, new_email: str, url: str, hours: int
) -> RenderedEmail:
    subject = f"Подтвердите новую почту в {BRAND}"
    text = (
        f"{_greeting(name)}\n\n"
        f"Чтобы входить в {BRAND} с адресом {new_email}, откройте ссылку: {url}\n"
        f"Ссылка действует {hours} часов."
    )
    html = _layout(
        subject,
        [
            _p(escape(_greeting(name))),
            _p(
                f"Чтобы входить в {BRAND} с адресом <b>{escape(new_email)}</b>, "
                "подтвердите его:"
            ),
            _button("Подтвердить почту", url),
            _muted(f"Ссылка действует {hours} часов."),
        ],
    )
    return RenderedEmail("change_email", subject, text, html)


def email_change_code(
    *, name: str | None, code: str, new_email: str, minutes: int
) -> RenderedEmail:
    """Второй фактор смены почты для учётки без приложения (ТЗ §3): код
    приходит на прежний адрес — сменить почту может только её владелец."""
    subject = f"Код для смены почты в {BRAND}: {code}"
    text = (
        f"{_greeting(name)}\n\n"
        f"Код для смены почты на {new_email}: {code}\n"
        f"Действует {minutes} минут.\n\n"
        "Если вы не меняли почту, смените пароль: его знает кто-то ещё."
    )
    html = _layout(
        subject,
        [
            _p(escape(_greeting(name))),
            _p(f"Код для смены почты на <b>{escape(new_email)}</b>:"),
            _code(code),
            _muted(
                f"Действует {minutes} минут. Если вы не меняли почту, смените "
                "пароль: его знает кто-то ещё."
            ),
        ],
    )
    return RenderedEmail("change_email_code", subject, text, html)


def email_changed(
    *, name: str | None, new_email: str, revert_url: str, days: int
) -> RenderedEmail:
    subject = f"Почта в {BRAND} изменена"
    text = (
        f"{_greeting(name)}\n\n"
        f"Почта вашей учётной записи {BRAND} изменена на {new_email}.\n"
        f"Если это были не вы, откройте ссылку в течение {days} дней: {revert_url}\n"
        "Мы вернём прежнюю почту, завершим все сеансы и пришлём ссылку для "
        "нового пароля."
    )
    html = _layout(
        subject,
        [
            _p(escape(_greeting(name))),
            _p(
                f"Почта вашей учётной записи {BRAND} изменена на "
                f"<b>{escape(new_email)}</b>."
            ),
            _p("Если это были не вы:"),
            _button("Это не я — вернуть почту", revert_url),
            _muted(
                f"Ссылка действует {days} дней. Мы вернём прежнюю почту, завершим "
                "все сеансы и пришлём ссылку для нового пароля."
            ),
        ],
    )
    return RenderedEmail("email_changed", subject, text, html)


def password_changed(*, name: str | None, reset_url: str) -> RenderedEmail:
    subject = f"Пароль {BRAND} изменён"
    text = (
        f"{_greeting(name)}\n\n"
        f"Пароль вашей учётной записи {BRAND} только что изменён, все сеансы "
        "на других устройствах завершены.\n"
        f"Если это были не вы, сразу восстановите пароль: {reset_url}"
    )
    html = _layout(
        subject,
        [
            _p(escape(_greeting(name))),
            _p(
                f"Пароль вашей учётной записи {BRAND} только что изменён, все "
                "сеансы на других устройствах завершены."
            ),
            _p("Если это были не вы, сразу восстановите пароль:"),
            _button("Восстановить пароль", reset_url),
        ],
    )
    return RenderedEmail("password_changed", subject, text, html)


def company_approved(*, name: str | None, company: str, url: str) -> RenderedEmail:
    subject = f"Компания «{company}» подключена к {BRAND}"
    text = (
        f"{_greeting(name)}\n\n"
        f"Мы подключили компанию «{company}». Вы — её администратор: загрузите "
        "документы или подключите источники, затем пригласите сотрудников.\n"
        f"Открыть {BRAND}: {url}"
    )
    html = _layout(
        subject,
        [
            _p(escape(_greeting(name))),
            _p(
                f"Мы подключили компанию «{escape(company)}». Вы — её "
                "администратор: загрузите документы или подключите источники, "
                "затем пригласите сотрудников."
            ),
            _button(f"Открыть {BRAND}", url),
        ],
    )
    return RenderedEmail("company_approved", subject, text, html)


def company_rejected(*, name: str | None, company: str) -> RenderedEmail:
    subject = f"Заявка на подключение компании «{company}»"
    text = (
        f"{_greeting(name)}\n\n"
        f"Мы рассмотрели заявку на подключение компании «{company}» и пока не "
        "можем её одобрить. Если хотите обсудить — ответьте на сообщение нашей "
        "команды или напишите в поддержку."
    )
    html = _layout(
        subject,
        [
            _p(escape(_greeting(name))),
            _p(
                f"Мы рассмотрели заявку на подключение компании «{escape(company)}» "
                "и пока не можем её одобрить. Если хотите обсудить — напишите в "
                "поддержку."
            ),
        ],
    )
    return RenderedEmail("company_rejected", subject, text, html)


def _device_line(device: str | None, ip: str | None) -> str:
    parts = [p for p in (device, f"адрес {ip}" if ip else None) if p]
    return ", ".join(parts) if parts else "неизвестное устройство"


def login_code(
    *, name: str | None, code: str, minutes: int, device: str | None, ip: str | None
) -> RenderedEmail:
    """Второй фактор по почте (ТЗ §3): код для входа с нового устройства."""
    where = _device_line(device, ip)
    subject = f"Код для входа в {BRAND}: {code}"
    text = (
        f"{_greeting(name)}\n\n"
        f"Код для входа: {code}\nДействует {minutes} минут.\n"
        f"Вход с устройства: {where}.\n\n"
        "Если это не вы — кто-то знает ваш пароль: смените его в настройках "
        f"{BRAND} и включите приложение-аутентификатор."
    )
    html = _layout(
        subject,
        [
            _p(escape(_greeting(name))),
            _p("Код для входа:"),
            _code(code),
            _muted(f"Действует {minutes} минут. Вход с устройства: {escape(where)}."),
            _muted(
                "Если это не вы — кто-то знает ваш пароль: смените его в "
                f"настройках {BRAND} и включите приложение-аутентификатор."
            ),
        ],
    )
    return RenderedEmail("login_code", subject, text, html)


def new_device_login(
    *, name: str | None, device: str | None, ip: str | None, reset_url: str
) -> RenderedEmail:
    """Вход с нового устройства вторым фактором не из почты (ТЗ §3)."""
    where = _device_line(device, ip)
    subject = f"Вход в {BRAND} с нового устройства"
    text = (
        f"{_greeting(name)}\n\n"
        f"В вашу учётную запись {BRAND} вошли с нового устройства: {where}.\n"
        f"Если это не вы, сразу смените пароль: {reset_url}"
    )
    html = _layout(
        subject,
        [
            _p(escape(_greeting(name))),
            _p(
                f"В вашу учётную запись {BRAND} вошли с нового устройства: "
                f"{escape(where)}."
            ),
            _p("Если это не вы, сразу смените пароль:"),
            _button("Сменить пароль", reset_url),
        ],
    )
    return RenderedEmail("new_device_login", subject, text, html)


def security_changed(*, name: str | None, what: str, reset_url: str) -> RenderedEmail:
    """Изменился второй фактор: включён, выключен, ключ добавлен или удалён."""
    subject = f"Защита учётной записи {BRAND} изменена"
    text = (
        f"{_greeting(name)}\n\n{what}\n\n"
        f"Если это были не вы, сразу смените пароль: {reset_url}"
    )
    html = _layout(
        subject,
        [
            _p(escape(_greeting(name))),
            _p(escape(what)),
            _p("Если это были не вы, сразу смените пароль:"),
            _button("Сменить пароль", reset_url),
        ],
    )
    return RenderedEmail("security_changed", subject, text, html)


def notice(
    *,
    kind: str,
    name: str | None,
    title: str,
    lines: list[str],
    url: str,
    action: str,
    settings_url: str,
) -> RenderedEmail:
    """Уведомление администратору (ТЗ §8): подключение, лимит, заявка на
    вступление, недельная сводка. Внизу — где выключить такие письма."""
    text = (
        f"{_greeting(name)}\n\n" + "\n".join(lines) + f"\n\n{action}: {url}\n\n"
        f"Какие письма присылать — {settings_url}"
    )
    html = _layout(
        title,
        [
            _p(escape(_greeting(name))),
            *(_p(escape(line)) for line in lines),
            _button(action, url),
            _muted(
                "Какие письма присылать — "
                f'<a href="{escape(settings_url, quote=True)}" '
                f'style="color:{_MUTED};">в настройках</a>.'
            ),
        ],
    )
    return RenderedEmail(f"notice_{kind}", title, text, html)


_WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def lead_received(
    *,
    company: str,
    contact: str,
    phone: str,
    email: str | None,
    seats: int,
    tariff: str,
    preferred_date: date,
    preferred_slot: str,
    comment: str | None,
) -> RenderedEmail:
    """Новая заявка на созвон — команде kronto (LEADS_NOTIFY_EMAIL).

    В отличие от сообщения в Telegram, здесь контакты: письмо уходит
    через наш почтовый сервис в ящик команды, а текст в очереди
    стирается после отправки (OutboxEmail).
    """
    when = (
        f"{preferred_date:%d.%m} ({_WEEKDAYS[preferred_date.weekday()]}), "
        f"{preferred_slot} по Москве"
    )
    subject = f"Заявка на созвон: {company}, {preferred_date:%d.%m} {preferred_slot}"
    rows = [
        ("Компания", company),
        ("Контакт", contact),
        ("Телефон", phone),
        ("Почта", email or "—"),
        ("Сотрудников за компьютером", str(seats)),
        ("Тариф", tariff),
        ("Удобно", when),
        ("Комментарий", comment or "—"),
    ]
    text = (
        "Новая заявка на созвон с сайта.\n\n"
        + "\n".join(f"{label}: {value}" for label, value in rows)
        + "\n\nПерезвоните, чтобы подтвердить время. Все заявки — cli leads list."
    )
    cell = "padding:6px 12px 6px 0;vertical-align:top;"
    table = "".join(
        f'<tr><td style="{cell}color:{_MUTED};white-space:nowrap;">'
        f'{escape(label)}</td><td style="{cell}">{escape(value)}</td></tr>'
        for label, value in rows
    )
    html = _layout(
        subject,
        [
            _p("Новая заявка на созвон с сайта."),
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="margin:0 0 16px;font-size:15px;">{table}</table>',
            _muted(
                "Перезвоните, чтобы подтвердить время. Все заявки — cli leads list."
            ),
        ],
    )
    return RenderedEmail("lead_received", subject, text, html)
