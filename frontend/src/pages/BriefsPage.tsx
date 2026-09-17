import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link, useNavigate } from "react-router-dom";

import { errorMessage, fieldErrorsOf, isForbidden } from "../api/ApiError";
import {
  createBrief,
  generateProgram,
  listBriefs,
  listPrograms,
} from "../api/endpoints";
import {
  TRACK_LABELS,
  type Brief,
  type ProgramListItem,
  type Track,
} from "../api/types";
import { useToken } from "../auth/AuthContext";
import { Button } from "../components/Button";
import { SelectField, TextArea, TextField } from "../components/Field";
import { Notice } from "../components/Notice";
import { PageLoader } from "../components/PageLoader";
import { formatDate } from "../format";
import styles from "./BriefsPage.module.css";

export function BriefsPage() {
  const token = useToken();
  const navigate = useNavigate();

  const [briefs, setBriefs] = useState<Brief[] | null>(null);
  const [programs, setPrograms] = useState<ProgramListItem[]>([]);
  const [listError, setListError] = useState<unknown>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [track, setTrack] = useState<Track>("marketing");
  const [roleTitle, setRoleTitle] = useState("");
  const [goals, setGoals] = useState("");
  const [tasks, setTasks] = useState("");
  const [internLevel, setInternLevel] = useState("");
  const [formError, setFormError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);

  const [generatingId, setGeneratingId] = useState<string | null>(null);
  const [generateError, setGenerateError] = useState<unknown>(null);

  const reload = useCallback(async () => {
    try {
      const [loadedBriefs, loadedPrograms] = await Promise.all([
        listBriefs(token),
        listPrograms(token),
      ]);
      setBriefs(loadedBriefs);
      setPrograms(loadedPrograms);
      setListError(null);
    } catch (caught) {
      setListError(caught);
    }
  }, [token]);

  useEffect(() => {
    void reload();
  }, [reload]);

  async function handleCreate(event: FormEvent) {
    event.preventDefault();
    setFormError(null);
    setSaving(true);

    try {
      await createBrief(token, {
        track,
        role_title: roleTitle,
        goals,
        tasks,
        intern_level: internLevel,
      });
      setRoleTitle("");
      setGoals("");
      setTasks("");
      setInternLevel("");
      setFormOpen(false);
      await reload();
    } catch (caught) {
      setFormError(caught);
    } finally {
      setSaving(false);
    }
  }

  async function handleGenerate(briefId: string) {
    setGenerateError(null);
    setGeneratingId(briefId);

    try {
      const program = await generateProgram(token, briefId);
      navigate(`/programs/${program.id}`);
    } catch (caught) {
      setGenerateError(caught);
      setGeneratingId(null);
    }
  }

  if (isForbidden(listError)) {
    return (
      <div className={styles.page}>
        <h1 className="title">Программы</h1>
        <Notice tone="error">{errorMessage(listError)}</Notice>
        <p>
          <Link to="/chat">Вернуться к вопросам</Link>
        </p>
      </div>
    );
  }

  const fieldErrors = fieldErrorsOf(formError);
  const programByBrief = new Map(
    programs.map((program) => [program.brief_id, program]),
  );

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div className={styles.headText}>
          <h1 className="title">Программы</h1>
          <p className="subtitle">
            Бриф руководителя — вход для программы адаптации. Чем конкретнее
            цели и задачи, тем меньше программа похожа на шаблон.
          </p>
        </div>
        <Button
          variant={formOpen ? "secondary" : "primary"}
          onClick={() => setFormOpen((open) => !open)}
        >
          {formOpen ? "Отмена" : "Новый бриф"}
        </Button>
      </header>

      {listError ? (
        <Notice tone="error">{errorMessage(listError)}</Notice>
      ) : null}
      {generateError ? (
        <Notice tone="error">{errorMessage(generateError)}</Notice>
      ) : null}
      {generatingId ? (
        <Notice tone="info">
          Собираем программу по брифу. Это занимает до минуты — страницу можно
          не закрывать.
        </Notice>
      ) : null}

      {formOpen ? (
        <form
          className={`${styles.panel} ${styles.form}`}
          onSubmit={handleCreate}
          noValidate
        >
          {formError && Object.keys(fieldErrors).length === 0 ? (
            <Notice tone="error">{errorMessage(formError)}</Notice>
          ) : null}

          <div className={styles.formRow}>
            <SelectField
              label="Направление"
              value={track}
              error={fieldErrors.track}
              onChange={(event) => setTrack(event.target.value as Track)}
            >
              <option value="marketing">Маркетинг</option>
              <option value="analytics">Аналитика</option>
            </SelectField>

            <TextField
              label="Роль стажёра"
              value={roleTitle}
              error={fieldErrors.role_title}
              onChange={(event) => setRoleTitle(event.target.value)}
              placeholder="Стажёр-маркетолог"
              required
            />
          </div>

          <div className={styles.formPair}>
            <TextArea
              label="Цели"
              value={goals}
              error={fieldErrors.goals}
              onChange={(event) => setGoals(event.target.value)}
              placeholder="Что стажёр должен уметь к концу стажировки"
              rows={4}
              required
            />
            <TextArea
              label="Задачи"
              value={tasks}
              error={fieldErrors.tasks}
              onChange={(event) => setTasks(event.target.value)}
              placeholder="Чем он будет заниматься каждую неделю"
              rows={4}
              required
            />
          </div>

          <TextField
            label="Уровень"
            value={internLevel}
            error={fieldErrors.intern_level}
            onChange={(event) => setInternLevel(event.target.value)}
            placeholder="Junior, без коммерческого опыта"
            required
          />

          <div>
            <Button type="submit" loading={saving}>
              {saving ? "Сохраняем" : "Сохранить бриф"}
            </Button>
          </div>
        </form>
      ) : null}

      <section className={styles.panel}>
        <div className={styles.tableHead}>
          <span>Роль</span>
          <span>Направление</span>
          <span>Создан</span>
          <span />
        </div>

        {briefs === null && !listError ? (
          <PageLoader text="Загружаем брифы" />
        ) : null}

        {briefs?.length === 0 ? (
          <p className={styles.empty}>
            Брифов пока нет. Первый заполняется кнопкой сверху.
          </p>
        ) : null}

        {briefs?.map((brief) => {
          const program = programByBrief.get(brief.id);
          const generating = generatingId === brief.id;

          return (
            <article key={brief.id} className={styles.row}>
              <span className={styles.title}>{brief.role_title}</span>
              <span className={styles.cell}>{TRACK_LABELS[brief.track]}</span>
              <span className="meta">{formatDate(brief.created_at)}</span>

              <div className={styles.action}>
                {program ? (
                  <Link className={styles.link} to={`/programs/${program.id}`}>
                    Открыть программу
                  </Link>
                ) : null}
                <Button
                  variant="quiet"
                  loading={generating}
                  disabled={generatingId !== null}
                  onClick={() => void handleGenerate(brief.id)}
                >
                  {generating
                    ? "Собираем"
                    : program
                      ? "Заново"
                      : "Собрать программу"}
                </Button>
              </div>
            </article>
          );
        })}
      </section>
    </div>
  );
}
