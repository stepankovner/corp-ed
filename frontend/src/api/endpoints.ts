import { request } from "./client";
import type {
  Brief,
  FaqAnswer,
  Intern,
  Material,
  Me,
  ProgramCreated,
  ProgramDetail,
  ProgramListItem,
  TokenResponse,
  Track,
} from "./types";

export function login(
  companyCode: string,
  email: string,
  password: string,
): Promise<TokenResponse> {
  return request<TokenResponse>("/api/v1/auth/login", {
    method: "POST",
    body: { company_code: companyCode, email, password },
  });
}

export function fetchMe(token: string): Promise<Me> {
  return request<Me>("/api/v1/auth/me", { token });
}

export function listMaterials(token: string): Promise<Material[]> {
  return request<Material[]>("/api/v1/materials", { token });
}

export function createMaterial(
  token: string,
  data: { track: Track; title: string; content: string },
): Promise<Material> {
  return request<Material>("/api/v1/materials", {
    method: "POST",
    body: data,
    token,
  });
}

/**
 * Подготовка материала к поиску.
 *
 * Вызывается сразу после создания и не показывается пользователю
 * отдельным действием: нарезка на фрагменты — устройство системы,
 * а не шаг в работе руководителя.
 */
export function prepareMaterial(token: string, materialId: string): Promise<unknown> {
  return request<unknown>(`/api/v1/materials/${materialId}/ingest`, {
    method: "POST",
    token,
  });
}

export function createBrief(
  token: string,
  data: {
    track: Track;
    role_title: string;
    goals: string;
    tasks: string;
    intern_level: string;
  },
): Promise<Brief> {
  return request<Brief>("/api/v1/briefs", {
    method: "POST",
    body: data,
    token,
  });
}

export function listPrograms(token: string): Promise<ProgramListItem[]> {
  return request<ProgramListItem[]>("/api/v1/programs", { token });
}

export function generateProgram(
  token: string,
  briefId: string,
): Promise<ProgramCreated> {
  return request<ProgramCreated>("/api/v1/programs/generate", {
    method: "POST",
    body: { brief_id: briefId },
    token,
  });
}

export function fetchProgram(
  token: string,
  programId: string,
): Promise<ProgramDetail> {
  return request<ProgramDetail>(`/api/v1/programs/${programId}`, { token });
}

export function updateProgram(
  token: string,
  programId: string,
  data: { content?: string; intern_id?: string },
): Promise<ProgramDetail> {
  return request<ProgramDetail>(`/api/v1/programs/${programId}`, {
    method: "PATCH",
    body: data,
    token,
  });
}

export function approveProgram(
  token: string,
  programId: string,
): Promise<ProgramDetail> {
  return request<ProgramDetail>(`/api/v1/programs/${programId}/approve`, {
    method: "POST",
    token,
  });
}

/** Программа стажёра. 404 означает «ещё не готова», а не сбой. */
export function fetchMyProgram(token: string): Promise<ProgramDetail> {
  return request<ProgramDetail>("/api/v1/programs/my", { token });
}

export function listInterns(token: string): Promise<Intern[]> {
  return request<Intern[]>("/api/v1/users/interns", { token });
}

export function askFaq(token: string, question: string): Promise<FaqAnswer> {
  return request<FaqAnswer>("/api/v1/faq/ask", {
    method: "POST",
    body: { question },
    token,
  });
}
