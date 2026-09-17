import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { errorMessage } from "../api/ApiError";
import { fetchProgram } from "../api/endpoints";
import type { ProgramDetail } from "../api/types";
import { useToken } from "../auth/AuthContext";
import { Notice } from "../components/Notice";
import { PageLoader } from "../components/PageLoader";
import { formatDate } from "../format";
import styles from "./ProgramPage.module.css";

const STATUS_LABELS = {
  draft: "Черновик",
  approved: "Утверждена",
} as const;

export function ProgramPage() {
  const token = useToken();
  const { programId } = useParams<{ programId: string }>();

  const [program, setProgram] = useState<ProgramDetail | null>(null);
  const [error, setError] = useState<unknown>(null);

  useEffect(() => {
    if (!programId) {
      return;
    }
    let cancelled = false;

    fetchProgram(token, programId)
      .then((loaded) => {
        if (!cancelled) {
          setProgram(loaded);
          setError(null);
        }
      })
      .catch((caught: unknown) => {
        if (!cancelled) {
          setError(caught);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [token, programId]);

  if (error) {
    return (
      <div className={styles.page}>
        <Notice tone="error">{errorMessage(error)}</Notice>
        <Link className={styles.back} to="/briefs">
          ← К брифам
        </Link>
      </div>
    );
  }

  if (!program) {
    return <PageLoader text="Загружаем программу" />;
  }

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <Link className={styles.back} to="/briefs">
          ← К брифам
        </Link>
        <p className="eyebrow">Программа адаптации</p>
        <h1 className="section-title">30 / 60 / 90 дней</h1>
        <div className={styles.meta}>
          <span className={styles.status}>{STATUS_LABELS[program.status]}</span>
          <span>Создана {formatDate(program.created_at)}</span>
        </div>
      </header>

      <article className={styles.sheet}>
        <div className={styles.content}>{program.content}</div>
      </article>
    </div>
  );
}
