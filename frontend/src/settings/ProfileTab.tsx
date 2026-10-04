import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Camera, Trash2 } from "lucide-react";
import { useRef, useState, type ChangeEvent, type SubmitEvent } from "react";
import { Link } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { displayName, needsStrongFactor, useAuth, useMe, type Company } from "../auth/context";
import { personInitials } from "../lib/initials";
import { formatPhone } from "../lib/people";
import { DEPARTMENTS_KEY, PEOPLE_KEY } from "../people/keys";
import { useDocumentTitle } from "../lib/title";
import { Avatar } from "../ui/Avatar";
import { Button } from "../ui/Button";
import { SelectField, TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { useToast } from "../ui/useToast";
import { Section } from "./common";
import styles from "./Settings.module.css";

/** Как в схеме бэкенда (PersonName, ProfileText). */
const MAX_NAME = 100;
const MAX_PHOTO_BYTES = 5 * 1024 * 1024;
const PHOTO_TYPES = "image/jpeg,image/png,image/webp";

/** Пробелы по краям и двойные внутри сервер всё равно уберёт. */
function clean(value: string): string {
  return value.split(/\s+/).filter(Boolean).join(" ");
}

/**
 * Профиль (ТЗ §4): имя и фамилия обязательны; отчество, фото, телефон и
 * Telegram — по желанию. Должность и отдел — свои в каждой компании.
 * Всё это видят коллеги по компаниям человека и никто вне их.
 */
export function ProfileTab() {
  useDocumentTitle("Профиль");
  const me = useMe();
  return (
    <div className={styles.stack}>
      <PhotoSection />
      {/* Ключ — сохранённые значения: после сохранения форма начинается с них. */}
      <PersonalSection
        key={[me.first_name, me.last_name, me.patronymic, me.phone, me.telegram].join("\n")}
      />
      {/* Без обязательной защиты данные компании закрыты (403) — секции нет. */}
      {me.company && !needsStrongFactor(me) ? (
        <WorkSection
          key={`${me.company.member_id}\n${me.company.position ?? ""}\n${me.company.department?.id ?? ""}`}
          company={me.company}
        />
      ) : null}
      <Section
        title="Почта"
        description="На неё приходят коды входа и письма о безопасности учётной записи."
      >
        <div className={styles.value}>
          <span className={styles.valueText}>{me.email}</span>
          <span className="muted">
            Сменить почту можно в разделе{" "}
            <Link to="/settings/account">«Управление учётной записью»</Link>.
          </span>
        </div>
      </Section>
    </div>
  );
}

function PhotoSection() {
  const me = useMe();
  const { reloadMe } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    await Promise.all([reloadMe(), queryClient.invalidateQueries({ queryKey: PEOPLE_KEY })]);
  }

  const upload = useMutation({
    mutationFn: (file: File) =>
      unwrap(
        api.PUT("/api/v1/account/avatar", {
          body: { file: file as unknown as string },
          bodySerializer: () => {
            const form = new FormData();
            form.append("file", file);
            return form;
          },
        }),
      ),
    onSuccess: async () => {
      await refresh();
      toast.show("Фото обновлено");
    },
    onError: (err) => setError(errorMessage(err)),
  });
  const remove = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/v1/account/avatar")),
    onSuccess: async () => {
      await refresh();
      toast.show("Фото удалено");
    },
    onError: (err) => setError(errorMessage(err)),
  });

  function choose(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    // Тот же файл ещё раз — тоже событие change.
    event.target.value = "";
    if (!file) return;
    setError(null);
    if (file.size > MAX_PHOTO_BYTES) {
      setError("Фото больше 5 МБ — выберите поменьше.");
      return;
    }
    upload.mutate(file);
  }

  return (
    <Section
      title="Фото"
      description="Видят коллеги по компании. Вырежем квадрат по центру; данные о месте съёмки и телефоне из файла не сохраняются."
    >
      <div className={styles.photoRow}>
        <Avatar
          size="xl"
          name={displayName(me)}
          src={me.avatar_url}
          initials={personInitials(me.full_name, me.email)}
        />
        <div className={styles.actions}>
          <input
            ref={input}
            type="file"
            accept={PHOTO_TYPES}
            className="visually-hidden"
            tabIndex={-1}
            aria-hidden
            onChange={choose}
          />
          <Button size="sm" busy={upload.isPending} onClick={() => input.current?.click()}>
            <Camera size={16} aria-hidden /> {me.avatar_url ? "Заменить фото" : "Загрузить фото"}
          </Button>
          {me.avatar_url ? (
            <Button
              size="sm"
              variant="ghost"
              busy={remove.isPending}
              onClick={() => remove.mutate()}
            >
              <Trash2 size={16} aria-hidden /> Удалить
            </Button>
          ) : null}
        </div>
      </div>
      <p className={`muted ${styles.small}`}>JPEG, PNG или WebP до 5 МБ.</p>
      {error ? <Notice kind="error">{error}</Notice> : null}
    </Section>
  );
}

type FieldErrors = Partial<Record<"first" | "last" | "phone" | "telegram", string>>;

function PersonalSection() {
  const me = useMe();
  const { reloadMe } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [first, setFirst] = useState(me.first_name ?? "");
  const [last, setLast] = useState(me.last_name ?? "");
  const [patronymic, setPatronymic] = useState(me.patronymic ?? "");
  const [phone, setPhone] = useState(me.phone ? formatPhone(me.phone) : "");
  const [telegram, setTelegram] = useState(me.telegram ? `@${me.telegram}` : "");
  const [errors, setErrors] = useState<FieldErrors>({});
  const body: Schemas["ProfileUpdateRequest"] = {
    first_name: clean(first),
    last_name: clean(last),
    patronymic: clean(patronymic) || null,
    phone: phone.trim() || null,
    telegram: telegram.trim() || null,
  };
  const changed =
    body.first_name !== (me.first_name ?? "") ||
    body.last_name !== (me.last_name ?? "") ||
    body.patronymic !== me.patronymic ||
    (body.phone ?? null) !== (me.phone ? formatPhone(me.phone) : null) ||
    (body.telegram ?? null) !== (me.telegram ? `@${me.telegram}` : null);

  const save = useMutation({
    mutationFn: () => unwrap(api.PATCH("/api/v1/account", { body })),
    onSuccess: async () => {
      await Promise.all([reloadMe(), queryClient.invalidateQueries({ queryKey: PEOPLE_KEY })]);
      toast.show("Профиль сохранён");
    },
    onError: (err) => {
      if (err instanceof ApiError && err.code === "invalid_phone") {
        setErrors({ phone: err.message });
      } else if (err instanceof ApiError && err.code === "invalid_telegram") {
        setErrors({ telegram: err.message });
      }
    },
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    const next: FieldErrors = {};
    if (!body.first_name) next.first = "Укажите имя.";
    if (!body.last_name) next.last = "Укажите фамилию.";
    setErrors(next);
    if (Object.keys(next).length) return;
    save.mutate();
  }

  const fieldError =
    save.error instanceof ApiError &&
    (save.error.code === "invalid_phone" || save.error.code === "invalid_telegram");

  return (
    <Section
      title="Личные данные"
      description="Так вас видят коллеги и администраторы ваших компаний."
    >
      <form className={styles.form} onSubmit={submit} noValidate>
        {save.isError && !fieldError ? (
          <Notice kind="error">{errorMessage(save.error)}</Notice>
        ) : null}
        <div className={styles.grid2}>
          <TextField
            label="Имя"
            autoComplete="given-name"
            required
            maxLength={MAX_NAME}
            value={first}
            onChange={(e) => setFirst(e.target.value)}
            error={errors.first}
          />
          <TextField
            label="Фамилия"
            autoComplete="family-name"
            required
            maxLength={MAX_NAME}
            value={last}
            onChange={(e) => setLast(e.target.value)}
            error={errors.last}
          />
        </div>
        <TextField
          label="Отчество"
          optional
          autoComplete="additional-name"
          maxLength={MAX_NAME}
          value={patronymic}
          onChange={(e) => setPatronymic(e.target.value)}
        />
        <div className={styles.grid2}>
          <TextField
            label="Телефон"
            optional
            type="tel"
            autoComplete="tel"
            inputMode="tel"
            maxLength={32}
            placeholder="+7 999 123-45-67"
            value={phone}
            onChange={(e) => {
              setPhone(e.target.value);
              setErrors((prev) => ({ ...prev, phone: undefined }));
            }}
            error={errors.phone}
          />
          <TextField
            label="Telegram"
            optional
            autoComplete="off"
            spellCheck={false}
            maxLength={64}
            placeholder="@username"
            value={telegram}
            onChange={(e) => {
              setTelegram(e.target.value);
              setErrors((prev) => ({ ...prev, telegram: undefined }));
            }}
            error={errors.telegram}
          />
        </div>
        <div className={styles.actions}>
          <Button type="submit" size="sm" busy={save.isPending} disabled={!changed}>
            Сохранить
          </Button>
        </div>
      </form>
    </Section>
  );
}

/** Должность и отдел — в выбранной компании (у другой компании свои). */
function WorkSection({ company }: { company: Company }) {
  const { reloadMe } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const [position, setPosition] = useState(company.position ?? "");
  const [department, setDepartment] = useState(company.department?.id ?? "");
  const departments = useQuery({
    queryKey: DEPARTMENTS_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/departments")),
  });
  const changed =
    clean(position) !== (company.position ?? "") || department !== (company.department?.id ?? "");

  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/api/v1/people/{member_id}", {
          params: { path: { member_id: company.member_id } },
          body: { position: clean(position) || null, department_id: department || null },
        }),
      ),
    onSuccess: async () => {
      await Promise.all([
        reloadMe(),
        queryClient.invalidateQueries({ queryKey: PEOPLE_KEY }),
        queryClient.invalidateQueries({ queryKey: DEPARTMENTS_KEY }),
      ]);
      toast.show("Сохранено");
    },
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    save.mutate();
  }

  const list = departments.data ?? [];
  return (
    <Section
      title={`Работа в «${company.name}»`}
      description="Должность и отдел у каждой компании свои. Администратор может их поправить."
    >
      <form className={styles.form} onSubmit={submit}>
        {save.isError ? <Notice kind="error">{errorMessage(save.error)}</Notice> : null}
        <div className={styles.grid2}>
          <TextField
            label="Должность"
            optional
            autoComplete="organization-title"
            maxLength={MAX_NAME}
            value={position}
            onChange={(e) => setPosition(e.target.value)}
          />
          <SelectField
            label="Отдел"
            optional
            value={department}
            onChange={(e) => setDepartment(e.target.value)}
            disabled={departments.isPending}
            hint={
              departments.isSuccess && list.length === 0
                ? "Отделы заводит администратор компании."
                : undefined
            }
          >
            <option value="">Не выбран</option>
            {list.map((item) => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </SelectField>
        </div>
        <div className={styles.actions}>
          <Button type="submit" size="sm" busy={save.isPending} disabled={!changed}>
            Сохранить
          </Button>
        </div>
      </form>
    </Section>
  );
}
