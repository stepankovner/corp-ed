import { describe, expect, it } from "vitest";

import indexHtml from "../../index.html?raw";
import nginxConf from "../../nginx.conf?raw";

async function sha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return btoa(String.fromCharCode(...new Uint8Array(digest)));
}

describe("CSP статики", () => {
  it("разрешает встроенный скрипт темы из index.html по его хешу", async () => {
    const scripts = [...indexHtml.matchAll(/<script>([\s\S]*?)<\/script>/g)].map(
      (match) => match[1] ?? "",
    );
    expect(scripts).toHaveLength(1);
    const policies = nginxConf.match(/script-src [^;]+;/g) ?? [];
    expect(policies).toHaveLength(2);
    for (const script of scripts) {
      const hash = `'sha256-${await sha256(script)}'`;
      for (const policy of policies) {
        expect(policy, `впишите ${hash} в script-src nginx.conf`).toContain(hash);
      }
    }
  });

  it("запрет фрейма и для браузеров без frame-ancestors — там же, где CSP", () => {
    // add_header в location отменяет заголовки сервера: каждый набор
    // заголовков с CSP несёт и X-Frame-Options.
    const policies = nginxConf.match(/frame-ancestors 'none'/g) ?? [];
    const frameOptions = nginxConf.match(/add_header X-Frame-Options "DENY" always;/g) ?? [];
    expect(policies).toHaveLength(2);
    expect(frameOptions).toHaveLength(policies.length);
  });
});
