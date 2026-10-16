"""Сверка выгрузки «кто что видит» с манифестом — без базы и без системы.

Выгрузку делает на сервере kronto команда `connector-visibility` (когда
подключение настоящее и база на стенде, а не здесь). Сверка берёт её JSON,
строит ожидания для начального состояния системы из манифеста и зовёт ту же
`compare()`, что и сценарий приёмки: расхождения называются теми же словами
(«УТЕЧКА: … видит …, а не должен»).

    python -m tests.acceptance.verify --system yandex360 \\
        --observed visibility.json [--connector <uuid>] [--domain krontoai.ru]

Почты сотрудников — `<логин>@<домен>`. Тот, кого нет среди учётных записей
системы в манифесте, игнорируется с предупреждением (например, администратор
компании на стенде); тот, кто есть в манифесте, но не в выгрузке, —
расхождение. Код возврата: 0 — совпало, 1 — расхождения, 2 — выгрузку или
параметры не удалось разобрать.
"""

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tests.acceptance.model import Expected, Manifest, System, load_manifest
from tests.acceptance.scenario import Observation, compare

DEFAULT_DOMAIN = "krontoai.ru"


class VerifyInputError(ValueError):
    """Выгрузку или параметры нельзя использовать."""


@dataclass
class Verdict:
    problems: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems


def pick_connector(report: dict[str, Any], system: System, wanted: str | None) -> Any:
    """Подключение из выгрузки: по id, а без него — единственное такого же
    вида, как система (иначе сверка смешала бы разные подключения)."""
    connectors = report.get("connectors")
    if not isinstance(connectors, list):
        raise VerifyInputError("в выгрузке нет списка connectors")
    if wanted is not None:
        for item in connectors:
            if str(item.get("id")) == wanted:
                return item
        raise VerifyInputError(f"в выгрузке нет подключения {wanted}")
    of_kind = [c for c in connectors if c.get("kind") == system.kind]
    if len(of_kind) == 1:
        return of_kind[0]
    ids = ", ".join(f"{c.get('id')} ({c.get('kind')})" for c in connectors)
    if not of_kind:
        raise VerifyInputError(
            f"в выгрузке нет подключения вида {system.kind}; есть: {ids or 'нет'}"
        )
    raise VerifyInputError(
        f"подключений вида {system.kind} несколько, укажите --connector: {ids}"
    )


def verify(
    manifest: Manifest,
    system: System,
    report: dict[str, Any],
    *,
    connector_id: str | None = None,
    domain: str = DEFAULT_DOMAIN,
) -> Verdict:
    connector = pick_connector(report, system, connector_id)
    verdict = Verdict()
    suffix = "@" + domain.casefold()
    seen: dict[str, set[str]] = {}
    for email, codes in connector.get("seen", {}).items():
        local = email.casefold()
        login = local[: -len(suffix)] if local.endswith(suffix) else None
        if login is None or login not in system.accounts:
            verdict.warnings.append(
                f"{email}: нет среди сотрудников {system.id} в манифесте "
                f"(<логин>{suffix}), пропущен"
            )
            continue
        seen[login] = set(codes)
    logins = [login for login in system.accounts if login in seen]
    for login in system.accounts:
        if login not in seen:
            verdict.problems.append(
                f"{login}@{domain}: нет в выгрузке (не активный участник "
                "компании или другая почта)"
            )
    obs = Observation(
        contents={
            code: [""] * int(count)
            for code, count in connector.get("documents", {}).items()
        },
        seen=seen,
        without_code=int(connector.get("without_code", 0)),
    )
    verdict.problems += compare(Expected(manifest, system), obs, logins)
    return verdict


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="tests.acceptance.verify",
        description="сверить выгрузку connector-visibility с манифестом",
    )
    parser.add_argument("--system", required=True, help="id системы из манифеста")
    parser.add_argument(
        "--observed", required=True, help="JSON от connector-visibility"
    )
    parser.add_argument("--connector", help="id подключения в выгрузке")
    parser.add_argument(
        "--domain", default=DEFAULT_DOMAIN, help="домен почт сотрудников в kronto"
    )
    args = parser.parse_args(argv)
    manifest = load_manifest()
    system = manifest.systems.get(args.system)
    if system is None:
        print(
            f"нет системы {args.system}; в манифесте: {', '.join(manifest.systems)}",
            file=sys.stderr,
        )
        return 2
    try:
        report = json.loads(Path(args.observed).read_text(encoding="utf-8"))
        verdict = verify(
            manifest, system, report, connector_id=args.connector, domain=args.domain
        )
    except (OSError, json.JSONDecodeError, VerifyInputError, AttributeError) as exc:
        print(f"выгрузка не разобрана: {exc}", file=sys.stderr)
        return 2
    for warning in verdict.warnings:
        print(f"предупреждение: {warning}", file=sys.stderr)
    if verdict.ok:
        print("ok")
        return 0
    for problem in verdict.problems:
        print(problem)
    return 1


if __name__ == "__main__":
    sys.exit(main())
