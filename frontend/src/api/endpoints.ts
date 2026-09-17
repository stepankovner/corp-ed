import { request } from "./client";
import type {
  Brief,
  FaqAnswer,
  IngestResult,
  Material,
  MaterialListItem,
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

export function listMaterials(token: string): Promise<MaterialListItem[]> {
  return request<MaterialListItem[]>("/api/v1/materials", { token });
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

export function ingestMaterial(
  token: string,
  materialId: string,
): Promise<IngestResult> {
  return request<IngestResult>(`/api/v1/materials/${materialId}/ingest`, {
    method: "POST",
    token,
  });
}

export function listBriefs(token: string): Promise<Brief[]> {
  return request<Brief[]>("/api/v1/briefs", { token });
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

export function askFaq(token: string, question: string): Promise<FaqAnswer> {
  return request<FaqAnswer>("/api/v1/faq/ask", {
    method: "POST",
    body: { question },
    token,
  });
}
