import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Link } from "react-router";

import { api, unwrap } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { Button } from "../ui/Button";
import styles from "./Chat.module.css";

const TOPUP_KEY = ["credits", "topup"] as const;

/**
 * Кредиты компании кончились (402, решение владельца 09.10). Администратору —
 * куда идти за пакетом; сотруднику — «Попросить администратора пополнить»:
 * администраторам уходит одно уведомление на весь эпизод, остальные видят
 * «Администратор уже уведомлён».
 */
export function CreditsStopped({ admin }: { admin: boolean }) {
  return (
    <div className={`${styles.msg} ${styles.limit}`} role="alert">
      <p className={styles.refusalTitle}>Кредиты компании на этот месяц закончились.</p>
      {admin ? (
        <p>
          Купите пакет кредитов или добавьте места — в разделе{" "}
          <Link to="/admin/tariff">«Тариф»</Link>.
        </p>
      ) : (
        <>
          <p>
            Вопросы снова станут доступны, когда администратор пополнит кредиты или начнётся новый
            месяц.
          </p>
          <TopupRequest />
        </>
      )}
    </div>
  );
}

function TopupRequest() {
  const queryClient = useQueryClient();
  const status = useQuery({
    queryKey: TOPUP_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/credits/topup-request")),
  });
  const press = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/credits/topup-request")),
    onSuccess: () => queryClient.setQueryData(TOPUP_KEY, { stopped: true, requested: true }),
  });
  if (press.data) {
    return <p>{press.data.sent ? "Администратор уведомлён." : "Администратор уже уведомлён."}</p>;
  }
  if (press.error instanceof ApiError && press.error.code === "credits_available") {
    return <p>Кредиты снова есть — можно задать вопрос.</p>;
  }
  if (!status.data?.stopped) return null;
  if (status.data.requested) return <p>Администратор уже уведомлён.</p>;
  return (
    <>
      {press.isError ? <p>{errorMessage(press.error)}</p> : null}
      <Button size="sm" busy={press.isPending} onClick={() => press.mutate()}>
        Попросить администратора пополнить
      </Button>
    </>
  );
}
