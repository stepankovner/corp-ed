import { useQuery } from "@tanstack/react-query";
import { Link, useLocation } from "react-router";

import { api, unwrap } from "../api/client";
import { formatCalendarDate, formatNumber } from "../lib/format";
import { Notice } from "../ui/Notice";
import styles from "./AppShell.module.css";

const FIVE_MINUTES = 5 * 60_000;
const BUY_OR_ADD = "Купите пакет кредитов или добавьте места.";

/**
 * Плашка администратору на всех экранах, когда кредиты подходят к концу
 * (досье 10.2, решения 28.09 и 09.10). Порог — из ответа API (`warning`),
 * тот же, что у события аудита: фронт его не знает и не дублирует. Пул
 * израсходован, но есть купленные кредиты, — предупреждение, а не
 * остановка. Сотрудники при остановке видят своё сообщение в чате (402).
 * На странице «Тариф» то же сказано в карточке кредитов — там плашки нет.
 */
export function UsageBanner() {
  const { pathname } = useLocation();
  const usage = useQuery({
    queryKey: ["usage"],
    queryFn: () => unwrap(api.GET("/api/v1/usage")),
    refetchInterval: FIVE_MINUTES,
  });
  if (!usage.data?.warning || pathname.startsWith("/admin/tariff")) return null;
  const { stopped, exhausted, used, pool, purchased, period_end } = usage.data;
  const share = pool > 0 ? Math.min(100, Math.round((used / pool) * 100)) : 100;
  return (
    <div className={styles.banner}>
      {stopped ? (
        <Notice kind="error" title="Кредиты закончились">
          Сотрудники не смогут задавать вопросы до {formatCalendarDate(period_end)}. {BUY_OR_ADD}{" "}
          <Link to="/admin/tariff">Купить пакет</Link>
        </Notice>
      ) : exhausted ? (
        <Notice kind="warn" title="Месячный пул кредитов израсходован">
          Вопросы списываются с купленных кредитов: осталось {formatNumber(purchased)}.{" "}
          <Link to="/admin/tariff">Подробнее</Link>
        </Notice>
      ) : (
        <Notice kind="warn" title="Кредиты скоро закончатся">
          Израсходовано {share} % месячного пула. {BUY_OR_ADD}{" "}
          <Link to="/admin/tariff">Подробнее</Link>
        </Notice>
      )}
    </div>
  );
}
