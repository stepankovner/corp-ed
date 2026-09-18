import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { errorMessage, fieldErrorsOf, isForbidden } from "../api/ApiError";
import { createMaterial, listMaterials, prepareMaterial } from "../api/endpoints";
import {
  TRACK_LABELS,
  TRACK_OPTIONS,
  type Material,
  type Track,
} from "../api/types";
import { useToken } from "../auth/AuthContext";
import { Button } from "../components/Button";
import { TextArea, TextField } from "../components/Field";
import { Notice } from "../components/Notice";
import { PageLoader } from "../components/PageLoader";
import { Segmented } from "../components/Segmented";
import { formatCount, formatDate } from "../format";
import styles from "./MaterialsPage.module.css";

const MAX_CONTENT = 20_000;

export function MaterialsPage() {
  const token = useToken();

  const [materials, setMaterials] = useState<Material[] | null>(null);
  const [listError, setListError] = useState<unknown>(null);

  const [track, setTrack] = useState<Track>("marketing");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [formError, setFormError] = useState<unknown>(null);
  const [warning, setWarning] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const reload = useCallback(async () => {
    try {
      setMaterials(await listMaterials(token));
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
    setWarning(null);
    setSaving(true);

    try {
      // Два запроса под одним индикатором: документ создаётся и сразу
      // готовится к поиску. Для руководителя это одно действие.
      const material = await createMaterial(token, { track, title, content });

      try {
        await prepareMaterial(token, material.id);
      } catch {
        setWarning(
          "Документ сохранён, но пока недоступен для ответов: сервис " +
            "временно недоступен. Откройте экран позже и добавьте документ " +
            "заново.",
        );
      }

      setTitle("");
      setContent("");
      await reload();
    } catch (caught) {
      setFormError(caught);
    } finally {
      setSaving(false);
    }
  }

  // Экран руководителя: стажёру бэкенд отвечает 403, и вместо пустоты
  // он должен увидеть причину и дорогу обратно.
  if (isForbidden(listError)) {
    return (
      <div className={styles.page}>
        <h1 className="h1">Материалы отдела</h1>
        <Notice tone="error">{errorMessage(listError)}</Notice>
        <p>
          <Link to="/my">Вернуться на страницу стажировки</Link>
        </p>
      </div>
    );
  }

  const fieldErrors = fieldErrorsOf(formError);

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <h1 className="h1">Материалы отдела</h1>
        <p className="lead">
          Документы, по которым бот отвечает стажёрам. Ответ строится только
          по ним.
        </p>
      </header>

      {listError ? (
        <Notice tone="error">{errorMessage(listError)}</Notice>
      ) : null}
      {warning ? <Notice tone="error">{warning}</Notice> : null}

      <div className={styles.columns}>
        <section className="card">
          <div className={styles.tableHead}>
            <span className="label">Заголовок</span>
            <span className="label">Трек</span>
            <span className="label">Дата</span>
          </div>

          {materials === null && !listError ? (
            <PageLoader text="Загружаем материалы" />
          ) : null}

          {materials?.length === 0 ? (
            <p className={styles.empty}>
              Материалов пока нет. Добавьте первый документ — по нему бот
              начнёт отвечать стажёрам.
            </p>
          ) : null}

          {materials?.map((material) => (
            <article key={material.id} className={styles.row}>
              <span className={styles.title}>{material.title}</span>
              <span className={styles.tag}>{TRACK_LABELS[material.track]}</span>
              <span className="mono">{formatDate(material.created_at)}</span>
            </article>
          ))}
        </section>

        <form
          className={`card ${styles.panel}`}
          onSubmit={handleCreate}
          noValidate
        >
          <h2 className="h2">Добавить материал</h2>

          {formError ? (
            <Notice tone="error">{errorMessage(formError)}</Notice>
          ) : null}

          <Segmented
            label="Трек"
            value={track}
            options={TRACK_OPTIONS}
            onChange={setTrack}
          />

          <TextField
            label="Заголовок"
            value={title}
            error={fieldErrors.title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Регламент запуска кампаний"
            required
          />

          <TextArea
            label="Текст документа"
            value={content}
            error={fieldErrors.content}
            onChange={(event) => setContent(event.target.value)}
            aside={
              <span className="mono">
                {formatCount(content.length)} / {formatCount(MAX_CONTENT)}
              </span>
            }
            maxLength={MAX_CONTENT}
            rows={8}
            required
          />

          <Button className={styles.submit} type="submit" loading={saving}>
            {saving ? "Добавляем материал" : "Добавить"}
          </Button>
        </form>
      </div>
    </div>
  );
}
