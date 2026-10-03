import type { Schemas } from "../api/client";

export function me(overrides: Partial<Schemas["MeResponse"]> = {}): Schemas["MeResponse"] {
  return {
    id: "u-1",
    email: "anna@meridian-stroy.ru",
    first_name: "Анна",
    last_name: "Смирнова",
    full_name: "Анна Смирнова",
    must_change_password: false,
    last_login_at: null,
    company: {
      tenant_id: "t-1",
      member_id: "m-1",
      name: "ООО «Меридиан Строй»",
      role: "employee",
    },
    companies: [
      {
        tenant_id: "t-1",
        company_name: "ООО «Меридиан Строй»",
        role: "employee",
        status: "active",
      },
    ],
    mfa: { strong: false, strong_required: false },
    ...overrides,
  };
}

/** Администратор компании с приложением-аутентификатором. */
export function adminMe(overrides: Partial<Schemas["MeResponse"]> = {}): Schemas["MeResponse"] {
  return me({
    company: {
      tenant_id: "t-1",
      member_id: "m-1",
      name: "ООО «Меридиан Строй»",
      role: "admin",
    },
    companies: [
      {
        tenant_id: "t-1",
        company_name: "ООО «Меридиан Строй»",
        role: "admin",
        status: "active",
      },
    ],
    mfa: { strong: true, strong_required: true },
    ...overrides,
  });
}

/** Учётка без компании (ТЗ §2). */
export function loneMe(overrides: Partial<Schemas["MeResponse"]> = {}): Schemas["MeResponse"] {
  return me({ company: null, companies: [], ...overrides });
}

export function tokens(n = 1): Schemas["TokenResponse"] {
  return {
    access_token: `access-${n}`,
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
