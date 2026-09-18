import { useEffect, useState } from "react";

import { ApiError, errorMessage } from "../api/ApiError";
import { fetchMyProgram } from "../api/endpoints";
import { TRACK_LABELS, type ProgramDetail } from "../api/types";
import { useToken } from "../auth/AuthContext";
import { Chat } from "../components/Chat";
import { Notice } from "../components/Notice";
import { PageLoader } from "../components/PageLoader";
import { formatDate } from "../format";
import styles from "./InternPage.module.css";

type State =
  | { kind: "loading" }
  | { kind: "ready"; program: ProgramDetail }
  | { kind: "waiting" }
  | { kind: "error"; error: unknown };

export function InternPage() {
  const token = useToken();
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    let cancelled = false;

    fetchMyProgram(token)
      .then((program) => {
        if (!cancelled) {
          setState({ kind: "ready", program });
        }
      })
      .catch((caught: unknown) => {
        if (cancelled) {
          return;
        }
        // 404 здесь — не сбой: руководитель ещё не открыл программу.
        if (caught instanceof ApiError && caught.status === 404) {
          setState({ kind: "waiting" });
          return;
        }
        setState({ kind: "error", error: caught });
      });

    return () => {
      cancelled = true;
    };
  }, [token]);

  return (
    <div className={styles.page}>
      <section className={styles.column}>
        <header className={styles.head}>
          <h1 className="h1">Моя программа адаптации</h1>
          {state.kind === "ready" ? (
            <div className={styles.meta}>
              <span className="label">{state.program.role_title}</span>
              <span className="label">
                трек {TRACK_LABELS[state.program.track]}
              </span>
              <span className={styles.tag}>
                создана {formatDate(state.program.created_at)}
              </span>
            </div>
          ) : null}
        </header>

        {state.kind === "loading" ? (
          <PageLoader text="Загружаем программу" />
        ) : null}

        {state.kind === "waiting" ? (
          <div className={`card ${styles.waiting}`}>
            <span className="label">Программа готовится</span>
            <p className="lead">
              Руководитель ещё не открыл вам программу адаптации. Как только
              он это сделает, она появится здесь. Вопросы по работе отдела
              можно задавать уже сейчас.
            </p>
          </div>
        ) : null}

        {state.kind === "error" ? (
          <Notice tone="error">{errorMessage(state.error)}</Notice>
        ) : null}

        {state.kind === "ready" ? (
          <article className={`card ${styles.sheet}`}>
            <div className={styles.content}>{state.program.content}</div>
          </article>
        ) : null}
      </section>

      <section className={styles.column}>
        <header className={styles.head}>
          <h2 className="h1">Чат по материалам</h2>
          <p className="lead">
            Отвечает только по документам вашего отдела. Если ответа в них
            нет — так и скажет.
          </p>
        </header>

        <Chat />
      </section>
    </div>
  );
}
