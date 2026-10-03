import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import indexHtml from "../../index.html?raw";

type ThemeModule = typeof import("./theme");

/** Системная тема: matchMedia, которой нет в jsdom, с переключением «на лету». */
function mockSystem(dark: boolean) {
  const listeners = new Set<() => void>();
  const query = {
    matches: dark,
    media: "(prefers-color-scheme: dark)",
    addEventListener: (_: string, listener: () => void) => listeners.add(listener),
    removeEventListener: (_: string, listener: () => void) => listeners.delete(listener),
  };
  window.matchMedia = vi.fn(() => query as unknown as MediaQueryList);
  return {
    change(next: boolean) {
      query.matches = next;
      listeners.forEach((listener) => listener());
    },
  };
}

// Модуль кеширует выбор — для каждого теста свежий.
async function load(): Promise<ThemeModule> {
  vi.resetModules();
  return import("./theme");
}

function themeColor(): string | null {
  return document.querySelector('meta[name="theme-color"]')?.getAttribute("content") ?? null;
}

beforeEach(() => {
  document.head.innerHTML = '<meta name="theme-color" content="#ffffff" />';
  document.documentElement.removeAttribute("data-theme");
});

afterEach(() => {
  vi.restoreAllMocks();
  // @ts-expect-error -- в jsdom matchMedia нет, возвращаем как было.
  delete window.matchMedia;
});

describe("тема оформления", () => {
  it("по умолчанию системная и следует настройке системы", async () => {
    mockSystem(true);
    const theme = await load();
    expect(theme.readPreference()).toBe("system");
    expect(theme.resolveTheme("system")).toBe("dark");
    theme.applyTheme("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(themeColor()).toBe(theme.THEME_COLOR.dark);
  });

  it("без matchMedia считает систему светлой", async () => {
    const theme = await load();
    expect(theme.systemTheme()).toBe("light");
  });

  it("явный выбор перекрывает систему и сохраняется", async () => {
    mockSystem(true);
    const theme = await load();
    theme.setThemePreference("light");
    expect(document.documentElement.dataset.theme).toBe("light");
    expect(themeColor()).toBe(theme.THEME_COLOR.light);
    expect(localStorage.getItem(theme.THEME_KEY)).toBe("light");

    theme.setThemePreference("system");
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(localStorage.getItem(theme.THEME_KEY)).toBeNull();
  });

  it("не падает, когда хранилище бросает исключение", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    const theme = await load();
    expect(theme.readPreference()).toBe("system");
    expect(() => theme.setThemePreference("dark")).not.toThrow();
    // Выбор действует до перезагрузки, хоть и не сохранён.
    expect(document.documentElement.dataset.theme).toBe("dark");
  });

  it("игнорирует мусор в хранилище", async () => {
    localStorage.setItem("kronto.theme", "sepia");
    const theme = await load();
    expect(theme.readPreference()).toBe("system");
  });

  it("хук отражает смену системной темы и выбор пользователя", async () => {
    const system = mockSystem(false);
    const theme = await load();
    const { result } = renderHook(() => theme.useTheme());
    expect(result.current).toMatchObject({ preference: "system", theme: "light" });

    act(() => system.change(true));
    expect(result.current.theme).toBe("dark");
    expect(themeColor()).toBe(theme.THEME_COLOR.dark);

    act(() => result.current.setPreference("light"));
    expect(result.current).toMatchObject({ preference: "light", theme: "light" });
  });

  it("подхватывает выбор из соседней вкладки", async () => {
    const theme = await load();
    const { result } = renderHook(() => theme.useTheme());
    localStorage.setItem(theme.THEME_KEY, "dark");
    act(() => {
      window.dispatchEvent(new StorageEvent("storage", { key: theme.THEME_KEY }));
    });
    expect(result.current.preference).toBe("dark");
    expect(document.documentElement.dataset.theme).toBe("dark");
  });
});

describe("скрипт темы в index.html", () => {
  const script = /<script>([\s\S]*?)<\/script>/.exec(indexHtml)?.[1] ?? "";

  function run() {
    // Тот же код, что выполнит браузер до загрузки приложения (файл репозитория).
    // eslint-disable-next-line @typescript-eslint/no-implied-eval, @typescript-eslint/no-unsafe-call
    new Function(script)();
  }

  it("ставит выбранную тему и цвет полосы, как applyTheme", async () => {
    const theme = await load();
    localStorage.setItem(theme.THEME_KEY, "dark");
    run();
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(themeColor()).toBe(theme.THEME_COLOR.dark);
  });

  it("при системной теме атрибут не ставит, цвет берёт из системы", async () => {
    mockSystem(true);
    const theme = await load();
    run();
    expect(document.documentElement.hasAttribute("data-theme")).toBe(false);
    expect(themeColor()).toBe(theme.THEME_COLOR.dark);
  });

  it("переживает недоступное хранилище", () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new DOMException("denied", "SecurityError");
    });
    expect(run).not.toThrow();
    expect(themeColor()).toBe("#ffffff");
  });
});
