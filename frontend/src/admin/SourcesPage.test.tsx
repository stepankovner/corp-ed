import { screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { afterEach, describe, expect, it, vi } from "vitest";

import { api, type Schemas } from "../api/client";
import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

type Material = Schemas["MaterialResponse"];
type Folder = Schemas["FolderResponse"];
type Connector = Schemas["ConnectorResponse"];

const HR = { id: "d-hr", name: "Кадры", members: 3 };
const ACCOUNTING = { id: "d-acc", name: "Бухгалтерия", members: 2 };

function material(overrides: Partial<Material> = {}): Material {
  return {
    id: "m-1",
    title: "Положение о командировках",
    status: "ready",
    status_error: null,
    source_filename: "komandirovki.docx",
    source_format: "docx",
    source_size: 12_000,
    source_url: null,
    connector_id: null,
    folder_id: null,
    indexed_at: "2026-09-01T10:00:00Z",
    created_at: "2026-09-01T09:00:00Z",
    visibility: "tenant",
    ...overrides,
  };
}

function folder(overrides: Partial<Folder> = {}): Folder {
  return {
    id: "f-hr",
    name: "Кадры",
    restricted: true,
    departments: [
      { id: HR.id, name: HR.name },
      { id: ACCOUNTING.id, name: ACCOUNTING.name },
    ],
    documents: 0,
    created_at: "2026-09-01T09:00:00Z",
    ...overrides,
  };
}

function connector(overrides: Partial<Connector> = {}): Connector {
  return {
    id: "c-1",
    kind: "bitrix24",
    name: "Битрикс24",
    mode: "per_user",
    status: "active",
    config: {},
    modules: ["disk"],
    credentials_set_at: "2026-09-01T09:00:00Z",
    last_error_code: null,
    last_sync_at: null,
    sync_interval_minutes: 60,
    grants_active: null,
    members_active: null,
    created_at: "2026-09-01T09:00:00Z",
    updated_at: "2026-09-01T09:00:00Z",
    ...overrides,
  };
}

/**
 * Сервер «Источников» в памяти: документы, папки (счётчики — по документам)
 * и отделы; bodies — тела запросов на изменение.
 */
function mockSources({
  materials: initialMaterials = [] as Material[],
  folders: initialFolders = [] as Folder[],
  departments = [ACCOUNTING, HR],
} = {}) {
  const state = { materials: initialMaterials, folders: initialFolders };
  const bodies: { method: string; path: string; body: unknown }[] = [];
  const withCounts = () =>
    state.folders.map((f) => ({
      ...f,
      documents: state.materials.filter((m) => m.folder_id === f.id).length,
    }));
  server.use(
    http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe())),
    http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
    http.get("/api/v1/materials", () => HttpResponse.json(state.materials)),
    http.get("/api/v1/folders", () => HttpResponse.json(withCounts())),
    http.get("/api/v1/departments", () => HttpResponse.json(departments)),
    http.post("/api/v1/folders", async ({ request }) => {
      const body = (await request.json()) as {
        name: string;
        restricted: boolean;
        department_ids: string[];
      };
      bodies.push({ method: "POST", path: "/folders", body });
      const created = folder({
        id: "f-new",
        name: body.name,
        restricted: body.restricted,
        departments: departments
          .filter((d) => body.department_ids.includes(d.id))
          .map(({ id, name }) => ({ id, name })),
      });
      state.folders = [...state.folders, created];
      return HttpResponse.json(created, { status: 201 });
    }),
    http.delete("/api/v1/folders/:id", ({ params }) => {
      if (state.materials.some((m) => m.folder_id === params.id)) {
        return HttpResponse.json(
          {
            detail: "В папке есть документы — сначала перенесите или удалите их",
            code: "folder_not_empty",
          },
          { status: 409 },
        );
      }
      state.folders = state.folders.filter((f) => f.id !== params.id);
      return new HttpResponse(null, { status: 204 });
    }),
    http.patch("/api/v1/materials/:id", async ({ request, params }) => {
      const body = (await request.json()) as { folder_id?: string | null };
      bodies.push({ method: "PATCH", path: `/materials/${String(params.id)}`, body });
      state.materials = state.materials.map((m) => (m.id === params.id ? { ...m, ...body } : m));
      return HttpResponse.json(state.materials.find((m) => m.id === params.id));
    }),
  );
  return { state, bodies };
}

function mockConnectors(list: Connector[]) {
  server.use(
    http.get("/api/v1/connectors", () => HttpResponse.json(list)),
    http.get("/api/v1/connectors/kinds", () => HttpResponse.json([])),
    http.get("/api/v1/connectors/tariff", () =>
      HttpResponse.json({
        tariff: "extended",
        title: "Расширенный",
        connectors: list.length,
        connector_limit: 5,
        limited_by_tariff: true,
      }),
    ),
  );
}

function tile(name: RegExp) {
  return within(screen.getByRole("group", { name: "Папки" })).getByRole("button", { name });
}

function row(table: HTMLElement, text: string) {
  const tr = within(table).getByText(text).closest("tr");
  if (!tr) throw new Error("no row");
  return within(tr);
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe("источники", () => {
  it("файлы и подключения — вкладки со своими адресами", async () => {
    const user = userEvent.setup();
    mockSources({ materials: [material()] });
    mockConnectors([]);
    const { router } = renderApp("/admin/sources");

    expect(await screen.findByRole("heading", { name: "Источники" })).toBeInTheDocument();
    await screen.findByRole("table", { name: "Документы" });
    expect(router.state.location.pathname).toBe("/admin/sources/files");
    const tabs = screen.getByRole("navigation", { name: "Источники" });
    expect(within(tabs).getByRole("link", { name: "Файлы" })).toHaveAttribute(
      "aria-current",
      "page",
    );
    // Заголовок вкладки браузера — у самой вкладки, а не у раздела.
    await waitFor(() => expect(document.title).toBe("Файлы — kronto"));

    await user.click(within(tabs).getByRole("link", { name: "Подключения" }));
    expect(await screen.findByText("Подключений пока нет")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/admin/sources/connections");
    expect(document.title).toBe("Подключения — kronto");
    expect(screen.getAllByRole("heading", { level: 1 })).toHaveLength(1);
  });

  it("прежний адрес документов ведёт во вкладку «Файлы»", async () => {
    mockSources();
    const { router } = renderApp("/admin/documents");
    expect(await screen.findByText("Документов пока нет")).toBeInTheDocument();
    expect(router.state.location.pathname).toBe("/admin/sources/files");
  });

  it("папки: счётчики, замок у закрытой, выбор фильтрует таблицу и живёт в адресе", async () => {
    const user = userEvent.setup();
    mockSources({
      materials: [
        material({ id: "m-1", title: "Положение о командировках" }),
        material({ id: "m-2", title: "Штатное расписание", folder_id: "f-hr" }),
        material({ id: "m-3", title: "Регламент портала", connector_id: "c-1" }),
      ],
      folders: [
        folder(),
        folder({ id: "f-docs", name: "Договоры", restricted: false, departments: [] }),
      ],
    });
    const { router } = renderApp("/admin/sources/files?folder=f-hr");

    // Открыта папка из адреса — в таблице только её документы.
    const table = await screen.findByRole("table", { name: "Документы" });
    await waitFor(() => expect(tile(/^Кадры/)).toHaveAttribute("aria-pressed", "true"));
    expect(within(table).getByText("Штатное расписание")).toBeInTheDocument();
    expect(within(table).queryByText("Положение о командировках")).not.toBeInTheDocument();

    const hr = tile(/^Кадры/);
    expect(within(hr).getByRole("img", { name: "доступ ограничен" })).toBeInTheDocument();
    expect(hr).toHaveTextContent("Кадры, Бухгалтерия");
    expect(hr).toHaveTextContent("1 документ");
    expect(tile(/^Договоры/)).toHaveTextContent("Все сотрудники");
    expect(tile(/^Договоры/)).toHaveTextContent("0 документов");
    expect(within(tile(/^Договоры/)).queryByRole("img")).not.toBeInTheDocument();
    expect(tile(/^Все файлы/)).toHaveTextContent("3 документа");
    expect(tile(/^Общие документы/)).toHaveTextContent("1 документ");
    expect(tile(/^Из подключений/)).toHaveTextContent("1 документ");
    // Документ закрытой папки — с её названием и замком.
    expect(
      row(table, "Штатное расписание").getByRole("img", { name: "доступ ограничен" }),
    ).toBeInTheDocument();

    expect(screen.getByText("Загрузка в папку «Кадры»")).toBeInTheDocument();
    expect(
      screen.getByText("Документы увидят только администраторы и отделы: Кадры, Бухгалтерия."),
    ).toBeInTheDocument();

    await user.click(tile(/^Все файлы/));
    expect(tile(/^Все файлы/)).toHaveAttribute("aria-pressed", "true");
    expect(router.state.location.search).toBe("");
    expect(within(table).getByText("Положение о командировках")).toBeInTheDocument();
    expect(within(table).getByText("Регламент портала")).toBeInTheDocument();
    expect(screen.getByText("Загрузка в «Общие документы»")).toBeInTheDocument();

    await user.click(tile(/^Общие документы/));
    expect(router.state.location.search).toBe("?folder=root");
    expect(within(table).getByText("Положение о командировках")).toBeInTheDocument();
    expect(within(table).queryByText("Штатное расписание")).not.toBeInTheDocument();
    expect(within(table).queryByText("Регламент портала")).not.toBeInTheDocument();
  });

  it("файл загружается в открытую папку", async () => {
    const user = userEvent.setup();
    const { state } = mockSources({ folders: [folder()] });
    // FormData из jsdom fetch из Node не принимает (см. тест фото в настройках):
    // загрузку подменяем, а форму собираем тем же bodySerializer, что уйдёт в сеть.
    const original = api.POST.bind(api);
    const sent: FormData[] = [];
    vi.spyOn(api, "POST").mockImplementation(((path: string, init: never) => {
      if (path !== "/api/v1/materials/upload") return original(path as never, init);
      const { body, bodySerializer } = init as {
        body: unknown;
        bodySerializer: (body: unknown) => FormData;
      };
      sent.push(bodySerializer(body));
      state.materials = [
        ...state.materials,
        material({ id: "m-new", title: "Отпуск", folder_id: "f-hr", status: "pending" }),
      ];
      return Promise.resolve({
        data: state.materials.at(-1),
        response: new Response(null, { status: 201 }),
      });
    }) as never);
    const { container } = renderApp("/admin/sources/files?folder=f-hr");

    expect(await screen.findByText("В папке «Кадры» пока пусто")).toBeInTheDocument();
    const input = container.querySelector<HTMLInputElement>('input[type="file"]');
    if (!input) throw new Error("no file input");
    const file = new File(["text"], "Отпуск.docx", {
      type: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    });
    await user.upload(input, file);

    const queue = await screen.findByRole("list", { name: "Загрузка" });
    expect(await within(queue).findByText("загружен")).toBeInTheDocument();
    expect(sent).toHaveLength(1);
    expect(sent[0]?.get("folder_id")).toBe("f-hr");
    expect(sent[0]?.get("title")).toBe("Отпуск");
    expect(sent[0]?.get("file")).toBeInstanceOf(File);
    // Счётчик папки — заново с сервера.
    await waitFor(() => expect(tile(/^Кадры/)).toHaveTextContent("1 документ"));
  });

  it("создаёт закрытую папку для отделов и сразу её открывает", async () => {
    const user = userEvent.setup();
    const { bodies } = mockSources({ materials: [material()] });
    const { router } = renderApp("/admin/sources/files");

    await screen.findByRole("table", { name: "Документы" });
    await user.click(screen.getByRole("button", { name: "Новая папка" }));
    const dialog = await screen.findByRole("dialog", { name: "Новая папка" });
    expect(within(dialog).getByText("Администраторы видят все папки.")).toBeInTheDocument();
    await user.type(within(dialog).getByLabelText("Название"), "  Кадры ");
    expect(within(dialog).getByRole("radio", { name: "Все сотрудники" })).toBeChecked();
    await user.click(within(dialog).getByRole("radio", { name: "Только отделы" }));
    const departments = within(dialog).getByRole("group", { name: "Отделы" });
    expect(
      within(dialog).getByText("Отдел не выбран — документы увидят только администраторы."),
    ).toBeInTheDocument();
    // Порядок в запросе — как в списке, а не как кликали.
    await user.click(within(departments).getByRole("checkbox", { name: "Кадры" }));
    await user.click(within(departments).getByRole("checkbox", { name: "Бухгалтерия" }));
    await user.click(within(dialog).getByRole("button", { name: "Создать папку" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(bodies).toEqual([
      {
        method: "POST",
        path: "/folders",
        body: { name: "Кадры", restricted: true, department_ids: ["d-acc", "d-hr"] },
      },
    ]);
    expect(await screen.findByText("В папке «Кадры» пока пусто")).toBeInTheDocument();
    expect(tile(/^Кадры/)).toHaveAttribute("aria-pressed", "true");
    expect(router.state.location.search).toBe("?folder=f-new");
  });

  it("без отделов закрытую папку предлагает начать с отделов", async () => {
    const user = userEvent.setup();
    mockSources({ departments: [] });
    renderApp("/admin/sources/files");

    await user.click(await screen.findByRole("button", { name: "Новая папка" }));
    const dialog = await screen.findByRole("dialog", { name: "Новая папка" });
    await user.click(within(dialog).getByRole("radio", { name: "Только отделы" }));
    expect(
      await within(dialog).findByRole("link", { name: "Сначала заведите отделы" }),
    ).toHaveAttribute("href", "/admin/departments");
  });

  it("переносит загруженный документ в папку; документ из подключения не переносится", async () => {
    const user = userEvent.setup();
    const { bodies } = mockSources({
      materials: [
        material({ id: "m-1", title: "Положение о командировках" }),
        material({ id: "m-3", title: "Регламент портала", connector_id: "c-1" }),
      ],
      folders: [folder()],
    });
    renderApp("/admin/sources/files");

    const table = await screen.findByRole("table", { name: "Документы" });
    expect(
      row(table, "Регламент портала").queryByRole("button", { name: "Переместить" }),
    ).not.toBeInTheDocument();

    await user.click(
      row(table, "Положение о командировках").getByRole("button", { name: "Переместить" }),
    );
    const dialog = await screen.findByRole("dialog", { name: "Переместить документ" });
    const move = within(dialog).getByRole("button", { name: "Переместить" });
    expect(move).toBeDisabled();
    await user.selectOptions(within(dialog).getByLabelText("Папка"), "Кадры");
    expect(
      within(dialog).getByText(
        "Документ увидят только администраторы и отделы: Кадры, Бухгалтерия.",
      ),
    ).toBeInTheDocument();
    await user.click(move);

    expect(await screen.findByText("Перенесён в папку «Кадры»")).toBeInTheDocument();
    expect(bodies).toEqual([
      { method: "PATCH", path: "/materials/m-1", body: { folder_id: "f-hr" } },
    ]);
    await waitFor(() =>
      expect(
        row(table, "Положение о командировках").getByRole("img", { name: "доступ ограничен" }),
      ).toBeInTheDocument(),
    );
    await waitFor(() => expect(tile(/^Кадры/)).toHaveTextContent("1 документ"));
  });

  it("непустую папку не удаляет — показывает ответ сервера; пустую удаляет", async () => {
    const user = userEvent.setup();
    mockSources({
      materials: [material({ id: "m-2", title: "Штатное расписание", folder_id: "f-hr" })],
      folders: [
        folder(),
        folder({ id: "f-docs", name: "Договоры", restricted: false, departments: [] }),
      ],
    });
    const { router } = renderApp("/admin/sources/files?folder=f-docs");

    await screen.findByText("В папке «Договоры» пока пусто");
    await user.click(screen.getByRole("button", { name: "Действия: Кадры" }));
    await user.click(await screen.findByRole("menuitem", { name: "Удалить папку" }));
    let confirm = await screen.findByRole("dialog", { name: "Удалить папку?" });
    await user.click(within(confirm).getByRole("button", { name: "Удалить" }));
    expect(await within(confirm).findByRole("alert")).toHaveTextContent(
      "В папке есть документы — сначала перенесите или удалите их",
    );
    await user.click(within(confirm).getByRole("button", { name: "Отмена" }));
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(tile(/^Кадры/)).toBeInTheDocument();

    // Удалили открытую папку — открываются все файлы.
    await user.click(screen.getByRole("button", { name: "Действия: Договоры" }));
    await user.click(await screen.findByRole("menuitem", { name: "Удалить папку" }));
    confirm = await screen.findByRole("dialog", { name: "Удалить папку?" });
    await user.click(within(confirm).getByRole("button", { name: "Удалить" }));
    await waitFor(() =>
      expect(
        within(screen.getByRole("group", { name: "Папки" })).queryByRole("button", {
          name: /^Договоры/,
        }),
      ).not.toBeInTheDocument(),
    );
    expect(router.state.location.search).toBe("");
    expect(tile(/^Все файлы/)).toHaveAttribute("aria-pressed", "true");
  });

  it("подключения: сколько сотрудников подключились сами", async () => {
    mockSources();
    mockConnectors([
      connector({ grants_active: 5, members_active: 12 }),
      connector({
        id: "c-2",
        kind: "confluence",
        name: "Confluence",
        mode: "organization",
      }),
    ]);
    renderApp("/admin/sources/connections");

    const card = (await screen.findByText("Битрикс24")).closest("a");
    if (!card) throw new Error("no card");
    expect(card).toHaveAttribute("href", "/admin/sources/connections/c-1");
    expect(card).toHaveTextContent("Подключились 5 из 12 сотрудников");
    const other = screen.getByText("Confluence").closest("a");
    expect(other).not.toHaveTextContent(/Подключил/);
    expect(screen.getByRole("link", { name: "«Настройки → Мои подключения»" })).toHaveAttribute(
      "href",
      "/settings/connections",
    );
    expect(screen.getByText(/Тариф «Расширенный»: подключений 2 из 5/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Добавить подключение" })).toBeInTheDocument();
  });
});
