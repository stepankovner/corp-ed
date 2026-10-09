import { afterEach, describe, expect, it, vi } from "vitest";

import {
  INVITE_TTL_MS,
  clearPendingInvite,
  pendingInvite,
  savePendingInvite,
} from "./pendingInvite";

const SECRET = "tok_0123456789abcdef";

afterEach(() => {
  vi.useRealTimers();
});

describe("приглашение, открытое до входа", () => {
  it("живёт в памяти страницы, а не в хранилище браузера", () => {
    savePendingInvite(SECRET);

    expect(pendingInvite()).toBe(SECRET);
    expect(sessionStorage.length).toBe(0);
    expect(localStorage.length).toBe(0);
  });

  it("стирается после использования", () => {
    savePendingInvite(SECRET);
    clearPendingInvite();
    expect(pendingInvite()).toBeNull();
  });

  it("истекает через 30 минут", () => {
    vi.useFakeTimers();
    savePendingInvite(SECRET);

    vi.advanceTimersByTime(INVITE_TTL_MS - 1000);
    expect(pendingInvite()).toBe(SECRET);
    vi.advanceTimersByTime(2000);
    expect(pendingInvite()).toBeNull();
    expect(INVITE_TTL_MS).toBe(30 * 60 * 1000);
  });

  it("новое приглашение заменяет прежнее и продлевает срок", () => {
    vi.useFakeTimers();
    savePendingInvite("первое");
    vi.advanceTimersByTime(INVITE_TTL_MS - 1000);
    savePendingInvite(SECRET);
    vi.advanceTimersByTime(2000);
    expect(pendingInvite()).toBe(SECRET);
  });

  it("секрет из прежней версии во вкладке убирается при загрузке", async () => {
    sessionStorage.setItem("kronto.invite", SECRET);
    vi.resetModules();
    const fresh = await import("./pendingInvite");

    expect(sessionStorage.getItem("kronto.invite")).toBeNull();
    expect(fresh.pendingInvite()).toBeNull();
  });
});
