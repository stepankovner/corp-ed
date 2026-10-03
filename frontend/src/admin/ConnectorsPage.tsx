import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronRight, Plug, Plus } from "lucide-react";
import { useState, type SubmitEvent } from "react";
import { Link, useNavigate } from "react-router";

import { api, unwrap } from "../api/client";
import { errorMessage } from "../api/errors";
import { describeCode } from "../lib/codes";
import { formatDateTime, formatRelative } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Badge } from "../ui/Badge";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Modal } from "../ui/Modal";
import { Notice } from "../ui/Notice";
import { EmptyState, Page, PageHeader } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { PageSpinner } from "../ui/Spinner";
import styles from "./Admin.module.css";
import {
  ConfigFields,
  IntervalField,
  ModulesField,
  OAuthInstructions,
  SecretInputs,
} from "./connectorFields";
import {
  CONNECTOR_STATUS,
  filled,
  MODE_LABEL,
  secretFields,
  useKinds,
  type Kind,
} from "./connectorModel";

export function ConnectorsPage() {
  useDocumentTitle("Подключения");
  const connectors = useQuery({
    queryKey: ["connectors"],
    queryFn: () => unwrap(api.GET("/api/v1/connectors")),
  });
  const kinds = useKinds();
  // Тариф (решение 30.09): сколько подключений можно и сколько заведено.
  // Ключ под ["connectors"] — обновляется вместе со списком.
  const tariff = useQuery({
    queryKey: ["connectors", "tariff"],
    queryFn: () => unwrap(api.GET("/api/v1/connectors/tariff")),
  });
  const full = tariff.data ? tariff.data.connectors >= tariff.data.connector_limit : false;
  const [creating, setCreating] = useState(false);
  const kindTitle = (kind: string) => kinds.data?.find((k) => k.kind === kind)?.title ?? kind;

  return (
    <Page>
      <PageHeader
        label="управление"
        title="Подключения"
        description="Источники, из которых Kronto сам забирает документы и обновляет их по расписанию: Битрикс24, Яндекс 360, Confluence."
        actions={
          <Button size="sm" onClick={() => setCreating(true)} disabled={!kinds.data || full}>
            <Plus size={16} aria-hidden /> Добавить подключение
          </Button>
        }
      />
      {tariff.data ? (
        <p className={styles.meta} style={{ marginBottom: "var(--s-4)" }}>
          Тариф «{tariff.data.title}»: подключений {tariff.data.connectors} из{" "}
          {tariff.data.connector_limit}.
          {full
            ? tariff.data.limited_by_tariff
              ? " Больше подключений — в тарифе «Расширенный», напишите нам."
              : " Это технический предел — напишите нам, если нужно больше."
            : null}
        </p>
      ) : null}
      {connectors.isPending ? (
        <PageSpinner />
      ) : connectors.isError ? (
        <Notice kind="error">{errorMessage(connectors.error)}</Notice>
      ) : connectors.data.length === 0 ? (
        <EmptyState icon={<Plug size={32} aria-hidden />} title="Подключений пока нет">
          <p>Подключите портал или Диск — документы будут подтягиваться сами.</p>
          <Button size="sm" onClick={() => setCreating(true)} disabled={!kinds.data}>
            Добавить подключение
          </Button>
        </EmptyState>
      ) : (
        <ul className={styles.cards}>
          {connectors.data.map((connector) => {
            const status = CONNECTOR_STATUS[connector.status];
            return (
              <li key={connector.id}>
                <Link
                  to={connector.id}
                  className={styles.card}
                  style={{ color: "inherit", textDecoration: "none" }}
                >
                  <div className={styles.cardHead}>
                    <div>
                      <p className={styles.cardTitle}>{connector.name}</p>
                      <p className="muted" style={{ fontSize: "var(--fs-small)" }}>
                        {kindTitle(connector.kind)} · {MODE_LABEL[connector.mode]}
                      </p>
                    </div>
                    <span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}>
                      {!connector.credentials_set_at ? (
                        <Badge tone="warn">ключи не заданы</Badge>
                      ) : null}
                      <Badge tone={status.tone}>{status.label}</Badge>
                      <ChevronRight size={18} aria-hidden />
                    </span>
                  </div>
                  <div className={styles.meta}>
                    <span title={formatDateTime(connector.last_sync_at)}>
                      {connector.last_sync_at
                        ? `синхронизировано ${formatRelative(connector.last_sync_at)}`
                        : "ещё не синхронизировалось"}
                    </span>
                    {connector.last_error_code ? (
                      <span style={{ color: "var(--error)" }}>
                        {describeCode(connector.last_error_code)}
                      </span>
                    ) : null}
                  </div>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
      {creating && kinds.data ? (
        <CreateConnectorDialog kinds={kinds.data} onClose={() => setCreating(false)} />
      ) : null}
    </Page>
  );
}

function CreateConnectorDialog({ kinds, onClose }: { kinds: Kind[]; onClose: () => void }) {
  const [kind, setKind] = useState<Kind | null>(kinds.length === 1 ? (kinds[0] ?? null) : null);
  return (
    <Modal
      open
      onOpenChange={(open) => !open && onClose()}
      title={kind ? kind.title : "Новое подключение"}
      description={kind ? MODE_LABEL[kind.mode] : "Выберите систему, из которой брать документы."}
    >
      {kind ? (
        <ConnectorForm
          kind={kind}
          onBack={kinds.length > 1 ? () => setKind(null) : undefined}
          onDone={onClose}
        />
      ) : (
        <div className={styles.kinds}>
          {kinds.map((item) => (
            <button
              key={item.kind}
              type="button"
              className={styles.kind}
              onClick={() => setKind(item)}
              disabled={!item.available}
            >
              <span style={{ fontWeight: 500 }}>{item.title}</span>
              <span className="muted" style={{ fontSize: "0.8125rem" }}>
                {item.available ? MODE_LABEL[item.mode] : "В тарифе «Корпоративный»"}
              </span>
            </button>
          ))}
        </div>
      )}
    </Modal>
  );
}

function ConnectorForm({
  kind,
  onBack,
  onDone,
}: {
  kind: Kind;
  onBack?: () => void;
  onDone: () => void;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [name, setName] = useState(kind.title);
  const [modules, setModules] = useState(kind.modules.map((module) => module.name));
  const [config, setConfig] = useState<Record<string, string>>({});
  const [secrets, setSecrets] = useState<Record<string, string>>({});
  const [syncInterval, setSyncInterval] = useState(60);
  const fields = secretFields(kind);

  const create = useMutation({
    mutationFn: async () => {
      const connector = await unwrap(
        api.POST("/api/v1/connectors", {
          body: {
            kind: kind.kind,
            name: name.trim(),
            modules,
            config: filled(config),
            sync_interval_minutes: syncInterval,
          },
        }),
      );
      const credentials = filled(secrets, false);
      if (Object.keys(credentials).length) {
        try {
          await unwrap(
            api.PUT("/api/v1/connectors/{connector_id}/credentials", {
              params: { path: { connector_id: connector.id } },
              body: { credentials },
            }),
          );
        } catch (error) {
          // Подключение создано — ключи можно поправить на его странице.
          await queryClient.invalidateQueries({ queryKey: ["connectors"] });
          void navigate(connector.id, { state: { credentialsError: errorMessage(error) } });
          throw error;
        }
      }
      return connector;
    },
    onSuccess: async (connector) => {
      await queryClient.invalidateQueries({ queryKey: ["connectors"] });
      onDone();
      void navigate(connector.id);
    },
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    create.mutate();
  }

  const missing =
    !name.trim() ||
    modules.length === 0 ||
    kind.config_fields.some((field) => field.required && !config[field.name]?.trim());

  return (
    <form className={pageStyles.form} onSubmit={submit}>
      {create.isError ? <Notice kind="error">{errorMessage(create.error)}</Notice> : null}
      <OAuthInstructions kind={kind} />
      <TextField
        label="Название"
        required
        maxLength={100}
        value={name}
        onChange={(e) => setName(e.target.value)}
      />
      <ConfigFields
        kind={kind}
        values={config}
        onChange={(key, value) => setConfig((c) => ({ ...c, [key]: value }))}
      />
      <SecretInputs
        fields={fields}
        values={secrets}
        onChange={(key, value) => setSecrets((c) => ({ ...c, [key]: value }))}
        replacing={false}
      />
      {typeof kind.extra?.auth === "string" ? (
        <p className="muted" style={{ fontSize: "0.875rem", marginTop: -8 }}>
          Достаточно одного способа: {kind.extra.auth}.
        </p>
      ) : null}
      <ModulesField kind={kind} selected={modules} onChange={setModules} />
      <IntervalField value={syncInterval} onChange={setSyncInterval} />
      <div className={pageStyles.row} style={{ justifyContent: "space-between" }}>
        {onBack ? (
          <Button variant="link" size="sm" onClick={onBack}>
            ← Другая система
          </Button>
        ) : (
          <span />
        )}
        <Button type="submit" size="sm" busy={create.isPending} disabled={missing}>
          Создать подключение
        </Button>
      </div>
    </form>
  );
}
