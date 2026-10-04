import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  FileUp,
  Folder as FolderIcon,
  FolderInput,
  FolderPlus,
  Lock,
  Pencil,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
import { useMemo, useRef, useState, type DragEvent, type SubmitEvent } from "react";
import { Link, useSearchParams } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { describeCode } from "../lib/codes";
import { formatBytes, formatDateTime, formatRelative, plural } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Badge, type Tone } from "../ui/Badge";
import { Button } from "../ui/Button";
import { TextAreaField, TextField } from "../ui/Field";
import { IconButton } from "../ui/IconButton";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { EmptyState } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { SegmentedControl } from "../ui/SegmentedControl";
import { SkeletonList } from "../ui/Skeleton";
import { Spinner } from "../ui/Spinner";
import { Table } from "../ui/Table";
import tableStyles from "../ui/Table.module.css";
import styles from "./Admin.module.css";
import { ConfirmDialog } from "./common";
import {
  ALL,
  audience,
  CONNECTORS,
  FOLDERS_KEY,
  ROOT,
  ROOT_NAME,
  useFolders,
  type Folder,
} from "./folderModel";
import { FolderBar, FolderDialog, MoveDialog, type FolderCounts } from "./folders";

type Material = Schemas["MaterialResponse"];
type Filter = "all" | "ready" | "processing" | "failed";

// Что принимает сервер при всех включённых форматах Р-5 (INGEST_EXTRA_FORMATS);
// выключенный формат сервер отклонит сам — с советом, как сохранить файл.
const ACCEPT = ".pdf,.docx,.doc,.xlsx,.pptx,.txt,.md,.markdown";
const ACCEPTED = /\.(pdf|docx?|xlsx|pptx|txt|md|markdown)$/i;
export const MAX_UPLOAD_BYTES = 25 * 1024 * 1024;

const STATUS: Record<Material["status"], { label: string; tone: Tone }> = {
  pending: { label: "в очереди", tone: "muted" },
  processing: { label: "обрабатывается", tone: "accent" },
  ready: { label: "готов", tone: "ok" },
  failed: { label: "ошибка", tone: "error" },
};

function inProgress(material: Material): boolean {
  return material.status === "pending" || material.status === "processing";
}

function matches(material: Material, filter: Filter): boolean {
  if (filter === "all") return true;
  if (filter === "processing") return inProgress(material);
  return material.status === filter;
}

function titleFromFile(name: string): string {
  return (
    name
      .replace(/\.[^.]+$/, "")
      .trim()
      .slice(0, 200) || name.slice(0, 200)
  );
}

const FORMAT_CLASS: Record<string, string | undefined> = {
  pdf: styles.pdf,
  docx: styles.docx,
  doc: styles.docx,
  xlsx: styles.xlsx,
  pptx: styles.pptx,
};

export function DocIcon({ format, url }: { format: string | null; url?: string | null }) {
  const kind = (format ?? "").toLowerCase();
  if (!kind && url) return <span className={`${styles.docIcon} ${styles.web}`}>web</span>;
  const cls = FORMAT_CLASS[kind] ?? "";
  return <span className={`${styles.docIcon} ${cls}`}>{kind || "txt"}</span>;
}

interface QueueItem {
  id: string;
  name: string;
  state: "waiting" | "uploading" | "done" | "error";
  message?: string;
}

/** ?folder= из адреса; неизвестная папка (удалили, старая ссылка) — все файлы. */
function placeFrom(raw: string | null, folders: Folder[] | undefined): string {
  if (!raw) return ALL;
  if (raw === ROOT || raw === CONNECTORS) return raw;
  if (folders && !folders.some((folder) => folder.id === raw)) return ALL;
  return raw;
}

function inPlace(material: Material, place: string): boolean {
  if (place === ALL) return true;
  if (place === CONNECTORS) return Boolean(material.connector_id);
  if (place === ROOT) return !material.connector_id && !material.folder_id;
  return material.folder_id === place;
}

/**
 * Вкладка «Файлы» в «Источниках» (ТЗ §5): загруженные документы по папкам
 * с доступом по отделам и документы из подключений. Открытая папка — в
 * адресе (?folder=), новые файлы ложатся в неё.
 */
export function DocumentsPage() {
  useDocumentTitle("Файлы");
  const queryClient = useQueryClient();
  const materials = useQuery({
    queryKey: ["materials"],
    queryFn: () => unwrap(api.GET("/api/v1/materials")),
    // Пока воркер индексирует — опрашиваем.
    refetchInterval: (query) => (query.state.data?.some(inProgress) ? 3000 : false),
  });
  const folders = useFolders();
  const [params, setParams] = useSearchParams();
  const [filter, setFilter] = useState<Filter>("all");
  const [search, setSearch] = useState("");
  const [queue, setQueue] = useState<QueueItem[]>([]);
  const [dragging, setDragging] = useState(false);
  const [textOpen, setTextOpen] = useState(false);
  const [renaming, setRenaming] = useState<Material | null>(null);
  const [moving, setMoving] = useState<Material | null>(null);
  const [deleting, setDeleting] = useState<Material | null>(null);
  const [editingFolder, setEditingFolder] = useState<Folder | "new" | null>(null);
  const [deletingFolder, setDeletingFolder] = useState<Folder | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);

  const folderList = useMemo(() => folders.data ?? [], [folders.data]);
  const folderById = useMemo(
    () => new Map(folderList.map((folder) => [folder.id, folder])),
    [folderList],
  );
  const place = placeFrom(params.get("folder"), folders.data);
  // Куда лягут новые файлы: в открытую папку, иначе — в общие документы.
  const target = folderById.get(place) ?? null;

  const list = useMemo(() => materials.data ?? [], [materials.data]);
  const here = useMemo(() => list.filter((m) => inPlace(m, place)), [list, place]);
  const visible = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return here.filter(
      (m) =>
        matches(m, filter) &&
        (!needle ||
          m.title.toLowerCase().includes(needle) ||
          (m.source_filename ?? "").toLowerCase().includes(needle)),
    );
  }, [here, filter, search]);

  function select(next: string) {
    setParams(
      (current) => {
        const updated = new URLSearchParams(current);
        if (next === ALL) updated.delete("folder");
        else updated.set("folder", next);
        return updated;
      },
      { replace: true },
    );
  }

  const refresh = () =>
    Promise.all([
      queryClient.invalidateQueries({ queryKey: ["materials"] }),
      queryClient.invalidateQueries({ queryKey: FOLDERS_KEY }),
    ]);

  const reingest = useMutation({
    mutationFn: (id: string) =>
      unwrap(
        api.POST("/api/v1/materials/{material_id}/ingest", {
          params: { path: { material_id: id } },
        }),
      ),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ["materials"] }),
  });

  async function upload(files: File[]) {
    // Папку запоминаем на старте: переключение во время загрузки очередь не разбрасывает.
    const folderId = target?.id ?? null;
    const items: QueueItem[] = files.map((file, index) => ({
      id: `${Date.now()}-${index}-${file.name}`,
      name: file.name,
      state: "waiting",
    }));
    setQueue((current) => [...current.filter((item) => item.state !== "done"), ...items]);
    const update = (id: string, patch: Partial<QueueItem>) =>
      setQueue((current) => current.map((item) => (item.id === id ? { ...item, ...patch } : item)));

    for (const [index, file] of files.entries()) {
      const item = items[index];
      if (!item) continue;
      if (!ACCEPTED.test(file.name)) {
        update(item.id, {
          state: "error",
          message: "Поддерживаются DOCX, DOC, XLSX, PPTX, PDF, TXT и MD",
        });
        continue;
      }
      if (file.size > MAX_UPLOAD_BYTES) {
        update(item.id, { state: "error", message: "Больше 25 МБ" });
        continue;
      }
      update(item.id, { state: "uploading" });
      try {
        await unwrap(
          api.POST("/api/v1/materials/upload", {
            body: {
              file: file as unknown as string,
              title: titleFromFile(file.name),
              folder_id: folderId,
            },
            bodySerializer: (body) => {
              const form = new FormData();
              form.append("file", file);
              form.append("title", body.title);
              if (body.folder_id) form.append("folder_id", body.folder_id);
              return form;
            },
          }),
        );
        update(item.id, { state: "done" });
      } catch (error) {
        const message =
          error instanceof ApiError && error.status === 409
            ? "Такой документ уже загружен"
            : errorMessage(error);
        update(item.id, { state: "error", message });
      }
      await refresh();
    }
  }

  function onDrop(event: DragEvent) {
    event.preventDefault();
    setDragging(false);
    const files = Array.from(event.dataTransfer.files);
    if (files.length) void upload(files);
  }

  const counts = {
    all: here.length,
    ready: here.filter((m) => m.status === "ready").length,
    processing: here.filter(inProgress).length,
    failed: here.filter((m) => m.status === "failed").length,
  };
  const folderCounts: FolderCounts | null = materials.data
    ? {
        all: list.length,
        root: list.filter((m) => inPlace(m, ROOT)).length,
        connectors: list.filter((m) => inPlace(m, CONNECTORS)).length,
      }
    : null;

  return (
    <>
      <div className={styles.tabHead}>
        <p className={styles.tabIntro}>
          По этим документам kronto отвечает сотрудникам. Папка решает, кому они видны, а файлы из
          подключений появляются здесь сами после синхронизации.
        </p>
        <div className={styles.tabActions}>
          <Button variant="ghost" size="sm" onClick={() => setEditingFolder("new")}>
            <FolderPlus size={16} aria-hidden /> Новая папка
          </Button>
          <Button variant="ghost" size="sm" onClick={() => setTextOpen(true)}>
            <Pencil size={16} aria-hidden /> Добавить текст
          </Button>
          <Button size="sm" onClick={() => fileInput.current?.click()}>
            <Upload size={16} aria-hidden /> Загрузить файлы
          </Button>
        </div>
      </div>
      <input
        ref={fileInput}
        type="file"
        accept={ACCEPT}
        multiple
        hidden
        onChange={(event) => {
          const files = Array.from(event.target.files ?? []);
          event.target.value = "";
          if (files.length) void upload(files);
        }}
      />

      {folders.isError ? (
        <div className={styles.block}>
          <Notice kind="error">Папки не загрузились: {errorMessage(folders.error)}</Notice>
        </div>
      ) : null}
      <FolderBar
        place={place}
        folders={folderList}
        counts={folderCounts}
        onSelect={select}
        onEdit={setEditingFolder}
        onDelete={setDeletingFolder}
      />

      <div
        className={`${styles.drop} ${dragging ? styles.dropActive : ""}`}
        onDragOver={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <FileUp size={28} aria-hidden className={styles.dropIcon} />
        <div className={styles.dropText}>
          <span className={styles.dropTarget}>
            {target ? `Загрузка в папку «${target.name}»` : `Загрузка в «${ROOT_NAME}»`}
          </span>
          <span className="muted">Документы увидят {audience(target)}.</span>
          <span className={`muted ${styles.dropFormats}`}>
            Перетащите файлы сюда: PDF, Word (DOCX, DOC), Excel (XLSX), PowerPoint (PPTX), TXT или
            MD, до 25 МБ каждый. Сканы без текстового слоя не читаются.
          </span>
        </div>
        <Button
          variant="ghost"
          size="sm"
          className={styles.dropPick}
          onClick={() => fileInput.current?.click()}
        >
          Выбрать
        </Button>
      </div>

      {queue.length ? (
        <ul className={styles.queue} aria-label="Загрузка">
          {queue.map((item) => (
            <li key={item.id} className={styles.queueItem}>
              {item.state === "uploading" ? <Spinner size={14} /> : null}
              <span className={styles.queueName}>{item.name}</span>
              {item.state === "waiting" ? <Badge>ждёт</Badge> : null}
              {item.state === "done" ? <Badge tone="ok">загружен</Badge> : null}
              {item.state === "error" ? <Badge tone="error">{item.message}</Badge> : null}
            </li>
          ))}
        </ul>
      ) : null}

      {materials.isPending ? (
        <SkeletonList label="Загрузка документов" />
      ) : materials.isError ? (
        <Notice kind="error">{errorMessage(materials.error)}</Notice>
      ) : list.length === 0 && place === ALL ? (
        <EmptyState title="Документов пока нет">
          <p>Загрузите регламенты и инструкции или настройте подключение к Диску или порталу.</p>
          <Link to="/admin/sources/connections">Настроить подключение</Link>
        </EmptyState>
      ) : here.length === 0 ? (
        place === CONNECTORS ? (
          <EmptyState title="Документов из подключений пока нет">
            <p>Они появятся здесь после первой синхронизации.</p>
            <Link to="/admin/sources/connections">К подключениям</Link>
          </EmptyState>
        ) : (
          <EmptyState title={target ? `В папке «${target.name}» пока пусто` : "Здесь пока пусто"}>
            <p>Перетащите файлы или нажмите «Загрузить файлы» — они попадут сюда.</p>
          </EmptyState>
        )
      ) : (
        <>
          <div className={styles.toolbar}>
            <SegmentedControl<Filter>
              label="Статус"
              value={filter}
              onChange={setFilter}
              options={[
                { value: "all", label: "Все", count: counts.all },
                { value: "ready", label: "Готовы", count: counts.ready },
                { value: "processing", label: "В работе", count: counts.processing },
                { value: "failed", label: "Ошибки", count: counts.failed },
              ]}
            />
            <span className={styles.searchWrap}>
              <input
                className={styles.search}
                type="search"
                placeholder="Поиск по названию"
                aria-label="Поиск по названию"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </span>
          </div>
          <Table label="Документы">
            <thead>
              <tr>
                <th>Документ</th>
                <th>Статус</th>
                <th>Размер</th>
                <th>Добавлен</th>
                <th className={tableStyles.actions}>
                  <span className="visually-hidden">Действия</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {visible.map((material) => {
                const status = STATUS[material.status];
                const fromConnector = Boolean(material.connector_id);
                const folder = material.folder_id ? folderById.get(material.folder_id) : undefined;
                return (
                  <tr key={material.id}>
                    <td>
                      <div className={styles.docCell}>
                        <DocIcon format={material.source_format} url={material.source_url} />
                        <div>
                          <span className={styles.docTitle}>{material.title}</span>
                          <span className={tableStyles.sub}>
                            {folder ? (
                              <>
                                <span className={styles.docFolder}>
                                  <FolderIcon size={12} aria-hidden />
                                  {folder.name}
                                  {folder.restricted ? (
                                    <Lock size={12} role="img" aria-label="доступ ограничен" />
                                  ) : null}
                                </span>
                                {" · "}
                              </>
                            ) : null}
                            {fromConnector ? (
                              <Link
                                to={`/admin/sources/connections/${material.connector_id ?? ""}`}
                              >
                                из подключения
                              </Link>
                            ) : (
                              (material.source_filename ?? "текст")
                            )}
                            {material.visibility !== "tenant" ? " · видят не все" : ""}
                          </span>
                        </div>
                      </div>
                    </td>
                    <td>
                      <div className={styles.statusCell}>
                        <Badge tone={status.tone}>{status.label}</Badge>
                        {material.status_error ? (
                          <span className={styles.errorText}>
                            {describeCode(material.status_error)}
                          </span>
                        ) : null}
                        {material.status === "ready" && material.indexed_at ? (
                          <span
                            className={tableStyles.sub}
                            title={formatDateTime(material.indexed_at)}
                          >
                            проиндексирован {formatRelative(material.indexed_at)}
                          </span>
                        ) : null}
                      </div>
                    </td>
                    <td className={`${tableStyles.nowrap} num`}>
                      {formatBytes(material.source_size)}
                    </td>
                    <td className={tableStyles.nowrap} title={formatDateTime(material.created_at)}>
                      {formatRelative(material.created_at)}
                    </td>
                    <td className={tableStyles.actions}>
                      <span className={styles.rowActions}>
                        <IconButton
                          size="sm"
                          label="Индексировать заново"
                          disabled={inProgress(material) || reingest.isPending}
                          onClick={() => reingest.mutate(material.id)}
                        >
                          <RefreshCw size={16} aria-hidden />
                        </IconButton>
                        {/* Документы из подключений правят в источнике: здесь только переиндексация. */}
                        {!fromConnector ? (
                          <>
                            {folderList.length ? (
                              <IconButton
                                size="sm"
                                label="Переместить"
                                onClick={() => setMoving(material)}
                              >
                                <FolderInput size={16} aria-hidden />
                              </IconButton>
                            ) : null}
                            <IconButton
                              size="sm"
                              label="Переименовать"
                              onClick={() => setRenaming(material)}
                            >
                              <Pencil size={16} aria-hidden />
                            </IconButton>
                            <IconButton
                              size="sm"
                              label="Удалить"
                              onClick={() => setDeleting(material)}
                            >
                              <Trash2 size={16} aria-hidden />
                            </IconButton>
                          </>
                        ) : null}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </Table>
          {visible.length === 0 ? (
            <p className="muted" style={{ marginTop: 16 }}>
              Ничего не найдено.
            </p>
          ) : (
            <p className="muted mono" style={{ marginTop: 12 }}>
              {visible.length} {plural(visible.length, "документ", "документа", "документов")}
            </p>
          )}
        </>
      )}

      <TextMaterialDialog open={textOpen} onOpenChange={setTextOpen} folder={target} />
      {renaming ? <RenameDialog material={renaming} onClose={() => setRenaming(null)} /> : null}
      {moving ? (
        <MoveDialog material={moving} folders={folderList} onClose={() => setMoving(null)} />
      ) : null}
      {editingFolder ? (
        <FolderDialog
          folder={editingFolder === "new" ? null : editingFolder}
          onClose={() => setEditingFolder(null)}
          // Новую папку сразу открываем: следующий шаг — загрузить в неё файлы.
          onSaved={editingFolder === "new" ? (saved) => select(saved.id) : undefined}
        />
      ) : null}
      <ConfirmDialog
        open={deleting !== null}
        onOpenChange={(open) => !open && setDeleting(null)}
        title="Удалить документ?"
        description={
          deleting
            ? `«${deleting.title}» пропадёт из ответов сразу. Вернуть можно только повторной загрузкой.`
            : undefined
        }
        confirmLabel="Удалить"
        onConfirm={async () => {
          if (!deleting) return;
          await unwrap(
            api.DELETE("/api/v1/materials/{material_id}", {
              params: { path: { material_id: deleting.id } },
            }),
          );
          await refresh();
        }}
      />
      <ConfirmDialog
        open={deletingFolder !== null}
        onOpenChange={(open) => !open && setDeletingFolder(null)}
        title="Удалить папку?"
        description={
          deletingFolder
            ? deletingFolder.documents
              ? `В «${deletingFolder.name}» ${deletingFolder.documents} ${plural(deletingFolder.documents, "документ", "документа", "документов")}. Удалить можно только пустую папку — сначала перенесите или удалите их.`
              : `«${deletingFolder.name}» пустая — удалится только сама папка.`
            : undefined
        }
        confirmLabel="Удалить"
        onConfirm={async () => {
          if (!deletingFolder) return;
          await unwrap(
            api.DELETE("/api/v1/folders/{folder_id}", {
              params: { path: { folder_id: deletingFolder.id } },
            }),
          );
          if (place === deletingFolder.id) select(ALL);
          await queryClient.invalidateQueries({ queryKey: FOLDERS_KEY });
        }}
      />
    </>
  );
}

function TextMaterialDialog({
  open,
  onOpenChange,
  folder,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Открытая папка; null — общие документы. */
  folder: Folder | null;
}) {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState("");
  const [content, setContent] = useState("");
  const create = useMutation({
    // Сразу в папку: создать в общих и потом перенести — значит на время
    // открыть документ всем.
    mutationFn: () =>
      unwrap(
        api.POST("/api/v1/materials", {
          body: { title: title.trim(), content, folder_id: folder?.id ?? null },
        }),
      ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["materials"] }),
        queryClient.invalidateQueries({ queryKey: FOLDERS_KEY }),
      ]);
      setTitle("");
      setContent("");
      onOpenChange(false);
    },
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    create.mutate();
  }

  return (
    <Modal
      open={open}
      onOpenChange={onOpenChange}
      title="Добавить текст"
      description={`Для коротких правил и ответов, которых нет в файлах. Поддерживается Markdown. Текст попадёт ${folder ? `в папку «${folder.name}»` : `в «${ROOT_NAME}»`}.`}
    >
      <form className={pageStyles.form} onSubmit={submit}>
        {create.isError ? <Notice kind="error">{errorMessage(create.error)}</Notice> : null}
        <TextField
          label="Название"
          required
          maxLength={200}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
        />
        <TextAreaField
          label="Текст"
          required
          rows={10}
          value={content}
          onChange={(e) => setContent(e.target.value)}
        />
        <div className={pageStyles.row} style={{ justifyContent: "flex-end" }}>
          <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)}>
            Отмена
          </Button>
          <Button
            type="submit"
            size="sm"
            busy={create.isPending}
            disabled={!title.trim() || !content.trim()}
          >
            Добавить
          </Button>
        </div>
      </form>
    </Modal>
  );
}

function RenameDialog({ material, onClose }: { material: Material; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [title, setTitle] = useState(material.title);
  const rename = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/api/v1/materials/{material_id}", {
          params: { path: { material_id: material.id } },
          body: { title: title.trim() },
        }),
      ),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ["materials"] });
      onClose();
    },
  });
  return (
    <Modal open onOpenChange={(open) => !open && onClose()} title="Переименовать документ">
      <form
        className={pageStyles.form}
        onSubmit={(event) => {
          event.preventDefault();
          rename.mutate();
        }}
      >
        {rename.isError ? <Notice kind="error">{errorMessage(rename.error)}</Notice> : null}
        <TextField
          label="Название"
          required
          maxLength={200}
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          hint="Так документ называется в источниках ответа."
          autoFocus
        />
        <div className={pageStyles.row} style={{ justifyContent: "flex-end" }}>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" size="sm" busy={rename.isPending} disabled={!title.trim()}>
            Сохранить
          </Button>
        </div>
      </form>
    </Modal>
  );
}
