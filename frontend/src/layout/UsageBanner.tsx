import { useQuery } from "@tanstack/react-query";
import { Link } from "react-router";

import { api, unwrap } from "../api/client";
import { formatDate } from "../lib/format";
import { Notice } from "../ui/Notice";
import styles from "./AppShell.module.css";

const FIVE_MINUTES = 5 * 60_000;

/**
 * Плашка администратору на всех экранах, когда пул кредитов подходит к
 * концу (досье 10.2, решение 28.09). Порог — из ответа API (`warning`),
 * тот же, что у события аудита: фронт его не знает и не дублирует.
 * Сотрудники при исчерпании пула видят своё сообщение в чате (402).
 */
export function UsageBanner() {
  const usage = useQuery({
    queryKey: ["usage"],
    queryFn: () => unwrap(api.GET("/api/v1/usage")),
    refetchInterval: FIVE_MINUTES,
  });
  if (!usage.data?.warning) return null;
  const { exhausted, used, pool, period_end } = usage.data;
  const share = pool > 0 ? Math.min(100, Math.round((used / pool) * 100)) : 100;
  return (
    <div className={styles.banner}>
      {exhausted ? (
        <Notice kind="error" title="Лимит вопросов исчерпан">
          Сотрудники не смогут задавать вопросы до {formatDate(period_end)}.{" "}
          <Link to="/admin/usage">Подробнее о лимите</Link>
        </Notice>
      ) : (
        <Notice kind="warn" title="Лимит вопросов скоро закончится">
          Израсходовано {share} % лимита на месяц. <Link to="/admin/usage">Подробнее</Link>
        </Notice>
      )}
    </div>
  );
}
