import { readFile } from "node:fs/promises";
import { createServer, type ServerResponse } from "node:http";
import { dirname, extname, resolve, sep } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

import type { components } from "../src/api/schema";
import { conversation, question, reply, sse, summary } from "../src/test/chat";
import { me, tokens } from "../src/test/fixtures";

type Attachment = components["schemas"]["AttachmentResponse"];
type Message = components["schemas"]["MessageResponse"];

/**
 * Изолированная проверка собранного UI в настоящем браузере. HTTP/SSE и
 * multipart настоящие, API — локальная заглушка, без компании и секретов.
 * Запуск: npm run build && npx playwright test chat-draft.spec.ts
 */
async function fakeApp() {
  const dist = resolve(dirname(fileURLToPath(import.meta.url)), "../dist");
  let current = conversation();
  let pending: { response: ServerResponse; answer: Message } | null = null;
  const asked: { question: string; parent_id: string; attachment_ids: string[] }[] = [];
  const uploaded: Attachment[] = [];
  const unexpected: string[] = [];

  function finish(status: "complete" | "stopped", disconnect = false) {
    if (!pending) throw new Error("No pending answer");
    const { response, answer } = pending;
    pending = null;
    const finished = { ...answer, status, content: "Начало ответа" };
    current = {
      ...current,
      messages: current.messages.map((m) => (m.id === answer.id ? finished : m)),
    };
    if (disconnect) response.destroy();
    else response.end(sse([{ type: "done", answer: finished, diagnostics: null }]));
  }

  const server = createServer((request, response) => {
    void (async () => {
      const path = new URL(request.url ?? "/", "http://localhost").pathname;
      const json = (value: unknown, status = 200) => {
        response.writeHead(status, { "Content-Type": "application/json" });
        response.end(JSON.stringify(value));
      };
      if (path === "/api/v1/auth/refresh") return json(tokens());
      if (path === "/api/v1/auth/me") return json(me());
      if (path === "/api/v1/conversations") return json({ items: [summary()], next_before: null });
      if (path === "/api/v1/conversations/c-1") return json(current);
      if (path === "/api/v1/connectors/mine") return json([]);
      if (path === "/api/v1/sources/mine") return json({ files: [], connectors: [] });
      if (path === "/api/v1/notifications") return json({ items: [], unread: 0 });
      if (path === "/api/v1/suggestions") return json({ company: [], frequent: [] });
      if (path === "/api/v1/onboarding")
        return json({
          documents: true,
          people: true,
          question: true,
          tips_seen: true,
          checklist_hidden: true,
        });
      if (path.endsWith("/stop")) {
        finish("stopped");
        response.writeHead(204).end();
        return;
      }
      if (path === "/api/v1/attachments" || path === "/api/v1/conversations/c-1/messages") {
        const chunks: Uint8Array[] = [];
        for await (const chunk of request) {
          if (!(chunk instanceof Uint8Array)) throw new Error("Unexpected HTTP body chunk");
          chunks.push(chunk);
        }
        const body = Buffer.concat(chunks).toString("utf8");
        if (path.endsWith("/attachments")) {
          const filename = /filename="([^"]+)"/.exec(body)?.[1];
          if (!request.headers["content-type"]?.startsWith("multipart/form-data") || !filename) {
            unexpected.push("Invalid multipart upload");
            return json({ detail: "Expected a file" }, 422);
          }
          const attachment = { id: `file-${uploaded.length + 1}`, filename, size: 4, tokens: 2 };
          uploaded.push(attachment);
          return json(attachment, 201);
        }
        const draft = JSON.parse(body) as (typeof asked)[number];
        asked.push(draft);
        const n = asked.length + 1;
        const askedQuestion = question({
          id: `q-${n}`,
          parent_id: draft.parent_id,
          content: draft.question,
          attachments: uploaded.filter((file) => draft.attachment_ids.includes(file.id)),
        });
        const answer = reply({
          id: `a-${n}`,
          parent_id: askedQuestion.id,
          status: "generating",
          content: "",
          sources: [],
          siblings: [`a-${n}`],
        });
        current = conversation([...current.messages, askedQuestion, answer]);
        response.writeHead(200, {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
        });
        response.write(
          sse([{ type: "start", conversation: summary(), question: askedQuestion, answer }]),
        );
        pending = { response, answer };
        response.write(sse([{ type: "delta", text: "Начало ответа" }]));
        if (asked.length > 1) finish("complete");
        return;
      }
      if (path.startsWith("/api/")) {
        unexpected.push(`${request.method} ${path}`);
        return json({ detail: "Unexpected test request" }, 404);
      }
      const file = resolve(
        dist,
        path.startsWith("/c/") ? "app.html" : `.${decodeURIComponent(path)}`,
      );
      if (!file.startsWith(`${dist}${sep}`)) return json({ detail: "Not found" }, 404);
      const types: Record<string, string> = {
        ".html": "text/html",
        ".js": "text/javascript",
        ".css": "text/css",
        ".woff2": "font/woff2",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
      };
      const body = await readFile(file);
      response.writeHead(200, {
        "Content-Type": types[extname(file)] ?? "application/octet-stream",
      });
      response.end(body);
    })().catch((error: unknown) => {
      unexpected.push(String(error));
      response.writeHead(500).end();
    });
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") throw new Error("No test server port");
  return {
    url: `http://127.0.0.1:${address.port}`,
    asked,
    uploaded,
    unexpected,
    finish,
    close: async () => {
      server.closeAllConnections();
      await new Promise<void>((resolve, reject) =>
        server.close((error) => (error ? reject(error) : resolve())),
      );
    },
  };
}

for (const ending of ["complete", "stop", "disconnect"] as const) {
  test(`следующий черновик с файлом сохраняется: ${ending}`, async ({ page }, testInfo) => {
    const app = await fakeApp();
    const errors: string[] = [];
    page.on("pageerror", (error) => errors.push(error.message));
    try {
      await page.addInitScript(() => localStorage.setItem("kronto.signedIn", "1"));
      await page.goto(`${app.url}/c/c-1`);
      const field = page.getByLabel("Ваш вопрос");
      const composer = page.locator("form").filter({ has: field });
      const upload = page.locator('input[type="file"]');
      await field.fill("Первый вопрос");
      await upload.setInputFiles({
        name: "first.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("text"),
      });
      await expect.poll(() => app.uploaded.length).toBe(1);
      await expect(composer.getByText("Читаю файл…")).toHaveCount(0);
      await page.getByRole("button", { name: "Отправить вопрос" }).click();
      await expect(field).toHaveValue("");
      await expect(page.getByRole("button", { name: "Остановить ответ" })).toBeVisible();

      await field.fill("Следующий вопрос");
      await upload.setInputFiles({
        name: "next.txt",
        mimeType: "text/plain",
        buffer: Buffer.from("next"),
      });
      await expect.poll(() => app.uploaded.length).toBe(2);
      await expect(composer.getByText("Читаю файл…")).toHaveCount(0);
      await page.screenshot({ path: testInfo.outputPath("pending-draft.png") });
      if (ending === "stop") await page.getByRole("button", { name: "Остановить ответ" }).click();
      else app.finish("complete", ending === "disconnect");

      await expect(page.getByRole("button", { name: "Отправить вопрос" })).toBeEnabled();
      await expect(field).toHaveValue("Следующий вопрос");
      await expect(composer.getByText("next.txt", { exact: true })).toBeVisible();
      await expect(composer.getByText("first.txt", { exact: true })).toHaveCount(0);
      if (ending === "stop") await expect(page.getByText("Ответ остановлен.")).toBeVisible();
      await page.getByRole("button", { name: "Отправить вопрос" }).click();
      await expect.poll(() => app.asked.length).toBe(2);
      expect(app.asked).toEqual([
        { question: "Первый вопрос", parent_id: "a-1", attachment_ids: ["file-1"] },
        { question: "Следующий вопрос", parent_id: "a-2", attachment_ids: ["file-2"] },
      ]);
      await expect(field).toHaveValue("");
      expect(app.unexpected).toEqual([]);
      expect(errors).toEqual([]);
    } finally {
      await app.close();
    }
  });
}
