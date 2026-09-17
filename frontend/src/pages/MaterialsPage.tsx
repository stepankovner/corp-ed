import { useCallback, useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";

import { errorMessage, fieldErrorsOf, isForbidden } from "../api/ApiError";
import {
  createMaterial,
  ingestMaterial,
  listMaterials,
} from "../api/endpoints";
import { TRACK_LABELS, type MaterialListItem, type Track } from "../api/types";
import { useToken } from "../auth/AuthContext";
import { Button } from "../components/Button";
import { SelectField, TextArea, TextField } from "../components/Field";
import { Notice } from "../components/Notice";
import { PageLoader } from "../components/PageLoader";
import { formatDate } from "../format";
import styles from "./MaterialsPage.module.css";

export function MaterialsPage() {
  const token = useToken();

  const [materials, setMaterials] = useState<MaterialListItem[] | null>(null);
  const [listError, setListError] = useState<unknown>(null);

  const [formOpen, setFormOpen] = useState(false);
  const [track, setTrack] = useState<Track>("marketing");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [formError, setFormError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);

  // id материала, который сейчас индексируется: индикатор нужен у своей
  // строки, а ингест идёт несколько секунд.
  const [ingestingId, setIngestingId] = useState<string | null>(null);
  const [ingestError, setIngestError] = useState<unknown>(null);

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
    setSaving(true);

    try {
      await createMaterial(token, { track, title, content });
      setTitle("");
      setContent("");
      setFormOpen(false);
      await reload();
    } catch (caught) {
      setFormError(caught);
    } finally {
      setSaving(false);
    }
  }

  async function handleIngest(materialId: string) {
    setIngestError(null);
    setIngestingId(materialId);

    try {
      await ingestMaterial(token, materialId);
      await reload();
    } catch (caught) {
      setIngestError(caught);
    } finally {
      setIngestingId(null);
    }
  }

  // Экран руководителя. Стажёру бэкенд отвечает 403 — показываем причину
  // и дорогу обратно, а не форму, которую он не сможет отправить.
  if (isForbidden(listError)) {
    return (
      <div className={styles.page}>
        <h1 className="title">Материалы</h1>
        <Notice tone="error">{errorMessage(listError)}</Notice>
        <p>
          <Link to="/chat">Вернуться к вопросам</Link>
        </p>
      </div>
    );
  }

  const fieldErrors = fieldErrorsOf(formError);

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <div className={styles.headText}>
          <h1 className="title">Материалы</h1>
          <p className="subtitle">
            Регламенты и инструкции отделов. Бот отвечает стажёрам только
            по ним.
          </p>
        </div>
        <Button
          variant={formOpen ? "secondary" : "primary"}
          onClick={() => setFormOpen((open) => !open)}
        >
          {formOpen ? "Отмена" : "Добавить материал"}
        </Button>
      </header>

      {listError ? (
        <Notice tone="error">{errorMessage(listError)}</Notice>
      ) : null}

      {ingestError ? (
        <Notice tone="error">{errorMessage(ingestError)}</Notice>
      ) : null}

      {formOpen ? (
        <form
          className={`${styles.panel} ${styles.form}`}
          onSubmit={handleCreate}
          noValidate
        >
          {formError && !fieldErrors.title && !fieldErrors.content ? (
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
              label="Название"
              value={title}
              error={fieldErrors.title}
              onChange={(event) => setTitle(event.target.value)}
              placeholder="Регламент отпусков и отгулов"
              required
            />
          </div>

          <TextArea
            label="Текст документа"
            value={content}
            error={fieldErrors.content}
            hint="Абзацы разделяйте пустой строкой. До 20 000 символов."
            onChange={(event) => setContent(event.target.value)}
            rows={9}
            required
          />

          <div className={styles.formActions}>
            <Button type="submit" loading={saving}>
              {saving ? "Сохраняем" : "Сохранить"}
            </Button>
            <span className="meta">
              {content.length.toLocaleString("ru-RU")} / 20 000
            </span>
          </div>
        </form>
      ) : null}

      <section className={styles.panel}>
        <div className={styles.tableHead}>
          <span>Документ</span>
          <span>Направление</span>
          <span>Добавлен</span>
          <span />
        </div>

        {materials === null && !listError ? (
          <PageLoader text="Загружаем материалы" />
        ) : null}

        {materials?.length === 0 ? (
          <p className={styles.empty}>
            Пока пусто. Первый документ добавляется кнопкой сверху.
          </p>
        ) : null}

        {materials?.map((material) => {
          const indexing = ingestingId === material.id;

          return (
            <article key={material.id} className={styles.row}>
              <div className={styles.name}>
                <span className={styles.title}>{material.title}</span>
                {material.chunks === 0 && !indexing ? (
                  <span className={styles.pending}>нет в поиске</span>
                ) : null}
              </div>

              <span className={styles.cell}>
                {TRACK_LABELS[material.track]}
              </span>
              <span className="meta">{formatDate(material.created_at)}</span>

              <Button
                className={`${styles.action} ${
                  indexing ? styles.actionBusy : ""
                }`}
                variant="quiet"
                loading={indexing}
                disabled={ingestingId !== null}
                onClick={() => void handleIngest(material.id)}
              >
                {indexing
                  ? "Индексируем"
                  : material.chunks > 0
                    ? "Обновить индекс"
                    : "Проиндексировать"}
              </Button>
            </article>
          );
        })}

        {materials && materials.length > 0 ? (
          <div className={styles.footer}>
            <span>
              {materials.length}{" "}
              {materials.length === 1 ? "документ" : "документа"}
            </span>
          </div>
        ) : null}
      </section>
    </div>
  );
}
