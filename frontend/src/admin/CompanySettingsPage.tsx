import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ImageUp, Trash2, X } from "lucide-react";
import {
  useId,
  useRef,
  useState,
  type ChangeEvent,
  type KeyboardEvent,
  type ReactNode,
  type SubmitEvent,
} from "react";

import { api, unwrap, type Schemas } from "../api/client";
import { ApiError, errorMessage } from "../api/errors";
import { useAuth } from "../auth/context";
import { plural } from "../lib/format";
import { companyInitials } from "../lib/initials";
import { useDocumentTitle } from "../lib/title";
import { Section } from "../settings/common";
import { Avatar } from "../ui/Avatar";
import { Button } from "../ui/Button";
import { SelectField, TextField } from "../ui/Field";
import fieldStyles from "../ui/Field.module.css";
import { IconButton } from "../ui/IconButton";
import { Notice } from "../ui/Notice";
import { Page, PageHeader } from "../ui/Page";
import { PageSpinner } from "../ui/Spinner";
import { Switch } from "../ui/Switch";
import { useToast } from "../ui/useToast";
import { ConfirmDialog } from "./common";
import styles from "./Company.module.css";

type Settings = Schemas["CompanySettingsResponse"];
type Update = Schemas["CompanySettingsRequest"];

/** Настройки компании в кэше; тот же ключ у страницы «Тариф». */
const COMPANY_KEY = ["company"];

/** Как на бэкенде (CompanySettingsRequest, company_service). */
const MAX_NAME = 200;
const MAX_DOMAINS = 10;
const MAX_LOGO_BYTES = 5 * 1024 * 1024;
const LOGO_TYPES = ["image/png", "image/jpeg", "image/webp"];

/** Пробелы по краям и двойные внутри сервер всё равно уберёт. */
function clean(value: string): string {
  return value.split(/\s+/).filter(Boolean).join(" ");
}

/**
 * «@Acme.RU», «https://acme.ru/», «anna@acme.ru» → «acme.ru». Сервер
 * приводит домен так же и проверяет окончательно; здесь — чтобы плашка
 * сразу показала то, что сохранится, и почту целиком тоже можно было вставить.
 */
function cleanDomain(value: string): string {
  const host =
    value
      .trim()
      .toLowerCase()
      .replace(/^[a-z]+:\/\//, "")
      .split("/")[0] ?? "";
  return (host.split("@").at(-1) ?? "").replace(/\.$/, "");
}

/** Грубая проверка до сервера: метки через точку, без пробелов. Кириллица (.рф) — тоже. */
const LOOKS_LIKE_DOMAIN = /^(?:[^\s.@/]+\.)+[\p{L}\d-]{2,}$/u;

/**
 * Настройки компании (ТЗ §7): название и логотип, ответ без документов,
 * правила входа, срок хранения диалогов, домены почты для приглашений. До
 * этапа 7 всё это меняла команда через CLI; каждое изменение сервер пишет
 * в журнал действий.
 */
export function CompanySettingsPage() {
  useDocumentTitle("Настройки компании");
  const settings = useQuery({
    queryKey: COMPANY_KEY,
    queryFn: () => unwrap(api.GET("/api/v1/company")),
  });

  return (
    <Page>
      <PageHeader
        label="управление"
        title="Настройки компании"
        description="Действуют для всех сотрудников компании. Каждое изменение попадает в журнал действий."
      />
      {settings.isPending ? (
        <PageSpinner />
      ) : settings.isError ? (
        <Notice kind="error">{errorMessage(settings.error)}</Notice>
      ) : (
        <div className={styles.stack}>
          <CompanySection settings={settings.data} />
          <NotFoundSection settings={settings.data} />
          <SecuritySection settings={settings.data} />
          <RetentionSection settings={settings.data} />
          {/* Ключ — сохранённый список: после сохранения правка начинается с него. */}
          <DomainsSection key={settings.data.email_domains.join("\n")} settings={settings.data} />
        </div>
      )}
    </Page>
  );
}

/**
 * PATCH настроек: в ответе — все настройки, ими и обновляем кэш. Общая
 * очередь (scope) — чтобы быстрые щелчки по переключателям дошли до
 * сервера по порядку и последний не перетёрся ответом предыдущего.
 */
function useSave(message: (body: Update) => string, after?: () => Promise<unknown>) {
  const queryClient = useQueryClient();
  const toast = useToast();
  return useMutation({
    scope: { id: "company-settings" },
    mutationFn: (body: Update) => unwrap(api.PATCH("/api/v1/company", { body })),
    onSuccess: async (data, body) => {
      queryClient.setQueryData(COMPANY_KEY, data);
      await after?.();
      toast.show(message(body));
    },
  });
}

function CompanySection({ settings }: { settings: Settings }) {
  return (
    <Section
      title="Компания"
      description="Название и логотип сотрудники видят в переключателе компаний."
    >
      <LogoField settings={settings} />
      <NameForm key={settings.name} name={settings.name} />
      <p className={`muted ${styles.small} ${styles.code}`}>
        Код компании — <span className="mono">{settings.company_code}</span>. Он есть в
        ссылках-приглашениях и не меняется.
      </p>
    </Section>
  );
}

function LogoField({ settings }: { settings: Settings }) {
  const { reloadMe } = useAuth();
  const toast = useToast();
  const queryClient = useQueryClient();
  const input = useRef<HTMLInputElement>(null);
  const [error, setError] = useState<string | null>(null);

  // Переключатель компаний берёт логотип из /auth/me — перечитываем и его.
  async function saved(logoUrl: string | null, message: string) {
    queryClient.setQueryData<Settings>(COMPANY_KEY, (old) =>
      old ? { ...old, logo_url: logoUrl } : old,
    );
    await reloadMe();
    toast.show(message);
  }

  const upload = useMutation({
    mutationFn: (file: File) =>
      unwrap(
        api.PUT("/api/v1/company/logo", {
          body: { file: file as unknown as string },
          bodySerializer: () => {
            const form = new FormData();
            form.append("file", file);
            return form;
          },
        }),
      ),
    onSuccess: (data) => saved(data.logo_url, "Логотип обновлён"),
    onError: (err) => setError(errorMessage(err)),
  });
  const remove = useMutation({
    mutationFn: () => unwrap(api.DELETE("/api/v1/company/logo")),
    onSuccess: () => saved(null, "Логотип удалён"),
    onError: (err) => setError(errorMessage(err)),
  });

  function choose(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    // Тот же файл ещё раз — тоже событие change.
    event.target.value = "";
    if (!file) return;
    setError(null);
    // В окне выбора можно переключиться на «Все файлы» — проверяем и тип.
    if (!LOGO_TYPES.includes(file.type)) {
      setError("Подойдёт картинка в PNG, JPEG или WebP.");
      return;
    }
    if (file.size > MAX_LOGO_BYTES) {
      setError("Файл больше 5 МБ — выберите поменьше.");
      return;
    }
    upload.mutate(file);
  }

  const busy = upload.isPending || remove.isPending;
  return (
    <>
      <div className={styles.logoRow}>
        {settings.logo_url ? (
          <span className={styles.logo}>
            <img src={settings.logo_url} alt="Логотип компании" />
          </span>
        ) : (
          // Пока логотипа нет, в переключателе — цветная плитка с инициалами.
          <Avatar
            shape="square"
            size="xl"
            colorful
            name={settings.name}
            initials={companyInitials(settings.name)}
          />
        )}
        <div className={styles.logoActions}>
          <input
            ref={input}
            type="file"
            accept={LOGO_TYPES.join(",")}
            className="visually-hidden"
            tabIndex={-1}
            aria-hidden
            onChange={choose}
          />
          <Button
            size="sm"
            busy={upload.isPending}
            disabled={busy}
            onClick={() => input.current?.click()}
          >
            <ImageUp size={16} aria-hidden />{" "}
            {settings.logo_url ? "Заменить логотип" : "Загрузить логотип"}
          </Button>
          {settings.logo_url ? (
            <Button
              size="sm"
              variant="ghost"
              busy={remove.isPending}
              disabled={busy}
              onClick={() => {
                setError(null);
                remove.mutate();
              }}
            >
              <Trash2 size={16} aria-hidden /> Удалить логотип
            </Button>
          ) : null}
        </div>
      </div>
      <p className={`muted ${styles.small}`}>
        PNG, JPEG или WebP до 5 МБ. Впишем в квадрат целиком, без обрезки; прозрачный фон
        сохранится.
      </p>
      {error ? <Notice kind="error">{error}</Notice> : null}
    </>
  );
}

function NameForm({ name }: { name: string }) {
  const { reloadMe } = useAuth();
  const [value, setValue] = useState(name);
  const [touched, setTouched] = useState(false);
  const cleaned = clean(value);
  // Название — в переключателе компаний, а он читает /auth/me.
  const save = useSave(() => "Название сохранено", reloadMe);

  function submit(event: SubmitEvent) {
    event.preventDefault();
    setTouched(true);
    if (cleaned && cleaned !== name) save.mutate({ name: cleaned });
  }

  return (
    <form className={styles.form} onSubmit={submit} noValidate>
      {save.isError ? <Notice kind="error">{errorMessage(save.error)}</Notice> : null}
      <TextField
        label="Название"
        required
        maxLength={MAX_NAME}
        autoComplete="organization"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        error={touched && !cleaned ? "Укажите название компании." : null}
      />
      <div className={styles.actions}>
        <Button type="submit" size="sm" busy={save.isPending} disabled={cleaned === name}>
          Сохранить
        </Button>
      </div>
    </form>
  );
}

type NotFoundMode = Schemas["NotFoundMode"];

const MODES: Choice<NotFoundMode>[] = [
  {
    value: "general",
    title: "Общий ответ с пометкой",
    text: "Ассистент ответит из общих знаний и пометит, что ответ не из документов компании.",
  },
  {
    value: "strict",
    title: "Честный отказ",
    text: "Ассистент скажет, что в документах ответа нет, и посоветует уточнить вопрос.",
  },
];

function NotFoundSection({ settings }: { settings: Settings }) {
  const save = useSave(() => "Сохранено");
  // Пока запрос в пути, показываем выбранное, а не прежнее.
  const value = (save.isPending ? save.variables.not_found_mode : null) ?? settings.not_found_mode;
  return (
    <Section
      title="Когда в документах нет ответа"
      description="Что делает ассистент, если в документах компании ничего не нашлось."
    >
      <ChoiceCards
        label="Когда в документах нет ответа"
        value={value}
        options={MODES}
        onChange={(mode) => save.mutate({ not_found_mode: mode })}
      />
      {save.isError ? <Notice kind="error">{errorMessage(save.error)}</Notice> : null}
    </Section>
  );
}

function SecuritySection({ settings }: { settings: Settings }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [confirming, setConfirming] = useState(false);
  const policy = useSave(() => "Сотрудникам снова достаточно кода на почту");
  const remember = useSave((body) =>
    body.allow_remember_device
      ? "«Запомнить это устройство» снова работает"
      : "Теперь второй шаг — при каждом входе",
  );
  const mfaPolicy = (policy.isPending ? policy.variables.mfa_policy : null) ?? settings.mfa_policy;
  const allowRemember =
    (remember.isPending ? remember.variables.allow_remember_device : null) ??
    settings.allow_remember_device;
  const error = policy.error ?? remember.error;

  return (
    <Section
      title="Вход и защита"
      description="Второй шаг входа по умолчанию — код на почту. Приложение-аутентификатор и ключ доступа надёжнее."
    >
      <Switch
        label="Требовать приложение или ключ доступа от всех сотрудников"
        hint="Администраторам это обязательно всегда. Сотрудники без приложения или ключа при следующем запросе увидят экран настройки защиты — это займёт пару минут."
        checked={mfaPolicy === "strong"}
        onCheckedChange={(checked) =>
          // Включение закрывает данные компании тем, у кого защиты ещё нет, — сначала спросим.
          checked ? setConfirming(true) : policy.mutate({ mfa_policy: "any" })
        }
      />
      <Switch
        label="Разрешить «Запомнить это устройство» при входе"
        hint="С этой отметкой на входе сотрудник 30 дней входит с того же устройства без второго шага. Если выключить — второй шаг при каждом входе."
        checked={allowRemember}
        onCheckedChange={(checked) => remember.mutate({ allow_remember_device: checked })}
      />
      {error ? <Notice kind="error">{errorMessage(error)}</Notice> : null}
      <ConfirmDialog
        open={confirming}
        onOpenChange={setConfirming}
        title="Требовать приложение или ключ доступа?"
        description="Код на почту перестанет подходить для входа. Сотрудники, у которых нет ни приложения, ни ключа доступа, не увидят документов и ответов, пока не настроят защиту — kronto сам покажет им, как это сделать."
        confirmLabel="Требовать"
        danger={false}
        onConfirm={async () => {
          const data = await unwrap(
            api.PATCH("/api/v1/company", { body: { mfa_policy: "strong" } }),
          );
          queryClient.setQueryData(COMPANY_KEY, data);
          toast.show("Теперь всем сотрудникам нужно приложение или ключ доступа");
        }}
      />
    </Section>
  );
}

type RetentionMonths = NonNullable<Update["chat_retention_months"]>;

/** Как на бэкенде (CHAT_RETENTION_MONTHS). */
const RETENTION_MONTHS: RetentionMonths[] = [1, 3, 6, 12, 24, 36];

/** «3 месяца» — и «дольше 3 месяцев» (родительный падеж). */
function months(n: number, genitive = false): string {
  return genitive
    ? `${n} ${plural(n, "месяца", "месяцев", "месяцев")}`
    : `${n} ${plural(n, "месяц", "месяца", "месяцев")}`;
}

function RetentionSection({ settings }: { settings: Settings }) {
  const queryClient = useQueryClient();
  const toast = useToast();
  const [shorter, setShorter] = useState<RetentionMonths | null>(null);
  const save = useSave((body) => `Диалоги хранятся ${months(body.chat_retention_months ?? 0)}`);
  const value =
    (save.isPending ? save.variables.chat_retention_months : null) ??
    shorter ??
    settings.chat_retention_months;

  function choose(next: RetentionMonths) {
    save.reset();
    // Короче срок — этой ночью часть диалогов удалится насовсем: сначала спросим.
    if (next < settings.chat_retention_months) setShorter(next);
    else save.mutate({ chat_retention_months: next });
  }

  return (
    <Section
      title="Хранение диалогов"
      description="Сколько хранятся диалоги сотрудников с ассистентом. Сами диалоги администратор не видит."
    >
      <SelectField
        label="Хранить диалоги"
        value={String(value)}
        onChange={(e) => choose(Number(e.target.value) as RetentionMonths)}
        hint="Диалоги без активности дольше этого срока удаляются целиком, вместе с вложениями и общими ссылками, — в том числе закреплённые. Активность — последний вопрос или «Ответить заново»."
      >
        {RETENTION_MONTHS.map((n) => (
          <option key={n} value={n}>
            {months(n)}
          </option>
        ))}
      </SelectField>
      {save.isError ? <Notice kind="error">{errorMessage(save.error)}</Notice> : null}
      <ConfirmDialog
        open={shorter !== null}
        onOpenChange={(open) => !open && setShorter(null)}
        title="Сократить срок хранения диалогов?"
        description={
          shorter
            ? `В ближайшую ночь удалятся диалоги, в которых не было вопросов дольше ${months(shorter, true)}. Вернуть их будет нельзя.`
            : undefined
        }
        confirmLabel="Сократить"
        onConfirm={async () => {
          if (shorter === null) return;
          const data = await unwrap(
            api.PATCH("/api/v1/company", { body: { chat_retention_months: shorter } }),
          );
          queryClient.setQueryData(COMPANY_KEY, data);
          toast.show(`Диалоги хранятся ${months(shorter)}`);
        }}
      />
    </Section>
  );
}

function DomainsSection({ settings }: { settings: Settings }) {
  const inputId = useId();
  const input = useRef<HTMLInputElement>(null);
  const [domains, setDomains] = useState(settings.email_domains);
  const [draft, setDraft] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const save = useSave(() => "Домены сохранены");
  const saved = settings.email_domains.join("\n");
  // Ошибку домена сервер называет по-человечески — показываем её под полем.
  const domainError =
    save.error instanceof ApiError && save.error.code === "invalid_domain"
      ? save.error.message
      : null;
  const fieldError = problem ?? domainError;

  /** Домен из поля — в список. null — не вышло, причина под полем. */
  function take(): string[] | null {
    const raw = draft.trim();
    if (!raw) return domains;
    const domain = cleanDomain(raw);
    if (!LOOKS_LIKE_DOMAIN.test(domain)) {
      setProblem(`Не похоже на домен почты: ${raw}`);
      return null;
    }
    setDraft("");
    setProblem(null);
    if (domains.includes(domain)) return domains;
    if (domains.length >= MAX_DOMAINS) {
      setProblem(`Доменов — не больше ${MAX_DOMAINS}.`);
      return null;
    }
    const next = [...domains, domain];
    setDomains(next);
    return next;
  }

  function add() {
    save.reset();
    take();
    input.current?.focus();
  }

  function remove(domain: string) {
    save.reset();
    setProblem(null);
    setDomains((prev) => prev.filter((item) => item !== domain));
    // Кнопка исчезла вместе с плашкой — фокус не должен пропасть.
    input.current?.focus();
  }

  function onKeyDown(event: KeyboardEvent<HTMLInputElement>) {
    // Enter добавляет домен в список, а сохраняет — кнопка «Сохранить».
    if (event.key === "Enter") {
      event.preventDefault();
      add();
    }
  }

  function submit(event: SubmitEvent) {
    event.preventDefault();
    // Введённый, но не добавленный домен тоже сохраняем: его явно хотели.
    const next = take();
    if (next && next.join("\n") !== saved) save.mutate({ email_domains: next });
  }

  const describedBy = fieldError ? `${inputId}-error` : `${inputId}-hint`;
  return (
    <Section
      title="Домены почты"
      description="Если домены заданы, вступить по любому приглашению можно только с почтой этих доменов. Если список пуст — с любой почтой."
    >
      <form className={styles.form} onSubmit={submit} noValidate>
        {save.isError && !domainError ? (
          <Notice kind="error">{errorMessage(save.error)}</Notice>
        ) : null}
        {domains.length ? (
          <ul className={styles.chips} aria-label="Домены почты">
            {domains.map((domain) => (
              <li key={domain} className={styles.chip}>
                <span>{domain}</span>
                <IconButton size="sm" label={`Убрать ${domain}`} onClick={() => remove(domain)}>
                  <X size={14} aria-hidden />
                </IconButton>
              </li>
            ))}
          </ul>
        ) : (
          <p className={`muted ${styles.small}`}>Доменов нет — вступить можно с любой почтой.</p>
        )}
        <div className={styles.field}>
          <label className={fieldStyles.label} htmlFor={inputId}>
            Добавить домен
          </label>
          <div className={styles.addRow}>
            <input
              ref={input}
              id={inputId}
              className={fieldStyles.control}
              inputMode="url"
              autoComplete="off"
              autoCapitalize="none"
              spellCheck={false}
              maxLength={253}
              placeholder="company.ru"
              value={draft}
              aria-invalid={fieldError ? true : undefined}
              aria-describedby={describedBy}
              onChange={(e) => {
                setDraft(e.target.value);
                setProblem(null);
              }}
              onKeyDown={onKeyDown}
            />
            <Button variant="ghost" onClick={add} disabled={!draft.trim()}>
              Добавить
            </Button>
          </div>
          {fieldError ? (
            <p className={fieldStyles.error} id={`${inputId}-error`} role="alert">
              {fieldError}
            </p>
          ) : (
            <p className={fieldStyles.hint} id={`${inputId}-hint`}>
              Например, meridian.ru. До {MAX_DOMAINS} доменов.
            </p>
          )}
        </div>
        <div className={styles.actions}>
          <Button
            type="submit"
            size="sm"
            busy={save.isPending}
            disabled={domains.join("\n") === saved && !draft.trim()}
          >
            Сохранить
          </Button>
        </div>
      </form>
    </Section>
  );
}

export interface Choice<T extends string> {
  value: T;
  title: ReactNode;
  /** Рядом с названием: «сейчас» и т. п. */
  aside?: ReactNode;
  text?: ReactNode;
}

/** Один вариант из нескольких — карточками; это настоящая группа radio. */
export function ChoiceCards<T extends string>({
  label,
  value,
  options,
  onChange,
}: {
  label: string;
  value: T;
  options: Choice<T>[];
  onChange: (value: T) => void;
}) {
  const name = useId();
  return (
    <fieldset className={styles.choices}>
      <legend className="visually-hidden">{label}</legend>
      {options.map((option) => (
        <label key={option.value} className={styles.choice}>
          <input
            type="radio"
            name={name}
            value={option.value}
            checked={option.value === value}
            onChange={() => onChange(option.value)}
          />
          <span className={styles.choiceText}>
            <span className={styles.choiceTitle}>
              {option.title}
              {option.aside}
            </span>
            {option.text ? <span className={styles.choiceHint}>{option.text}</span> : null}
          </span>
        </label>
      ))}
    </fieldset>
  );
}
