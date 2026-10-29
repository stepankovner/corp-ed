import { Link } from "react-router";

const CONNECTIONS = "/admin/sources/connections";
const MINE = "/settings/connections";

/** Где в kronto добавляется подключение — одинаково для всех систем. */
export function AddConnection({ system }: { system: string }) {
  return (
    <>
      <Link to={CONNECTIONS}>Управление → Источники → Подключения</Link> → «Добавить подключение» →
      «{system}»
    </>
  );
}

/**
 * Сотрудник подключает свой аккаунт — одинаково для всех систем режима
 * «каждый сам». account — система называет учётку («Аккаунт: …»).
 */
export function EmployeesConnect({ how, account = true }: { how: string; account?: boolean }) {
  return (
    <p>
      <strong>Сотрудники.</strong> Каждый открывает{" "}
      <Link to={MINE}>Настройки → Мои подключения</Link> и {how}.
      {account
        ? " Под названием системы появится «Аккаунт: …» — проверьте, что это рабочая учётка, а не личная."
        : null}{" "}
      kronto сразу начнёт забирать документы; в ответах они появятся через несколько минут.
    </p>
  );
}
