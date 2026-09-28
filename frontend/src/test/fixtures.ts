import type { Schemas } from "../api/client";

export function me(overrides: Partial<Schemas["MeResponse"]> = {}): Schemas["MeResponse"] {
  return {
    id: "u-1",
    email: "anna@meridian-stroy.ru",
    full_name: "Анна Смирнова",
    role: "employee",
    tenant_id: "t-1",
    company_name: "ООО «Меридиан Строй»",
    must_change_password: false,
    last_login_at: null,
    ...overrides,
  };
}

export function tokens(n = 1): Schemas["TokenResponse"] {
  return {
    access_token: `access-${n}`,
    refresh_token: `refresh-${n}`,
    token_type: "bearer",
    expires_in: 900,
  };
}

export function answer(
  overrides: Partial<Schemas["FaqAnswerResponse"]> = {},
): Schemas["FaqAnswerResponse"] {
  return {
    answer_id: "a-1",
    content: "Суточные по России — 700 рублей [1]. За рубеж — 2500 рублей [2].",
    answer_given: true,
    origin: "documents",
    sources: [
      {
        material_id: "m-1",
        title: "Положение о командировках.docx",
        heading_path: ["Положение о командировках", "2. Суточные"],
        position: 3,
        content:
          "Положение о командировках > 2. Суточные\nСуточные при командировках по России — 700 рублей в сутки.",
        source_url: "https://portal.example.ru/docs/42",
      },
      {
        material_id: "m-2",
        title: "Приказ о суточных",
        heading_path: [],
        position: 0,
        content: "Приказ о суточных\nЗа рубеж — 2500 рублей.",
        source_url: null,
      },
    ],
    diagnostics: null,
    ...overrides,
  };
}
