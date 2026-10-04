import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, Pause, Play, PlugZap, RefreshCw, Trash2 } from "lucide-react";
import { useState, type SubmitEvent } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router";

import { api, unwrap, type Schemas } from "../api/client";
import { errorMessage } from "../api/errors";
import { describeCode } from "../lib/codes";
import { formatDateTime, formatRelative } from "../lib/format";
import { useDocumentTitle } from "../lib/title";
import { Badge, type Tone } from "../ui/Badge";
import { Button } from "../ui/Button";
import { TextField } from "../ui/Field";
import { Notice } from "../ui/Notice";
import { Page, PageHeader } from "../ui/Page";
import pageStyles from "../ui/Page.module.css";
import { PageSpinner } from "../ui/Spinner";
import { Table } from "../ui/Table";
import tableStyles from "../ui/Table.module.css";
import { ConfirmDialog } from "./common";
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
  grantsLabel,
  intervalLabel,
  MODE_LABEL,
  secretFields,
  useKinds,
  type Connector,
  type Kind,
} from "./connectorModel";

type Run = Schemas["SyncRunResponse"];

const RUN_STATUS: Record<string, { label: string; tone: Tone }> = {
  running: { label: "идёт", tone: "accent" },
  succeeded: { label: "успешно", tone: "ok" },
  partial: { label: "частично", tone: "warn" },
  failed: { label: "ошибка", tone: "error" },
};

const STAT_LABELS: [string, string][] = [
  ["added", "добавлено"],
  ["updated", "обновлено"],
  ["removed", "удалено"],
  ["failed", "с ошибкой"],
  ["skipped", "пропущено"],
  ["seen", "просмотрено"],
];

function runStats(stats: Run["stats"]): string {
  return STAT_LABELS.filter(([key]) => typeof stats[key] === "number" && stats[key] !== 0)
    .map(([key, label]) => `${label} ${String(stats[key])}`)
    .join(" · ");
}

/**
 * Что адаптер отсеял, не скачивая (решение 28.09, П-3): форматы, которых
 * ассистент не читает, и слишком большие файлы. Иначе админ не узнает, что
 * часть регламентов лежит в .doc и в ответы не попадает.
 */
function runSkips(stats: Run["stats"]): string {
  const parts: string[] = [];
  const formats = stats["skipped_formats"];
  if (formats && typeof formats === "object") {
    const list = Object.entries(formats as Record<string, unknown>)
      .filter(([, count]) => typeof count === "number" && count > 0)
      .map(([ext, count]) => `${ext} — ${String(count)}`);
    if (list.length > 0) parts.push(`не читаются: ${list.join(", ")}`);
  }
  const large = stats["too_large"];
  if (typeof large === "number" && large > 0) parts.push(`слишком большие: ${large}`);
  return parts.join(" · ");
}

function duration(run: Run): string {
  if (!run.finished_at) return "";
  const seconds = Math.round(
    (new Date(run.finished_at).getTime() - new Date(run.started_at).getTime()) / 1000,
  );
  return seconds < 60 ? `${seconds} с` : `${Math.round(seconds / 60)} мин`;
}

export function ConnectorPage() {
  const { connectorId = "" } = useParams();
  const location = useLocation();
  const credentialsError = (location.state as { credentialsError?: string } | null)
    ?.credentialsError;
  const connector = useQuery({
    queryKey: ["connectors", connectorId],
    queryFn: () =>
      unwrap(
        api.GET("/api/v1/connectors/{connector_id}", {
          params: { path: { connector_id: connectorId } },
        }),
      ),
  });
  const kinds = useKinds();
  const kind = kinds.data?.find((item) => item.kind === connector.data?.kind);
  useDocumentTitle(connector.data?.name ?? "Подключение");

  if (connector.isPending || kinds.isPending) return <PageSpinner />;
  if (connector.isError) {
    return (
      <Page>
        <Notice kind="error">{errorMessage(connector.error)}</Notice>
      </Page>
    );
  }
  if (!kind) {
    return (
      <Page>
        <Notice kind="error">
          Вид подключения «{connector.data.kind}» не поддерживается этой сборкой.
        </Notice>
      </Page>
    );
  }
  return (
    <ConnectorView connector={connector.data} kind={kind} credentialsError={credentialsError} />
  );
}

function ConnectorView({
  connector,
  kind,
  credentialsError,
}: {
  connector: Connector;
  kind: Kind;
  credentialsError?: string;
}) {
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const [deleting, setDeleting] = useState(false);
  const status = CONNECTOR_STATUS[connector.status];
  const path = { params: { path: { connector_id: connector.id } } };
  const refresh = () => queryClient.invalidateQueries({ queryKey: ["connectors"] });

  const runs = useQuery({
    queryKey: ["connectors", connector.id, "runs"],
    queryFn: () => unwrap(api.GET("/api/v1/connectors/{connector_id}/runs", path)),
    refetchInterval: (query) =>
      query.state.data?.some((run) => run.status === "running") ? 4000 : false,
  });

  const test = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/connectors/{connector_id}/test", path)),
  });
  const sync = useMutation({
    mutationFn: () => unwrap(api.POST("/api/v1/connectors/{connector_id}/sync", path)),
    onSuccess: () => {
      void refresh();
      window.setTimeout(() => void runs.refetch(), 1500);
    },
  });
  const toggle = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/api/v1/connectors/{connector_id}", {
          ...path,
          body: { status: connector.status === "paused" ? "active" : "paused" },
        }),
      ),
    onSettled: refresh,
  });

  return (
    <Page>
      <Link
        to="/admin/sources/connections"
        style={{
          display: "inline-flex",
          gap: 6,
          alignItems: "center",
          marginBottom: 12,
          fontSize: "var(--fs-small)",
        }}
      >
        <ArrowLeft size={16} aria-hidden /> Все подключения
      </Link>
      <PageHeader
        label={kind.title}
        title={
          <span style={{ display: "inline-flex", gap: 12, alignItems: "center", flexWrap: "wrap" }}>
            {connector.name} <Badge tone={status.tone}>{status.label}</Badge>
          </span>
        }
        description={[
          MODE_LABEL[connector.mode],
          intervalLabel(connector.sync_interval_minutes),
          grantsLabel(connector)?.toLowerCase(),
        ]
          .filter(Boolean)
          .join(" · ")}
        actions={
          <>
            <Button variant="ghost" size="sm" busy={test.isPending} onClick={() => test.mutate()}>
              <PlugZap size={16} aria-hidden /> Проверить
            </Button>
            <Button
              size="sm"
              busy={sync.isPending}
              disabled={connector.status !== "active"}
              onClick={() => sync.mutate()}
            >
              <RefreshCw size={16} aria-hidden /> Синхронизировать
            </Button>
          </>
        }
      />

      <div className={pageStyles.stack}>
        {credentialsError ? (
          <Notice kind="error" title="Подключение создано, но ключи не приняты">
            {credentialsError}
          </Notice>
        ) : null}
        {connector.last_error_code ? (
          <Notice kind="error" title="Последняя синхронизация не удалась">
            {describeCode(connector.last_error_code)}
          </Notice>
        ) : null}
        {test.isSuccess ? (
          test.data.ok ? (
            <Notice kind="ok">Проверка прошла: источник отвечает и принимает доступ.</Notice>
          ) : (
            <Notice kind="error" title="Проверка не прошла">
              {describeCode(test.data.error_code)}
              {connector.mode === "per_user" && test.data.error_code === "credentials_missing" ? (
                <>
                  {" "}
                  — проверка идёт от вашего имени: сначала подключите свой аккаунт в{" "}
                  <Link to="/settings/connections">«Настройки → Мои подключения»</Link>.
                </>
              ) : null}
            </Notice>
          )
        ) : null}
        {test.isError ? <Notice kind="error">{errorMessage(test.error)}</Notice> : null}
        {sync.isSuccess ? (
          <Notice kind="ok">
            {sync.data.queued
              ? "Синхронизация поставлена в очередь."
              : "Синхронизация уже в очереди."}
          </Notice>
        ) : null}
        {sync.isError ? <Notice kind="error">{errorMessage(sync.error)}</Notice> : null}
      </div>

      <section className={pageStyles.section}>
        <h2 className={pageStyles.sectionTitle}>История синхронизаций</h2>
        {runs.isPending ? (
          <PageSpinner />
        ) : runs.isError ? (
          <Notice kind="error">{errorMessage(runs.error)}</Notice>
        ) : runs.data.length === 0 ? (
          <p className="muted">Запусков ещё не было.</p>
        ) : (
          <>
            {runs.data[0] && runSkips(runs.data[0].stats).includes("не читаются") ? (
              <Notice kind="info" title="Часть файлов в источнике ассистент не читает">
                {runSkips(runs.data[0].stats)}. Чтобы они попали в ответы, сохраните их в
                поддерживаемом формате: .docx, .xlsx, .pptx или PDF.
              </Notice>
            ) : null}
            <Table label="История синхронизаций" rowTitle={false}>
              <thead>
                <tr>
                  <th>Начало</th>
                  <th>Итог</th>
                  <th>Документы</th>
                  <th>Запуск</th>
                </tr>
              </thead>
              <tbody>
                {runs.data.slice(0, 20).map((run) => {
                  const runStatus = RUN_STATUS[run.status] ?? {
                    label: run.status,
                    tone: "muted" as Tone,
                  };
                  return (
                    <tr key={run.id}>
                      <td className={tableStyles.nowrap} title={formatDateTime(run.started_at)}>
                        {formatRelative(run.started_at)}
                        {duration(run) ? (
                          <span className={tableStyles.sub}>{duration(run)}</span>
                        ) : null}
                      </td>
                      <td>
                        <Badge tone={runStatus.tone}>{runStatus.label}</Badge>
                        {run.error_code ? (
                          <span className={tableStyles.sub} style={{ color: "var(--error)" }}>
                            {describeCode(run.error_code)}
                          </span>
                        ) : null}
                      </td>
                      <td>
                        {runStats(run.stats) || "без изменений"}
                        {runSkips(run.stats) ? (
                          <span className={tableStyles.sub}>{runSkips(run.stats)}</span>
                        ) : null}
                      </td>
                      <td>{run.trigger === "manual" ? "вручную" : "по расписанию"}</td>
                    </tr>
                  );
                })}
              </tbody>
            </Table>
          </>
        )}
      </section>

      <section className={pageStyles.section}>
        <h2 className={pageStyles.sectionTitle}>Настройки</h2>
        <SettingsForm connector={connector} kind={kind} />
      </section>

      <section className={pageStyles.section}>
        <h2 className={pageStyles.sectionTitle}>
          {connector.mode === "organization" ? "Учётная запись" : "Ключи приложения"}
        </h2>
        <CredentialsForm connector={connector} kind={kind} />
      </section>

      <section className={pageStyles.section}>
        <h2 className={pageStyles.sectionTitle}>Остановка</h2>
        <div className={pageStyles.row}>
          <Button variant="ghost" size="sm" busy={toggle.isPending} onClick={() => toggle.mutate()}>
            {connector.status === "paused" ? (
              <>
                <Play size={16} aria-hidden /> Возобновить
              </>
            ) : (
              <>
                <Pause size={16} aria-hidden /> Приостановить
              </>
            )}
          </Button>
          <Button variant="danger" size="sm" onClick={() => setDeleting(true)}>
            <Trash2 size={16} aria-hidden /> Удалить подключение
          </Button>
        </div>
        {toggle.isError ? <Notice kind="error">{errorMessage(toggle.error)}</Notice> : null}
      </section>

      <ConfirmDialog
        open={deleting}
        onOpenChange={setDeleting}
        title="Удалить подключение?"
        description="Документы из этого источника пропадут из ответов, доступы сотрудников будут удалены."
        confirmLabel="Удалить"
        onConfirm={async () => {
          await unwrap(api.DELETE("/api/v1/connectors/{connector_id}", path));
          await queryClient.invalidateQueries({ queryKey: ["connectors"] });
          await queryClient.invalidateQueries({ queryKey: ["materials"] });
          void navigate("/admin/sources/connections", { replace: true });
        }}
      />
    </Page>
  );
}

function SettingsForm({ connector, kind }: { connector: Connector; kind: Kind }) {
  const queryClient = useQueryClient();
  const [name, setName] = useState(connector.name);
  const [modules, setModules] = useState(connector.modules);
  const [config, setConfig] = useState<Record<string, string>>(() =>
    Object.fromEntries(
      Object.entries(connector.config).map(([key, value]) => [key, String(value)]),
    ),
  );
  const [syncInterval, setSyncInterval] = useState(connector.sync_interval_minutes);
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PATCH("/api/v1/connectors/{connector_id}", {
          params: { path: { connector_id: connector.id } },
          body: {
            name: name.trim(),
            modules,
            config: filled(config),
            sync_interval_minutes: syncInterval,
          },
        }),
      ),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["connectors"] }),
  });

  function submit(event: SubmitEvent) {
    event.preventDefault();
    save.mutate();
  }

  return (
    <form className={`${pageStyles.card} ${pageStyles.form}`} onSubmit={submit}>
      {save.isError ? <Notice kind="error">{errorMessage(save.error)}</Notice> : null}
      {save.isSuccess ? <Notice kind="ok">Сохранено.</Notice> : null}
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
      <ModulesField kind={kind} selected={modules} onChange={setModules} />
      <IntervalField value={syncInterval} onChange={setSyncInterval} />
      <div>
        <Button
          type="submit"
          size="sm"
          busy={save.isPending}
          disabled={!name.trim() || modules.length === 0}
        >
          Сохранить настройки
        </Button>
      </div>
    </form>
  );
}

function CredentialsForm({ connector, kind }: { connector: Connector; kind: Kind }) {
  const queryClient = useQueryClient();
  const fields = secretFields(kind);
  const [values, setValues] = useState<Record<string, string>>({});
  const save = useMutation({
    mutationFn: () =>
      unwrap(
        api.PUT("/api/v1/connectors/{connector_id}/credentials", {
          params: { path: { connector_id: connector.id } },
          body: { credentials: filled(values, false) },
        }),
      ),
    onSuccess: async () => {
      setValues({});
      await queryClient.invalidateQueries({ queryKey: ["connectors"] });
    },
  });

  if (fields.length === 0) {
    return <p className="muted">Этому подключению ключи не нужны.</p>;
  }

  return (
    <form
      className={`${pageStyles.card} ${pageStyles.form}`}
      onSubmit={(event) => {
        event.preventDefault();
        save.mutate();
      }}
    >
      <p className="muted" style={{ fontSize: "var(--fs-small)" }}>
        {connector.credentials_set_at
          ? `Заданы ${formatDateTime(connector.credentials_set_at)}. Сохранённые значения не показываются — впишите новые, чтобы заменить.`
          : "Ещё не заданы — без них синхронизация не начнётся."}
      </p>
      {save.isError ? <Notice kind="error">{errorMessage(save.error)}</Notice> : null}
      {save.isSuccess ? (
        <Notice kind="ok">Сохранено, синхронизация поставлена в очередь.</Notice>
      ) : null}
      <SecretInputs
        fields={fields}
        values={values}
        onChange={(key, value) => setValues((c) => ({ ...c, [key]: value }))}
        replacing={Boolean(connector.credentials_set_at)}
      />
      <div>
        <Button
          type="submit"
          size="sm"
          busy={save.isPending}
          disabled={Object.keys(filled(values, false)).length === 0}
        >
          Сохранить ключи
        </Button>
      </div>
    </form>
  );
}
