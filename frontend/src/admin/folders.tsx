import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Ellipsis, Files, Folder as FolderIcon, Lock, Pencil, Plug, Trash2 } from "lucide-react";
import { useId, useState, type ReactNode, type SubmitEvent } from "react";
import { Link } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { plural } from "../lib/format";
import { DEPARTMENTS_KEY } from "../people/keys";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "../ui/DropdownMenu";
import { Checkbox, SelectField, TextField } from "../ui/Field";
import fieldStyles from "../ui/Field.module.css";
import { Button } from "../ui/Button";
import { IconButton } from "../ui/IconButton";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import pageStyles from "../ui/Page.module.css";
import { Spinner } from "../ui/Spinner";
import { useToast } from "../ui/useToast";
import styles from "./Admin.module.css";
import {
  ALL,
  audience,
  CONNECTORS,
  FOLDERS_KEY,
  folderAccess,
  ROOT,
  ROOT_NAME,
  type Folder,
} from "./folderModel";

type Material = Schemas["MaterialResponse"];

export interface FolderCounts {
  all: number;
  root: number;
  connectors: number;
}

/**
 * Папки над таблицей документов: выбор фильтрует таблицу и задаёт, куда
 * лягут новые файлы. Кнопки с aria-pressed — как у SegmentedControl.
 */
export function FolderBar({
  place,
  folders,
  counts,
  onSelect,
  onEdit,
  onDelete,
}: {
  place: string;
  folders: Folder[];
  /** null — список документов ещё грузится. */
  counts: FolderCounts | null;
  onSelect: (place: string) => void;
  onEdit: (folder: Folder) => void;
  onDelete: (folder: Folder) => void;
}) {
  return (
    <div className={styles.folders} role="group" aria-label="Папки">
      <FolderTile
        icon={<Files size={16} aria-hidden />}
        name="Все файлы"
        meta="С разным доступом"
        count={counts?.all ?? null}
        active={place === ALL}
        onSelect={() => onSelect(ALL)}
      />
      <FolderTile
        icon={<FolderIcon size={16} aria-hidden />}
        name={ROOT_NAME}
        meta="Все сотрудники"
        count={counts?.root ?? null}
        active={place === ROOT}
        onSelect={() => onSelect(ROOT)}
      />
      {folders.map((folder) => (
        <FolderTile
          key={folder.id}
          icon={<FolderIcon size={16} aria-hidden />}
          name={folder.name}
          restricted={folder.restricted}
          meta={folderAccess(folder)}
          metaTitle={folder.departments.map((department) => department.name).join(", ")}
          count={folder.documents}
          active={place === folder.id}
          onSelect={() => onSelect(folder.id)}
          menu={
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <IconButton
                  size="sm"
                  label={`Действия: ${folder.name}`}
                  className={styles.folderMenu}
                >
                  <Ellipsis size={16} aria-hidden />
                </IconButton>
              </DropdownMenuTrigger>
              <DropdownMenuContent align="end">
                <DropdownMenuItem
                  icon={<Pencil size={16} aria-hidden />}
                  onSelect={() => onEdit(folder)}
                >
                  Название и доступ
                </DropdownMenuItem>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  className={styles.menuDanger}
                  icon={<Trash2 size={16} aria-hidden />}
                  onSelect={() => onDelete(folder)}
                >
                  Удалить папку
                </DropdownMenuItem>
              </DropdownMenuContent>
            </DropdownMenu>
          }
        />
      ))}
      {/* Документы из подключений в папки не кладутся: их видимость — права в источнике. */}
      {counts && counts.connectors > 0 ? (
        <FolderTile
          icon={<Plug size={16} aria-hidden />}
          name="Из подключений"
          meta="Доступ — как в источнике"
          count={counts.connectors}
          active={place === CONNECTORS}
          onSelect={() => onSelect(CONNECTORS)}
        />
      ) : null}
    </div>
  );
}

function FolderTile({
  icon,
  name,
  restricted = false,
  meta,
  metaTitle,
  count,
  active,
  onSelect,
  menu,
}: {
  icon: ReactNode;
  name: string;
  restricted?: boolean;
  meta: string;
  metaTitle?: string;
  count: number | null;
  active: boolean;
  onSelect: () => void;
  menu?: ReactNode;
}) {
  return (
    <div className={styles.folder}>
      <button
        type="button"
        className={`${styles.folderPick} ${menu ? styles.folderPickMenu : ""}`}
        aria-pressed={active}
        onClick={onSelect}
      >
        <span className={styles.folderName}>
          {icon}
          <span className={styles.folderTitle}>{name}</span>
        </span>
        <span className={styles.folderMeta} title={metaTitle || undefined}>
          {restricted ? <Lock size={12} role="img" aria-label="доступ ограничен" /> : null}
          <span>{meta}</span>
        </span>
        {count !== null ? (
          <span className={styles.folderCount}>
            {count} {plural(count, "документ", "документа", "документов")}
          </span>
        ) : null}
      </button>
      {menu}
    </div>
  );
}

/** Новая папка (folder = null) или правка названия и доступа. */
/**
 * Сколько людей в выбранных отделах ждут подтверждения отдела (ТЗ §7): им
 * папка откроется только после подтверждения в «Сотрудниках».
 */
function WaitingHint({ waiting }: { waiting: number }) {
  if (waiting === 0) return null;
  return (
    <p className={styles.checkHint}>
      {waiting} {plural(waiting, "человек", "человека", "человек")} в этих отделах{" "}
      {plural(waiting, "ждёт", "ждут", "ждут")} подтверждения отдела — папка откроется после него.{" "}
      <Link to="/admin/users">Подтвердить в «Сотрудниках»</Link>
    </p>
  );
}

export function FolderDialog({
  folder,
  onClose,
  onSaved,
}: {
  folder: Folder | null;
  onClose: () => void;
  onSaved?: (folder: Folder) => void;
}) {
  const queryClient = useQueryClient();
  const departments = useQuery({
    queryKey: DEPARTMENTS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/departments")),
  });
  const [name, setName] = useState(folder?.name ?? "");
  const [restricted, setRestricted] = useState(folder?.restricted ?? false);
  const [selected, setSelected] = useState<string[]>(
    () => folder?.departments.map((department) => department.id) ?? [],
  );
  const value = name.split(/\s+/).filter(Boolean).join(" ");
  const groupName = useId();

  const save = useMutation({
    mutationFn: () => {
      // Отделы — в порядке списка, а не кликов; пока список не загрузился —
      // как были, чтобы правка названия не сбросила доступ.
      const ids = departments.data
        ? departments.data.filter((d) => selected.includes(d.id)).map((d) => d.id)
        : selected;
      const body = { name: value, restricted, department_ids: restricted ? ids : [] };
      return folder
        ? unwrap(
            api.PATCH("/api/v1/folders/{folder_id}", {
              params: { path: { folder_id: folder.id } },
              body,
            }),
          )
        : unwrap(api.POST("/api/v1/folders", { body }));
    },
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: FOLDERS_KEY });
      onSaved?.(saved);
      onClose();
    },
  });
  // Занятое название — ошибка поля, остальное — над формой.
  const nameTaken = save.error instanceof ApiError && save.error.code === "folder_exists";

  function submit(event: SubmitEvent) {
    event.preventDefault();
    if (value) save.mutate();
  }

  function toggle(id: string, checked: boolean) {
    setSelected((current) =>
      checked ? [...current, id] : current.filter((selectedId) => selectedId !== id),
    );
  }

  return (
    <Modal
      open
      onOpenChange={(open) => !open && onClose()}
      title={folder ? "Название и доступ" : "Новая папка"}
      description="Папка решает, кто из сотрудников найдёт её документы в ответах и источниках."
    >
      <form className={pageStyles.form} onSubmit={submit}>
        {save.isError && !nameTaken ? (
          <Notice kind="error">{errorMessage(save.error)}</Notice>
        ) : null}
        <TextField
          label="Название"
          required
          maxLength={100}
          value={name}
          onChange={(e) => setName(e.target.value)}
          error={nameTaken ? errorMessage(save.error) : null}
          autoFocus
        />
        <fieldset className={styles.fieldset}>
          <legend className={styles.legend}>Кто видит документы</legend>
          <Choice
            name={groupName}
            checked={!restricted}
            onChange={() => setRestricted(false)}
            label="Все сотрудники"
            hint="Документы найдутся у каждого в компании."
          />
          <Choice
            name={groupName}
            checked={restricted}
            onChange={() => setRestricted(true)}
            label="Только отделы"
            hint="Остальные не увидят их ни в ответах, ни в источниках, ни по ссылке на диалог."
          />
          {restricted ? (
            departments.isPending ? (
              <Spinner size={16} />
            ) : departments.isError ? (
              <Notice kind="error">{errorMessage(departments.error)}</Notice>
            ) : departments.data.length === 0 ? (
              <p className={styles.checkHint}>
                В компании пока нет отделов.{" "}
                <Link to="/admin/departments">Сначала заведите отделы</Link> и распределите по ним
                сотрудников.
              </p>
            ) : (
              <>
                <div className={styles.departmentList} role="group" aria-label="Отделы">
                  {departments.data.map((department) => (
                    <Checkbox
                      key={department.id}
                      label={department.name}
                      checked={selected.includes(department.id)}
                      onChange={(e) => toggle(department.id, e.target.checked)}
                    />
                  ))}
                </div>
                {!departments.data.some((d) => selected.includes(d.id)) ? (
                  <p className={styles.checkHint}>
                    Отдел не выбран — документы увидят только администраторы.
                  </p>
                ) : null}
                <WaitingHint
                  waiting={departments.data
                    .filter((d) => selected.includes(d.id))
                    .reduce((sum, d) => sum + d.unconfirmed, 0)}
                />
              </>
            )
          ) : null}
          <p className={fieldStyles.hint}>Администраторы видят все папки.</p>
        </fieldset>
        <div className={pageStyles.row} style={{ justifyContent: "flex-end" }}>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" size="sm" busy={save.isPending} disabled={!value}>
            {folder ? "Сохранить" : "Создать папку"}
          </Button>
        </div>
      </form>
    </Modal>
  );
}

/** Переключатель с пояснением под подписью (вровень с ней, а не с кружком). */
function Choice({
  name,
  checked,
  onChange,
  label,
  hint,
}: {
  name: string;
  checked: boolean;
  onChange: () => void;
  label: string;
  hint: string;
}) {
  const hintId = useId();
  return (
    <div>
      <label className={fieldStyles.check}>
        <input
          type="radio"
          name={name}
          checked={checked}
          onChange={onChange}
          aria-describedby={hintId}
        />
        <span>{label}</span>
      </label>
      <p className={styles.checkHint} id={hintId}>
        {hint}
      </p>
    </div>
  );
}

/** Перенос загруженного документа в папку или в общие документы. */
export function MoveDialog({
  material,
  folders,
  onClose,
}: {
  material: Material;
  folders: Folder[];
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const current = material.folder_id ?? "";
  const [target, setTarget] = useState(current);
  const folder = folders.find((item) => item.id === target) ?? null;
  const move = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/api/v1/materials/{material_id}", {
          params: { path: { material_id: material.id } },
          body: { folder_id: target || null },
        }),
      ),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ["materials"] }),
        queryClient.invalidateQueries({ queryKey: FOLDERS_KEY }),
      ]);
      toast.show(folder ? `Перенесён в папку «${folder.name}»` : `Перенесён в «${ROOT_NAME}»`);
      onClose();
    },
  });

  return (
    <Modal
      open
      onOpenChange={(open) => !open && onClose()}
      title="Переместить документ"
      description={`«${material.title}»`}
    >
      <form
        className={pageStyles.form}
        onSubmit={(event) => {
          event.preventDefault();
          move.mutate();
        }}
      >
        {move.isError ? <Notice kind="error">{errorMessage(move.error)}</Notice> : null}
        <SelectField
          label="Папка"
          value={target}
          onChange={(e) => setTarget(e.target.value)}
          hint={`Документ увидят ${audience(folder)}.`}
        >
          <option value="">{ROOT_NAME}</option>
          {folders.map((item) => (
            <option key={item.id} value={item.id}>
              {item.name}
            </option>
          ))}
        </SelectField>
        <div className={pageStyles.row} style={{ justifyContent: "flex-end" }}>
          <Button variant="ghost" size="sm" onClick={onClose}>
            Отмена
          </Button>
          <Button type="submit" size="sm" busy={move.isPending} disabled={target === current}>
            Переместить
          </Button>
        </div>
      </form>
    </Modal>
  );
}
