import { Checkbox, SelectField, TextField } from "../ui/Field";
import styles from "./Admin.module.css";
import { CopyButton } from "./common";
import { INTERVALS, intervalLabel, type FieldSpec, type Kind } from "./connectorModel";

export function ConfigFields({
  kind,
  values,
  onChange,
}: {
  kind: Kind;
  values: Record<string, string>;
  onChange: (name: string, value: string) => void;
}) {
  return (
    <>
      {kind.config_fields.map((field) => (
        <TextField
          key={field.name}
          label={field.title}
          name={field.name}
          required={field.required}
          optional={!field.required}
          value={values[field.name] ?? ""}
          onChange={(e) => onChange(field.name, e.target.value)}
          autoComplete="off"
        />
      ))}
    </>
  );
}

export function SecretInputs({
  fields,
  values,
  onChange,
  replacing,
}: {
  fields: FieldSpec[];
  values: Record<string, string>;
  onChange: (name: string, value: string) => void;
  replacing: boolean;
}) {
  return (
    <>
      {fields.map((field) => (
        <TextField
          key={field.name}
          label={field.title}
          name={field.name}
          type={field.secret ? "password" : "text"}
          autoComplete={field.secret ? "new-password" : "off"}
          required={field.required && !replacing}
          optional={!field.required}
          value={values[field.name] ?? ""}
          onChange={(e) => onChange(field.name, e.target.value)}
          placeholder={
            replacing && field.secret ? "••••••••  (задан; впишите, чтобы заменить)" : undefined
          }
        />
      ))}
    </>
  );
}

export function ModulesField({
  kind,
  selected,
  onChange,
}: {
  kind: Kind;
  selected: string[];
  onChange: (modules: string[]) => void;
}) {
  return (
    <fieldset className={styles.fieldset}>
      <legend className={styles.legend}>Что загружать</legend>
      {kind.modules.map((module) => (
        <Checkbox
          key={module.name}
          label={module.title}
          checked={selected.includes(module.name)}
          onChange={(e) =>
            onChange(
              e.target.checked
                ? [...selected, module.name]
                : selected.filter((name) => name !== module.name),
            )
          }
        />
      ))}
    </fieldset>
  );
}

export function IntervalField({
  value,
  onChange,
}: {
  value: number;
  onChange: (value: number) => void;
}) {
  return (
    <SelectField
      label="Как часто обновлять"
      value={value}
      onChange={(e) => onChange(Number(e.target.value))}
    >
      {INTERVALS.map((minutes) => (
        <option key={minutes} value={minutes}>
          {intervalLabel(minutes)}
        </option>
      ))}
    </SelectField>
  );
}

/** Что вписать в карточку OAuth-приложения на стороне источника. */
export function OAuthInstructions({ kind }: { kind: Kind }) {
  if (!kind.oauth) return null;
  const extra = kind.extra ?? {};
  const scopes = typeof extra.app_scopes === "string" ? extra.app_scopes : null;
  const appType = typeof extra.app_type === "string" ? extra.app_type : null;
  return (
    <div className={styles.card} style={{ background: "var(--surface-2)" }}>
      <p style={{ fontWeight: 500 }}>Приложение в {kind.title}</p>
      <p className="muted" style={{ fontSize: "var(--fs-small)" }}>
        Зарегистрируйте приложение на стороне {kind.title} и перенесите сюда его код и секрет.
        Каждый сотрудник потом подключит свой аккаунт в «Настройки → Мои подключения» — kronto видит
        только то, что доступно ему.
      </p>
      <dl className={styles.kv}>
        {appType ? (
          <>
            <dt>Тип приложения</dt>
            <dd>{appType}</dd>
          </>
        ) : null}
        {scopes ? (
          <>
            <dt>Права</dt>
            <dd className="mono">{scopes}</dd>
          </>
        ) : null}
      </dl>
      {kind.oauth_callback_url ? (
        <div>
          <p style={{ fontSize: "var(--fs-small)", marginBottom: 6 }}>
            Адрес возврата (Redirect URI)
          </p>
          <div className={styles.copyRow}>
            <code>{kind.oauth_callback_url}</code>
            <CopyButton value={kind.oauth_callback_url} label="Скопировать адрес" />
          </div>
        </div>
      ) : (
        <p style={{ color: "var(--warn-ink)", fontSize: "var(--fs-small)" }}>
          Адрес возврата не настроен на сервере (CONNECTOR_OAUTH_CALLBACK_URL) — сотрудники не
          смогут подключиться.
        </p>
      )}
    </div>
  );
}
