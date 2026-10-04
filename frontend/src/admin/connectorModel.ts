import { useQuery } from "@tanstack/react-query";

import { api, unwrap, type Schemas } from "../api/client";
import { plural } from "../lib/format";
import type { Tone } from "../ui/Badge";

export type Kind = Schemas["ConnectorKindResponse"];
export type Connector = Schemas["ConnectorResponse"];
export type FieldSpec = Schemas["FieldSpecResponse"];

export const CONNECTOR_STATUS: Record<Connector["status"], { label: string; tone: Tone }> = {
  active: { label: "работает", tone: "ok" },
  paused: { label: "на паузе", tone: "muted" },
  error: { label: "ошибка", tone: "error" },
};

export const MODE_LABEL: Record<Connector["mode"], string> = {
  per_user: "каждый сотрудник подключает свой аккаунт",
  organization: "служебная учётная запись компании",
};

/** «Подключились 5 из 12 сотрудников» — у подключений, куда каждый входит своим аккаунтом. */
export function grantsLabel(connector: Connector): string | null {
  if (connector.mode !== "per_user") return null;
  const { grants_active: grants, members_active: members } = connector;
  if (grants == null || members == null) return null;
  return `${plural(grants, "Подключился", "Подключились", "Подключились")} ${grants} из ${members} ${plural(members, "сотрудника", "сотрудников", "сотрудников")}`;
}

export const INTERVALS = [15, 30, 60, 180, 360, 720, 1440];

export function intervalLabel(minutes: number): string {
  if (minutes < 60) return `каждые ${minutes} минут`;
  if (minutes === 60) return "каждый час";
  if (minutes === 1440) return "раз в сутки";
  return `каждые ${minutes / 60} часов`;
}

/** Секретные поля: для per_user — ключи приложения, для organization — учётка. */
export function secretFields(kind: Kind): FieldSpec[] {
  return kind.mode === "organization" ? kind.credential_fields : (kind.app_credential_fields ?? []);
}

/**
 * Только заполненные значения: пустое необязательное поле не отправляем.
 * Настройки обрезаем по краям, секреты (пароли) — нет.
 */
export function filled(values: Record<string, string>, trim = true): Record<string, string> {
  return Object.fromEntries(
    Object.entries(values)
      .map(([key, value]) => [key, trim ? value.trim() : value] as const)
      .filter(([, value]) => value.trim() !== ""),
  );
}

export function useKinds() {
  return useQuery({
    queryKey: ["connector-kinds"],
    queryFn: () => unwrap(api.GET("/api/v1/connectors/kinds")),
    staleTime: 5 * 60_000,
  });
}
