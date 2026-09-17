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
        <Link className={styles.back} to="/briefs">
          ← Программы
        </Link>
        <Notice tone="error">{errorMessage(error)}</Notice>
      </div>
    );
  }

  if (!program) {
    return <PageLoader text="Загружаем программу" />;
  }

  return (
    <div className={styles.page}>
      <Link className={styles.back} to="/briefs">
        ← Программы
      </Link>

      <header className={styles.head}>
        <h1 className={styles.title}>Программа адаптации</h1>
        <span className="meta">
          {STATUS_LABELS[program.status]} · собрана{" "}
          {formatDate(program.created_at)}
        </span>
      </header>

      <article className={styles.sheet}>
        <div className={styles.content}>{program.content}</div>
      </article>
    </div>
  );
}
