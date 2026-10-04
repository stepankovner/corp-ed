import { describe, expect, it } from "vitest";

import { matches, searchable, stem, textOf } from "./search";

describe("поиск по статьям", () => {
  it("находит слово в другой форме", () => {
    expect(stem("файлы")).toBe("файл");
    expect(stem("приглашения")).toBe("приглашен");
    expect(stem("пароли")).toBe("парол");
    expect(stem("вопросов")).toBe("вопрос");
    expect(stem("код")).toBe("код");
    const text = searchable(
      "Пригласите сотрудников: ссылка и код приглашения. Пароль — в настройках.",
    );
    expect(matches(text, "приглашение")).toBe(true);
    expect(matches(text, "Пароли")).toBe(true);
    expect(matches(text, "код ссылки")).toBe(true);
  });

  it("ищет с начала слова, все слова запроса, «ё» как «е»", () => {
    const text = searchable("Подключите свой аккаунт. Ещё один шаг.");
    expect(matches(text, "ключ")).toBe(false);
    expect(matches(text, "подключить аккаунт")).toBe(true);
    expect(matches(text, "подключить телефон")).toBe(false);
    expect(matches(text, "еще")).toBe(true);
    expect(matches(text, "  ")).toBe(true);
  });

  it("собирает текст из разметки", () => {
    expect(
      textOf(
        <p>
          Откройте{" "}
          <a href="/settings">
            Настройки → <strong>Безопасность</strong>
          </a>{" "}
          и {5} кодов
        </p>,
      ),
    ).toContain("Безопасность");
  });
});
