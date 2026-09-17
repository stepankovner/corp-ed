import { useCallback, useEffect, useState, type FormEvent } from "react";

import { errorMessage, fieldErrorsOf, isForbidden } from "../api/ApiError";
import {
  createMaterial,
  ingestMaterial,
  listMaterials,
} from "../api/endpoints";
import { TRACK_LABELS, type MaterialListItem, type Track } from "../api/types";
import { useToken } from "../auth/AuthContext";
import { Link } from "react-router-dom";

import { Button } from "../components/Button";
import { SelectField, TextArea, TextField } from "../components/Field";
import { Notice } from "../components/Notice";
import { PageLoader } from "../components/PageLoader";
import { formatDate, pluralize } from "../format";
import styles from "./MaterialsPage.module.css";

export function MaterialsPage() {
  const token = useToken();

  const [materials, setMaterials] = useState<MaterialListItem[] | null>(null);
  const [listError, setListError] = useState<unknown>(null);

  const [track, setTrack] = useState<Track>("marketing");
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const [formError, setFormError] = useState<unknown>(null);
  const [created, setCreated] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // id материала, который сейчас индексируется: индикатор нужен
  // именно у своей строки, а ингест идёт несколько секунд.
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
    setCreated(null);
    setSaving(true);

    try {
      const material = await createMaterial(token, { track, title, content });
      setCreated(material.title);
      setTitle("");
      setContent("");
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

  const fieldErrors = fieldErrorsOf(formError);

  // Экран руководителя. Стажёру бэкенд отвечает 403 — показываем причину
  // и дорогу обратно, а не форму, которую он не сможет отправить.
  if (isForbidden(listError)) {
    return (
      <div className={styles.page}>
        <header className={styles.head}>
          <p className="eyebrow">Материалы компании</p>
          <h1 className="page-heading">Экран доступен только руководителю.</h1>
        </header>
        <Notice tone="error">{errorMessage(listError)}</Notice>
        <p>
          <Link to="/chat">← Вернуться к вопросам</Link>
        </p>
      </div>
    );
  }

  return (
    <div className={styles.page}>
      <header className={styles.head}>
        <p className="eyebrow">Материалы компании</p>
        <h1 className="page-heading">
          База знаний,{" "}
          <span className="display-muted">по которой отвечает бот.</span>
        </h1>
        <p className="muted">
          Бот отвечает только по проиндексированным материалам. Добавьте
          документ и нажмите «Проиндексировать» — текст будет нарезан
          на фрагменты и подготовлен для поиска.
        </p>
      </header>

      {listError ? (
        <Notice tone="error">{errorMessage(listError)}</Notice>
      ) : null}

      {ingestError ? (
        <Notice tone="error">{errorMessage(ingestError)}</Notice>
      ) : null}

      {ingestingId ? (
        <Notice tone="info">
          Индексируем материал: нарезаем текст на фрагменты и считаем
          эмбеддинги. Это занимает несколько секунд.
        </Notice>
      ) : null}

      <div className={styles.columns}>
        <section className={styles.list}>
          {materials === null && !listError ? (
            <PageLoader text="Загружаем материалы" />
          ) : null}

          {materials?.length === 0 ? (
            <p className={styles.empty}>
              Материалов пока нет. Добавьте первый — форма справа.
            </p>
          ) : null}

          {materials?.map((material) => {
            const indexing = ingestingId === material.id;
            return (
              <article key={material.id} className={styles.item}>
                <div className={styles.itemBody}>
                  <h2 className={styles.title}>{material.title}</h2>
                  <div className={styles.meta}>
                    <span>{TRACK_LABELS[material.track]}</span>
                    <span>{formatDate(material.created_at)}</span>
                    <span
                      className={`${styles.badge} ${
                        material.chunks > 0 ? styles.indexed : styles.pending
                      }`}
                    >
                      {material.chunks > 0
                        ? `Проиндексирован · ${pluralize(
                            material.chunks,
                            "фрагмент",
                            "фрагмента",
                            "фрагментов",
                          )}`
                        : "Не проиндексирован"}
                    </span>
                  </div>
                </div>

                <Button
                  variant="secondary"
                  loading={indexing}
                  disabled={ingestingId !== null}
                  onClick={() => void handleIngest(material.id)}
                >
                  {indexing
                    ? "Индексируем"
                    : material.chunks > 0
                      ? "Переиндексировать"
                      : "Проиндексировать"}
                </Button>
              </article>
            );
          })}
        </section>

        <form className={styles.form} onSubmit={handleCreate} noValidate>
          <div className="stack gap-8">
            <p className="eyebrow">Новый материал</p>
            <p className="muted">
              Регламент, инструкция или FAQ отдела — обычным текстом.
            </p>
          </div>

          {formError ? (
            <Notice tone="error">{errorMessage(formError)}</Notice>
          ) : null}

          {created ? (
            <Notice tone="success">
              «{created}» добавлен. Не забудьте проиндексировать.
            </Notice>
          ) : null}

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
            label="Заголовок"
            value={title}
            error={fieldErrors.title}
            onChange={(event) => setTitle(event.target.value)}
            placeholder="Регламент отпусков"
            required
          />

          <TextArea
            label="Текст"
            value={content}
            error={fieldErrors.content}
            hint="Абзацы разделяйте пустой строкой. До 20 000 символов."
            onChange={(event) => setContent(event.target.value)}
            rows={10}
            required
          />

          <Button type="submit" loading={saving}>
            {saving ? "Сохраняем" : "Добавить материал"}
          </Button>
        </form>
      </div>
    </div>
  );
}
