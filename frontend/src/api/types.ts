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
}

export interface MaterialListItem {
  id: string;
  track: Track;
  title: string;
  created_at: string;
  /** 0 означает «не проиндексирован». */
  chunks: number;
}

export interface Material {
  id: string;
  track: Track;
  title: string;
  created_at: string;
}

export interface IngestResult {
  material_id: string;
  chunks: number;
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
  brief_id: string;
  created_at: string;
}

export interface ProgramCreated {
  id: string;
  status: ProgramStatus;
}

export interface ProgramDetail {
  id: string;
  status: ProgramStatus;
  brief_id: string;
  content: string;
  created_at: string;
}

export interface FaqSource {
  material_id: string;
  position: number;
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
