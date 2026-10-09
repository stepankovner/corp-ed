"""Версии юридических текстов на сайте и на сервере совпадают.

Согласие пишется в учётку с версией из настроек сервера
(REGISTRATION_POLICY_VERSION, REGISTRATION_TERMS_VERSION), а текст
показывает сайт (frontend/src/site/legal/documents.ts). Сменили текст и
версию на сайте — значения по умолчанию на сервере меняются вместе.
"""

import re
from pathlib import Path

from corp_ed.core.config import RegistrationSettings

DOCUMENTS = Path(__file__).resolve().parents[1] / "frontend/src/site/legal/documents.ts"


def _site_versions() -> dict[str, str]:
    source = DOCUMENTS.read_text()
    return dict(
        re.findall(
            r"(\w+): \{\s*text: \w+,\s*edition: \w+,\s*version: \"([^\"]+)\"", source
        )
    )


def test_registration_versions_match_the_site() -> None:
    site = _site_versions()
    defaults = RegistrationSettings.model_fields
    assert site["consent"] == defaults["policy_version"].default
    assert site["terms"] == defaults["terms_version"].default
    assert site["consent"] != site["terms"]
