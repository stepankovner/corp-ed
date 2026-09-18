import { useEffect, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";

import { errorMessage, fieldErrorsOf } from "../api/ApiError";
import { createBrief, generateProgram } from "../api/endpoints";
import { TRACK_OPTIONS, type Track } from "../api/types";
import { useToken } from "../auth/AuthContext";
import { Button } from "../components/Button";
import { TextArea, TextField } from "../components/Field";
import { Notice } from "../components/Notice";
import { Segmented } from "../components/Segmented";
import styles from "./BriefPage.module.css";

const STEPS = [
  "Собрали бриф и материалы трека",
  "Подбираем фрагменты под цели и задачи",
  "Формулируем недельный план",
];

export function BriefPage() {
  const token = useToken();
  const navigate = useNavigate();

  const [track, setTrack] = useState<Track>("marketing");
  const [roleTitle, setRoleTitle] = useState("");
  const [goals, setGoals] = useState("");
  const [tasks, setTasks] = useState("");
  const [internLevel, setInternLevel] = useState("");

  const [briefId, setBriefId] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [generating, setGenerating] = useState(false);
  const [step, setStep] = useState(0);
  const [error, setError] = useState<unknown>(null);

  // Шаги — оценка по времени, а не отчёт сервера: генерация идёт одним
  // запросом и промежуточных ответов не даёт. Нужны, чтобы минута ожидания
  // не выглядела зависшим экраном.
  useEffect(() => {
    if (!generating) {
      setStep(0);
      return;
    }

    const timers = [
      window.setTimeout(() => setStep(1), 5_000),
      window.setTimeout(() => setStep(2), 20_000),
    ];

    return () => timers.forEach(window.clearTimeout);
  }, [generating]);

  async function handleCreate(event: FormEvent) {
    event.preventDefault();
    setError(null);
    setSaving(true);

    try {
      const brief = await createBrief(token, {
        track,
        role_title: roleTitle,
        goals,
        tasks,
        intern_level: internLevel,
      });
      setBriefId(brief.id);
    } catch (caught) {
      setError(caught);
    } finally {
      setSaving(false);
    }
  }

  async function handleGenerate() {
    if (!briefId) {
      return;
    }

    setError(null);
    setGenerating(true);

    try {
      const program = await generateProgram(token, briefId);
      navigate(`/programs/${program.id}`);
    } catch (caught) {
      setError(caught);
      setGenerating(false);
    }
  }

  const fieldErrors = fieldErrorsOf(error);

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <h1 className="h1">Бриф стажировки</h1>
        <p className="lead">
          Бриф задаёт рамку: по нему и материалам трека собирается программа
          адаптации.
        </p>
      </header>

      <div className={styles.columns}>
        <form
          className={`card ${styles.form}`}
          onSubmit={handleCreate}
          noValidate
        >
          {error && Object.keys(fieldErrors).length === 0 ? (
            <Notice tone="error">{errorMessage(error)}</Notice>
          ) : null}

          <Segmented
            label="Трек"
            value={track}
            options={TRACK_OPTIONS}
            onChange={setTrack}
          />

          <TextField
            label="Должность стажёра"
            value={roleTitle}
            error={fieldErrors.role_title}
            onChange={(event) => setRoleTitle(event.target.value)}
            placeholder="Стажёр-маркетолог"
            disabled={briefId !== null}
            required
          />

          <TextArea
            label="Цели"
            value={goals}
            error={fieldErrors.goals}
            onChange={(event) => setGoals(event.target.value)}
            placeholder="Что стажёр должен уметь к концу стажировки"
            disabled={briefId !== null}
            rows={2}
            required
          />

          <TextArea
            label="Задачи"
            value={tasks}
            error={fieldErrors.tasks}
            onChange={(event) => setTasks(event.target.value)}
            placeholder="Чем он будет заниматься на практике"
            disabled={briefId !== null}
            rows={2}
            required
          />

          <TextField
            label="Уровень стажёра"
            value={internLevel}
            error={fieldErrors.intern_level}
            onChange={(event) => setInternLevel(event.target.value)}
            placeholder="3 курс, опыт учебных проектов"
            disabled={briefId !== null}
            required
          />

          <div className={styles.actions}>
            <Button type="submit" loading={saving} disabled={briefId !== null}>
              {saving ? "Сохраняем" : "Создать бриф"}
            </Button>

            {briefId ? <span className="label">Бриф сохранён</span> : null}

            <Button
              type="button"
              variant="accent"
              disabled={briefId === null}
              loading={generating}
              onClick={() => void handleGenerate()}
            >
              {generating ? "Генерируем" : "Сгенерировать программу"}
            </Button>
          </div>
        </form>

        <aside className={styles.rail}>
          {generating ? (
            <>
              <span className="label">
                Генерация · шаг {step + 1} из {STEPS.length}
              </span>
              <p className={styles.railTitle}>
                Генерируем программу адаптации, это займёт до минуты
              </p>
              <p className="lead">
                Можно не ждать на этом экране — программа появится в списке,
                когда будет готова.
              </p>

              <div className={styles.progress}>
                <div
                  className={styles.progressFill}
                  style={{ width: `${((step + 1) / STEPS.length) * 100}%` }}
                />
              </div>

              <div className={styles.steps}>
                {STEPS.map((text, index) => (
                  <div
                    key={text}
                    className={`${styles.step} ${
                      index < step ? styles.done : ""
                    } ${index === step ? styles.current : ""}`}
                  >
                    <span className={styles.dot} />
                    <span>{text}</span>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <div className={`card ${styles.hintCard}`}>
              <span className="label">Что будет дальше</span>
              <p className="lead">
                По брифу и материалам трека соберётся программа на четыре
                недели. Её можно будет отредактировать и только потом открыть
                стажёру.
              </p>
            </div>
          )}
        </aside>
      </div>
    </div>
  );
}
