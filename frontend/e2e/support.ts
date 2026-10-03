import { createHmac } from "node:crypto";

/**
 * Помощники сквозных проверок: коды приложения-аутентификатора (TOTP,
 * RFC 6238) и письма из перехватчика Mailpit (CI, задание e2e).
 */

export function env(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is required (see playwright.config.ts)`);
  return value;
}

const BASE32 = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";

function base32Decode(secret: string): Buffer {
  let bits = "";
  for (const char of secret.replace(/=+$/, "").toUpperCase()) {
    const index = BASE32.indexOf(char);
    if (index < 0) throw new Error("TOTP secret is not base32");
    bits += index.toString(2).padStart(5, "0");
  }
  const bytes: number[] = [];
  for (let i = 0; i + 8 <= bits.length; i += 8) bytes.push(parseInt(bits.slice(i, i + 8), 2));
  return Buffer.from(bytes);
}

export function totpAt(secret: string, step: number): string {
  const counter = Buffer.alloc(8);
  counter.writeBigUInt64BE(BigInt(step));
  const digest = createHmac("sha1", base32Decode(secret)).update(counter).digest();
  const offset = (digest[digest.length - 1] ?? 0) & 0x0f;
  const value = digest.readUInt32BE(offset) & 0x7fffffff;
  return String(value % 1_000_000).padStart(6, "0");
}

/**
 * Сервер принимает шаг до и после текущего, но один шаг — только раз.
 * Берём самый ранний ещё не использованный; кончились — ждём следующий.
 */
let lastStep = 0;
export async function nextTotp(secret: string): Promise<string> {
  for (;;) {
    const current = Math.floor(Date.now() / 30_000);
    const step = Math.max(current - 1, lastStep + 1);
    if (step <= current + 1) {
      lastStep = step;
      return totpAt(secret, step);
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
}

interface MailSummary {
  ID: string;
  Subject: string;
  Created: string;
}

/**
 * Последнее письмо на адрес после момента since (мс). Письма отправляет
 * воркер из очереди — ждём до 30 секунд.
 */
export async function waitForMail(
  to: string,
  since: number,
  subject?: RegExp,
): Promise<{ subject: string; text: string }> {
  const api = env("MAILPIT_URL");
  const deadline = Date.now() + 30_000;
  while (Date.now() < deadline) {
    const query = encodeURIComponent(`to:"${to}"`);
    const response = await fetch(`${api}/api/v1/search?query=${query}&limit=20`);
    if (response.ok) {
      const { messages } = (await response.json()) as { messages: MailSummary[] };
      const fresh = messages.find(
        (message) =>
          Date.parse(message.Created) >= since - 1_000 &&
          (!subject || subject.test(message.Subject)),
      );
      if (fresh) {
        const full = await fetch(`${api}/api/v1/message/${fresh.ID}`);
        const body = (await full.json()) as { Subject: string; Text: string };
        return { subject: body.Subject, text: body.Text };
      }
    }
    await new Promise((resolve) => setTimeout(resolve, 1_000));
  }
  throw new Error(`no mail to ${to}${subject ? ` matching ${String(subject)}` : ""}`);
}

export function codeFrom(text: string): string {
  const match = /\b(\d{6})\b/.exec(text);
  if (!match?.[1]) throw new Error("no 6-digit code in the mail");
  return match[1];
}

export function linkFrom(text: string, path: string): string {
  const match = new RegExp(`https?://\\S+${path}#token=[\\w-]+`).exec(text);
  if (!match) throw new Error(`no ${path} link in the mail`);
  return match[0];
}
