import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";

import { errorMessage, isForbidden } from "../api/ApiError";
import {
  approveProgram,
  fetchProgram,
  listInterns,
  listPrograms,
  updateProgram,
} from "../api/endpoints";
import {
  STATUS_LABELS,
  TRACK_LABELS,
  type Intern,
  type ProgramDetail,
  type ProgramListItem,
} from "../api/types";
import { useToken } from "../auth/AuthContext";
import { Button } from "../components/Button";
import { SelectField } from "../components/Field";
import { Notice } from "../components/Notice";
import { PageLoader } from "../components/PageLoader";
import { formatDate } from "../format";
import styles from "./ProgramPage.module.css";

export function ProgramPage() {
  const token = useToken();
  const navigate = useNavigate();
  const { programId } = useParams<{ programId: string }>();

  const [programs, setPrograms] = useState<ProgramListItem[] | null>(null);
  const [interns, setInterns] = useState<Intern[]>([]);
  const [program, setProgram] = useState<ProgramDetail | null>(null);
  const [error, setError] = useState<unknown>(null);

  const [editing, setEditing] = useState(false);
  const [draftText, setDraftText] = useState("");
  const [internId, setInternId] = useState("");
  const [saving, setSaving] = useState(false);
  const [approving, setApproving] = useState(false);

  const reloadList = useCallback(async () => {
    try {
      const [loadedPrograms, loadedInterns] = await Promise.all([
        listPrograms(token),
        listInterns(token),
      ]);
      setPrograms(loadedPrograms);
      setInterns(loadedInterns);
      return loadedPrograms;
    } catch (caught) {
      setError(caught);
      return null;
    }
  }, [token]);

  useEffect(() => {
    void reloadList();
  }, [reloadList]);

  // Без явного адреса открывается самая свежая программа: список в правой
  // колонке уже загружен, и показывать пустой экран незачем.
  useEffect(() => {
    if (programId || programs === null || programs.length === 0) {
      return;
    }
    navigate(`/programs/${programs[0].id}`, { replace: true });
  }, [programId, programs, navigate]);

  useEffect(() => {
    if (!programId) {
      setProgram(null);
      return;
    }
    let cancelled = false;

    fetchProgram(token, programId)
      .then((loaded) => {
        if (!cancelled) {
          setProgram(loaded);
          setDraftText(loaded.content);
          setInternId(loaded.intern_id ?? "");
          setEditing(false);
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

  async function handleSaveText() {
    if (!program) {
      return;
    }
    setSaving(true);
    setError(null);

    try {
      const updated = await updateProgram(token, program.id, {
        content: draftText,
      });
      setProgram(updated);
      setEditing(false);
    } catch (caught) {
      setError(caught);
    } finally {
      setSaving(false);
    }
  }

  async function handleApprove() {
    if (!program || !internId) {
      return;
    }
    setApproving(true);
    setError(null);

    try {
      // Назначение и утверждение — два запроса: стажёра можно менять
      // и до утверждения, а утверждение уже необратимо.
      if (internId !== program.intern_id) {
        await updateProgram(token, program.id, { intern_id: internId });
      }
      const approved = await approveProgram(token, program.id);
      setProgram(approved);
      await reloadList();
    } catch (caught) {
      setError(caught);
    } finally {
      setApproving(false);
    }
  }

  if (isForbidden(error)) {
    return (
      <div className={styles.page}>
        <h1 className="h1">Программы</h1>
        <Notice tone="error">{errorMessage(error)}</Notice>
        <p>
          <Link to="/my">Вернуться на страницу стажировки</Link>
        </p>
      </div>
    );
  }

  if (programs !== null && programs.length === 0) {
    return (
      <div className={styles.page}>
        <h1 className="h1">Программы</h1>
        <section className={`card ${styles.emptyState}`}>
          <p className="lead">
            Программ пока нет. Заполните бриф — по нему и материалам трека
            соберётся программа адаптации.
          </p>
          <Link to="/programs/new">
            <Button>Заполнить бриф</Button>
          </Link>
        </section>
      </div>
    );
  }

  const internName = (id: string | null) => {
    const intern = interns.find((candidate) => candidate.id === id);
    return intern?.full_name ?? intern?.email ?? "стажёр";
  };

  return (
    <div className={styles.page}>
      {error ? <Notice tone="error">{errorMessage(error)}</Notice> : null}

      <div className={styles.columns}>
        <section className="stack gap-24">
          {!program ? (
            <PageLoader text="Загружаем программу" />
          ) : (
            <>
              <header className={styles.head}>
                <h1 className="h1">
                  Программа адаптации · {program.role_title}
                </h1>
                <div className={styles.meta}>
                  <span className="label">
                    трек {TRACK_LABELS[program.track]}
                  </span>
                  <span className="label">
                    создана {formatDate(program.created_at)}
                  </span>
                  <span
                    className={`${styles.tag} ${
                      program.status === "draft" ? styles.draftTag : ""
                    }`}
                  >
                    {STATUS_LABELS[program.status]}
                  </span>
                </div>
              </header>

              {program.status === "draft" ? (
                <div className={styles.banner}>
                  <span className="label">Черновик</span>
                  <span className={styles.bannerText}>
                    Стажёр пока не видит эту программу. Проверьте текст,
                    выберите стажёра и откройте ему доступ.
                  </span>
                </div>
              ) : (
                <div className={styles.banner}>
                  <span className="label">Открыта стажёру</span>
                  <span className={styles.bannerText}>
                    {internName(program.intern_id)} видит эту программу на
                    своей странице. Текст закрыт от правок.
                  </span>
                </div>
              )}

              {program.status === "draft" && !editing ? (
                <section className={`card ${styles.approval}`}>
                  <h2 className="h2">Открыть программу стажёру</h2>

                  <div className={styles.approvalRow}>
                    <SelectField
                      label="Стажёр"
                      value={internId}
                      onChange={(event) => setInternId(event.target.value)}
                      hint={
                        interns.length === 0
                          ? "В компании пока нет стажёров"
                          : undefined
                      }
                    >
                      <option value="">Выберите стажёра</option>
                      {interns.map((intern) => (
                        <option key={intern.id} value={intern.id}>
                          {intern.full_name ?? intern.email}
                        </option>
                      ))}
                    </SelectField>

                    <Button
                      variant="accent"
                      loading={approving}
                      disabled={!internId}
                      onClick={() => void handleApprove()}
                    >
                      {approving ? "Открываем" : "Утвердить и открыть стажёру"}
                    </Button>
                  </div>

                  <div className={styles.editRow}>
                    <Button
                      variant="secondary"
                      onClick={() => setEditing(true)}
                    >
                      Редактировать текст
                    </Button>
                    <span className="mono">
                      после утверждения текст менять нельзя
                    </span>
                  </div>
                </section>
              ) : null}
              {editing ? (
                <div className="stack gap-16">
                  <textarea
                    className={styles.editor}
                    value={draftText}
                    onChange={(event) => setDraftText(event.target.value)}
                    aria-label="Текст программы"
                  />
                  <div className={styles.editRow}>
                    <Button loading={saving} onClick={() => void handleSaveText()}>
                      {saving ? "Сохраняем" : "Сохранить текст"}
                    </Button>
                    <Button
                      variant="secondary"
                      onClick={() => {
                        setDraftText(program.content);
                        setEditing(false);
                      }}
                    >
                      Отмена
                    </Button>
                  </div>
                </div>
              ) : (
                <article className={styles.content}>{program.content}</article>
              )}

            </>
          )}
        </section>

        <aside className={styles.rail}>
          <span className="label">Созданные программы</span>

          {programs?.map((item) => (
            <Link
              key={item.id}
              to={`/programs/${item.id}`}
              className={`${styles.railItem} ${
                item.id === program?.id ? styles.railCurrent : ""
              }`}
            >
              <span className={styles.railTitle}>{item.role_title}</span>
              <div className="mono">
                {formatDate(item.created_at)} · {STATUS_LABELS[item.status]}
              </div>
            </Link>
          ))}

          <Link to="/programs/new">
            <Button variant="secondary">Новый бриф</Button>
          </Link>
        </aside>
      </div>
    </div>
  );
}
