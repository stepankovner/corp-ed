import { http, HttpResponse } from "msw";
import { describe, expect, it } from "vitest";

import { server } from "../test/server";
import { api, restoreSession, unwrap } from "./client";
import { ApiError } from "./errors";
import { getSession, hasSignedInBefore, setSession } from "./session";

function signIn(expiresIn = 600_000) {
  setSession({ accessToken: "access-1", expiresAt: Date.now() + expiresIn });
}

const refreshed = {
  access_token: "access-2",
  token_type: "bearer",
  expires_in: 900,
};

describe("authFetch", () => {
  it("sends the access token", async () => {
    signIn();
    let seen: string | null = null;
    server.use(
      http.get("/api/v1/usage", ({ request }) => {
        seen = request.headers.get("authorization");
        return HttpResponse.json({ used: 1 });
      }),
    );
    await unwrap(api.GET("/api/v1/usage"));
    expect(seen).toBe("Bearer access-1");
  });

  it("refreshes once on 401 and repeats the request with its body", async () => {
    signIn();
    const refreshes: string[] = [];
    const bodies: unknown[] = [];
    server.use(
      http.post("/api/v1/auth/refresh", async ({ request }) => {
        // Refresh-токен — в httpOnly-cookie, тело пустое.
        refreshes.push(await request.text());
        return HttpResponse.json(refreshed);
      }),
      http.post("/api/v1/faq/ask", async ({ request }) => {
        bodies.push(await request.json());
        if (request.headers.get("authorization") !== "Bearer access-2") {
          return HttpResponse.json({ detail: "expired" }, { status: 401 });
        }
        return HttpResponse.json({ ok: true });
      }),
    );
    await unwrap(api.POST("/api/v1/faq/ask", { body: { question: "Суточные?" } }));
    expect(refreshes).toEqual([""]);
    expect(bodies).toEqual([{ question: "Суточные?" }, { question: "Суточные?" }]);
    expect(getSession()?.accessToken).toBe("access-2");
  });

  it("uses a single refresh for concurrent 401 responses", async () => {
    // Бэкенд отзывает всю цепочку при повторном refresh — второй запрос
    // обновления разлогинил бы пользователя.
    signIn();
    let refreshes = 0;
    server.use(
      http.post("/api/v1/auth/refresh", async () => {
        refreshes += 1;
        await new Promise((resolve) => setTimeout(resolve, 20));
        return HttpResponse.json(refreshed);
      }),
      http.get("/api/v1/usage", ({ request }) =>
        request.headers.get("authorization") === "Bearer access-2"
          ? HttpResponse.json({ ok: true })
          : HttpResponse.json({ detail: "expired" }, { status: 401 }),
      ),
    );
    await Promise.all([
      unwrap(api.GET("/api/v1/usage")),
      unwrap(api.GET("/api/v1/usage")),
      unwrap(api.GET("/api/v1/usage")),
    ]);
    expect(refreshes).toBe(1);
  });

  it("refreshes an expired access token before the request", async () => {
    signIn(-1000);
    const seen: (string | null)[] = [];
    server.use(
      http.post("/api/v1/auth/refresh", () => HttpResponse.json(refreshed)),
      http.get("/api/v1/usage", ({ request }) => {
        seen.push(request.headers.get("authorization"));
        return HttpResponse.json({ ok: true });
      }),
    );
    await unwrap(api.GET("/api/v1/usage"));
    expect(seen).toEqual(["Bearer access-2"]);
  });

  it("drops the session when the refresh token is rejected", async () => {
    signIn();
    server.use(
      http.post("/api/v1/auth/refresh", () =>
        HttpResponse.json({ detail: "revoked" }, { status: 401 }),
      ),
      http.get("/api/v1/usage", () => HttpResponse.json({ detail: "expired" }, { status: 401 })),
    );
    await expect(unwrap(api.GET("/api/v1/usage"))).rejects.toMatchObject({ status: 401 });
    expect(getSession()).toBeNull();
  });

  it("keeps the session on a network failure during refresh", async () => {
    signIn();
    server.use(
      http.post("/api/v1/auth/refresh", () => HttpResponse.error()),
      http.get("/api/v1/usage", () => HttpResponse.json({ detail: "expired" }, { status: 401 })),
    );
    await expect(unwrap(api.GET("/api/v1/usage"))).rejects.toMatchObject({ status: 0 });
    expect(getSession()?.accessToken).toBe("access-1");
  });
});

describe("restoreSession", () => {
  it("gets a fresh access token by the refresh cookie after a reload", async () => {
    let refreshes = 0;
    server.use(
      http.post("/api/v1/auth/refresh", () => {
        refreshes += 1;
        return HttpResponse.json(refreshed);
      }),
    );
    expect(getSession()).toBeNull();
    await expect(restoreSession()).resolves.toBe(true);
    await expect(restoreSession()).resolves.toBe(true);
    expect(refreshes).toBe(1);
    expect(getSession()?.accessToken).toBe("access-2");
    expect(hasSignedInBefore()).toBe(true);
  });

  it("forgets the sign-in mark when the cookie is gone", async () => {
    localStorage.setItem("kronto.signedIn", "1");
    server.use(
      http.post("/api/v1/auth/refresh", () =>
        HttpResponse.json({ detail: "Нет refresh-токена" }, { status: 401 }),
      ),
    );
    await expect(restoreSession()).resolves.toBe(false);
    expect(getSession()).toBeNull();
    expect(hasSignedInBefore()).toBe(false);
  });

  it("never stores tokens in localStorage", () => {
    signIn();
    const stored = Object.keys(localStorage).map((key) => localStorage.getItem(key) ?? "");
    expect(stored.join()).not.toContain("access-1");
  });
});

describe("unwrap", () => {
  it("maps the backend error body", async () => {
    signIn();
    server.use(
      http.post("/api/v1/faq/ask", () =>
        HttpResponse.json({ detail: "Лимит исчерпан", code: "credits_exhausted" }, { status: 402 }),
      ),
    );
    const error = await unwrap(api.POST("/api/v1/faq/ask", { body: { question: "?" } })).catch(
      (e: unknown) => e,
    );
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 402,
      code: "credits_exhausted",
      message: "Лимит исчерпан",
    });
  });

  it("uses a readable message for validation errors", async () => {
    server.use(
      http.post("/api/v1/auth/login", () =>
        HttpResponse.json({ detail: [{ msg: "bad email" }] }, { status: 422 }),
      ),
    );
    const error = await unwrap(
      api.POST("/api/v1/auth/login", { body: { email: "x", password: "x", remember: false } }),
    ).catch((e: unknown) => e);
    expect(error).toMatchObject({ status: 422, message: "Проверьте заполнение полей." });
  });
});
