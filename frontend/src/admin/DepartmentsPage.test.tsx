import { screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { adminMe } from "../test/fixtures";
import { renderApp } from "../test/render";
import { server } from "../test/server";

describe("отделы", () => {
  it("добавляет, переименовывает и удаляет с предупреждением о людях", async () => {
    const user = userEvent.setup();
    let departments = [{ id: "d-1", name: "Продажи", members: 2 }];
    const bodies: unknown[] = [];
    server.use(
      http.get("/api/v1/auth/me", () => HttpResponse.json(adminMe())),
      http.get("/api/v1/usage", () => HttpResponse.json({ warning: false })),
      http.get("/api/v1/departments", () => HttpResponse.json(departments)),
      http.post("/api/v1/departments", async ({ request }) => {
        const body = (await request.json()) as { name: string };
        bodies.push(body);
        departments = [...departments, { id: "d-2", name: body.name, members: 0 }];
        return HttpResponse.json(departments.at(-1), { status: 201 });
      }),
      http.patch("/api/v1/departments/:id", async ({ request }) => {
        const body = (await request.json()) as { name: string };
        bodies.push(body);
        departments = departments.map((d) => (d.id === "d-1" ? { ...d, name: body.name } : d));
        return HttpResponse.json(departments[0]);
      }),
      http.delete("/api/v1/departments/:id", ({ params }) => {
        departments = departments.filter((d) => d.id !== params.id);
        return new HttpResponse(null, { status: 204 });
      }),
    );
    renderApp("/admin/departments");

    const table = await screen.findByRole("table", { name: "Отделы" });
    expect(within(table).getByText("Продажи")).toBeInTheDocument();
    expect(document.title).toBe("Отделы — kronto");

    await user.click(screen.getByRole("button", { name: "Добавить отдел" }));
    await user.type(screen.getByLabelText("Название"), "  Отдел   кадров ");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    expect(await within(table).findByText("Отдел кадров")).toBeInTheDocument();

    const sales = within(table).getByText("Продажи").closest("tr");
    if (!sales) throw new Error("no row");
    await user.click(within(sales).getByRole("button", { name: "Переименовать" }));
    const name = screen.getByLabelText("Название");
    await user.clear(name);
    await user.type(name, "Коммерческий отдел");
    await user.click(screen.getByRole("button", { name: "Сохранить" }));
    expect(await within(table).findByText("Коммерческий отдел")).toBeInTheDocument();
    expect(bodies).toEqual([{ name: "Отдел кадров" }, { name: "Коммерческий отдел" }]);

    const renamed = within(table).getByText("Коммерческий отдел").closest("tr");
    if (!renamed) throw new Error("no row");
    await user.click(within(renamed).getByRole("button", { name: "Удалить" }));
    const confirm = screen.getByRole("dialog", { name: "Удалить отдел?" });
    expect(within(confirm).getByText(/уберётся у 2 сотрудников/)).toBeInTheDocument();
    await user.click(within(confirm).getByRole("button", { name: "Удалить" }));
    await screen.findByText("Отдел кадров");
    expect(within(table).queryByText("Коммерческий отдел")).not.toBeInTheDocument();
  });

  it("сотрудника в отделы не пускает", async () => {
    server.use(
      http.get("/api/v1/auth/me", () =>
        HttpResponse.json(
          adminMe({
            company: {
              tenant_id: "t-1",
              member_id: "m-1",
              name: "ООО «Меридиан Строй»",
              role: "employee",
              position: null,
              department: null,
            },
          }),
        ),
      ),
    );
    const { router } = renderApp("/admin/departments");
    await screen.findByRole("navigation", { name: "Разделы" });
    expect(router.state.location.pathname).toBe("/");
  });
});
