/** Типы ответов бэкенда. Повторяют pydantic-схемы из api/v1/schemas. */

export type Track = "marketing" | "analytics";
export type UserRole = "manager" | "intern";
export type ProgramStatus = "draft" | "approved";

export interface TokenResponse {
  access_token: string;
  token_type: string;
}

export interface Me {
  id: string;
  email: string;
  full_name: string | null;
  role: UserRole;
  tenant_id: string;
  company_name: string;
}

export interface Material {
  id: string;
  track: Track;
  title: string;
  created_at: string;
}

export interface Brief {
  id: string;
  track: Track;
  role_title: string;
  created_at: string;
}

export interface ProgramListItem {
  id: string;
  status: ProgramStatus;
  role_title: string;
  created_at: string;
}

export interface ProgramCreated {
  id: string;
  status: ProgramStatus;
}

export interface ProgramDetail {
  id: string;
  status: ProgramStatus;
  content: string;
  created_at: string;
  intern_id: string | null;
  role_title: string;
  track: Track;
}

export interface Intern {
  id: string;
  email: string;
  full_name: string | null;
}

export interface FaqSource {
  material_id: string;
  /** Источник показывается названием документа: номер фрагмента читателю
   *  ничего не говорит, и бэкенд его не отдаёт. */
  material_title: string;
  content: string;
}

export interface FaqAnswer {
  content: string;
  /** false — ответа в материалах нет. Это не ошибка, а честный отказ. */
  answer_given: boolean;
  sources: FaqSource[];
}

export const TRACK_LABELS: Record<Track, string> = {
  marketing: "Маркетинг",
  analytics: "Аналитика",
};

export const TRACK_OPTIONS: { value: Track; label: string }[] = [
  { value: "marketing", label: "Маркетинг" },
  { value: "analytics", label: "Аналитика" },
];

export const STATUS_LABELS: Record<ProgramStatus, string> = {
  draft: "Черновик",
  approved: "Согласована",
};
