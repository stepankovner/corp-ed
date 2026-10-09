"""Проверка самой обвязки (не тест продукта): компания, вход с
приложением, загрузка и индексация, ответ по документу, письмо с кодом."""

import re

from tests.blackbox.conftest import API, Kronto


async def test_harness_end_to_end(kronto: Kronto) -> None:
    await kronto.create_company(
        code="acme",
        name="ACME",
        admin_email="boss@acme.ru",
        admin_password="Temp-pass-123456",
    )
    secret = await kronto.enable_totp("boss@acme.ru")
    browser = kronto.browser()

    first = await browser.post(
        f"{API}/auth/login",
        json={
            "email": "boss@acme.ru",
            "password": "Temp-pass-123456",
            "remember": True,
        },
    )
    assert first.status_code == 200, first.text
    body = first.json()
    if body["status"] != "ok":
        verify = await browser.post(
            f"{API}/auth/mfa/verify",
            json={
                "token": body["mfa"]["token"],
                "method": "totp",
                "code": kronto.totp(secret),
            },
        )
        assert verify.status_code == 200, verify.text
        body = verify.json()
    token = body["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    changed = await browser.post(
        f"{API}/auth/change-password",
        json={
            "current_password": "Temp-pass-123456",
            "new_password": "Brand-new-pass-987654",
        },
        headers=headers,
    )
    assert changed.status_code in (200, 204), changed.text
    if changed.status_code == 200 and "access_token" in changed.json():
        headers = {"Authorization": f"Bearer {changed.json()['access_token']}"}

    upload = await browser.post(
        f"{API}/materials/upload",
        files={
            "file": (
                "guide.md",
                "# Парковка\n\nПарковка для гостей на минус втором этаже.".encode(),
                "text/markdown",
            )
        },
        data={"title": "Парковка"},
        headers=headers,
    )
    assert upload.status_code in (200, 201), upload.text
    await kronto.run_background()
    ask = await browser.post(
        f"{API}/faq/ask", json={"question": "Где парковка для гостей?"}, headers=headers
    )
    assert ask.status_code == 200, ask.text
    assert "минус втором этаже" in ask.json()["content"]

    register = await kronto.browser().post(
        f"{API}/auth/register",
        json={
            "email": "anna@example.ru",
            "password": "Kh7-velvet-orbit-2026",
            "first_name": "Анна",
            "last_name": "Иванова",
            "terms": True,
            "consent": True,
        },
    )
    assert register.status_code in (200, 201, 202), register.text
    letter = await kronto.last_letter("anna@example.ru")
    assert re.search(r"\d{6}", letter.text)
