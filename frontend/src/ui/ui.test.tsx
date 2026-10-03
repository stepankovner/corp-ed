import { act, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { companyInitials, personInitials, tileIndex } from "../lib/initials";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from "./DropdownMenu";
import { IconButton } from "./IconButton";
import { SegmentedControl } from "./SegmentedControl";
import { SkeletonList } from "./Skeleton";
import { Switch } from "./Switch";
import { Tabs } from "./Tabs";
import { UiProvider } from "./UiProvider";
import { useToast } from "./useToast";

describe("инициалы и цвет плитки", () => {
  it("убирают форму собственности и кавычки из названия компании", () => {
    expect(companyInitials("ООО «Меридиан Строй»")).toBe("МС");
    expect(companyInitials('АО "Ромашка"')).toBe("Ро");
    expect(companyInitials("QA")).toBe("QA");
    expect(companyInitials("  ")).toBe("?");
  });

  it("берут имя и фамилию, без имени — почту", () => {
    expect(personInitials("Анна Смирнова", "anna@x.ru")).toBe("АС");
    expect(personInitials(null, "pavel.orlov@x.ru")).toBe("PO");
  });

  it("цвет плитки один и тот же для одного названия", () => {
    const first = tileIndex("ООО «Меридиан Строй»");
    expect(tileIndex("ООО «Меридиан Строй»")).toBe(first);
    expect(first).toBeGreaterThanOrEqual(1);
    expect(first).toBeLessThanOrEqual(8);
  });
});

describe("вкладки", () => {
  it("переключаются стрелками, Home и End, в порядке табуляции одна вкладка", async () => {
    const user = userEvent.setup();
    render(
      <Tabs
        label="Источники"
        items={[
          { value: "files", label: "Файлы", content: <p>Список файлов</p> },
          { value: "links", label: "Подключения", content: <p>Список подключений</p> },
          { value: "mine", label: "Мои", content: <p>Мои подключения</p> },
        ]}
      />,
    );
    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.tabIndex)).toEqual([0, -1, -1]);
    expect(screen.getByRole("tabpanel")).toHaveTextContent("Список файлов");

    await user.click(tabs[0]!);
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Подключения" })).toHaveAttribute(
      "aria-selected",
      "true",
    );
    expect(screen.getByRole("tab", { name: "Подключения" })).toHaveFocus();
    expect(screen.getByRole("tabpanel", { name: "Подключения" })).toHaveTextContent(
      "Список подключений",
    );

    await user.keyboard("{End}");
    expect(screen.getByRole("tab", { name: "Мои" })).toHaveFocus();
    await user.keyboard("{ArrowRight}");
    expect(screen.getByRole("tab", { name: "Файлы" })).toHaveAttribute("aria-selected", "true");
  });
});

describe("переключатель", () => {
  it("меняет состояние по щелчку на подписи", async () => {
    const user = userEvent.setup();
    function Demo() {
      const [on, setOn] = useState(false);
      return <Switch checked={on} onCheckedChange={setOn} label="Письма о входе" hint="На почту" />;
    }
    render(<Demo />);
    const control = screen.getByRole("switch", { name: "Письма о входе" });
    expect(control).toHaveAttribute("aria-checked", "false");
    expect(control).toHaveAccessibleDescription("На почту");
    await user.click(screen.getByText("Письма о входе"));
    expect(control).toHaveAttribute("aria-checked", "true");
  });
});

describe("сегменты", () => {
  it("отмечают выбранный вариант и сообщают о выборе", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <SegmentedControl
        label="Статус"
        value="all"
        onChange={onChange}
        options={[
          { value: "all", label: "Все", count: 4 },
          { value: "failed", label: "Ошибки", count: 0 },
        ]}
      />,
    );
    const group = screen.getByRole("group", { name: "Статус" });
    expect(within(group).getByRole("button", { name: "Все 4" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    await user.click(within(group).getByRole("button", { name: "Ошибки 0" }));
    expect(onChange).toHaveBeenCalledWith("failed");
  });
});

describe("заглушки загрузки", () => {
  it("озвучиваются одним сообщением", () => {
    render(<SkeletonList label="Загрузка документов" rows={3} />);
    expect(screen.getByRole("status", { name: "Загрузка документов" })).toBeInTheDocument();
  });
});

describe("уведомления", () => {
  function Trigger() {
    const toast = useToast();
    return (
      <button type="button" onClick={() => toast.show("Ответ скопирован")}>
        Показать
      </button>
    );
  }

  it("появляются в области aria-live и закрываются сами", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const user = userEvent.setup({ advanceTimers: vi.advanceTimersByTime });
    render(
      <UiProvider>
        <Trigger />
      </UiProvider>,
    );
    const region = screen.getByRole("status");
    expect(region).toHaveAttribute("aria-live", "polite");
    await user.click(screen.getByRole("button", { name: "Показать" }));
    expect(region).toHaveTextContent("Ответ скопирован");
    await act(async () => {
      vi.advanceTimersByTime(4100);
    });
    expect(region).toBeEmptyDOMElement();
    vi.useRealTimers();
  });

  it("закрываются кнопкой", async () => {
    const user = userEvent.setup();
    render(
      <UiProvider>
        <Trigger />
      </UiProvider>,
    );
    await user.click(screen.getByRole("button", { name: "Показать" }));
    await user.click(screen.getByRole("button", { name: "Закрыть уведомление" }));
    expect(screen.getByRole("status")).toBeEmptyDOMElement();
  });

  it("без поставщика — понятная ошибка", () => {
    vi.spyOn(console, "error").mockImplementation(() => undefined);
    expect(() => render(<Trigger />)).toThrow("useToast outside ToastProvider");
  });
});

describe("выпадающее меню и подсказка", () => {
  it("меню открывается кнопкой, пункты-переключатели отмечают выбор", async () => {
    const user = userEvent.setup();
    const onSelect = vi.fn();
    function Demo() {
      const [value, setValue] = useState("light");
      return (
        <UiProvider>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <IconButton label="Тема оформления">☼</IconButton>
            </DropdownMenuTrigger>
            <DropdownMenuContent>
              <DropdownMenuRadioGroup value={value} onValueChange={setValue}>
                <DropdownMenuRadioItem value="light">Светлая</DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="dark">Тёмная</DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
              <DropdownMenuItem onSelect={onSelect}>Выйти</DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </UiProvider>
      );
    }
    render(<Demo />);
    await user.click(screen.getByRole("button", { name: "Тема оформления" }));
    const menu = await screen.findByRole("menu");
    expect(within(menu).getByRole("menuitemradio", { name: "Светлая" })).toHaveAttribute(
      "aria-checked",
      "true",
    );
    await user.click(within(menu).getByRole("menuitemradio", { name: "Тёмная" }));

    await user.click(screen.getByRole("button", { name: "Тема оформления" }));
    expect(
      within(await screen.findByRole("menu")).getByRole("menuitemradio", { name: "Тёмная" }),
    ).toHaveAttribute("aria-checked", "true");
    await user.click(screen.getByRole("menuitem", { name: "Выйти" }));
    expect(onSelect).toHaveBeenCalled();
  });

  it("подсказка кнопки-иконки появляется при фокусе с клавиатуры", async () => {
    const user = userEvent.setup();
    render(
      <UiProvider>
        <IconButton label="Удалить">×</IconButton>
      </UiProvider>,
    );
    await user.tab();
    expect(await screen.findByRole("tooltip")).toHaveTextContent("Удалить");
    expect(screen.getByRole("button", { name: "Удалить" })).toBeInTheDocument();
  });
});
