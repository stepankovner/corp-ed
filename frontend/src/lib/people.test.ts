import { describe, expect, it } from "vitest";

import { formatPhone, fullNameWithPatronymic, shortName, telegramUrl } from "./people";

describe("профиль: отображение", () => {
  it("российский номер — с пробелами и дефисами, остальные как есть", () => {
    expect(formatPhone("+79991234567")).toBe("+7 999 123-45-67");
    expect(formatPhone("+375291234567")).toBe("+375291234567");
  });

  it("имя полностью и коротко; без имени — почта", () => {
    const anna = {
      first_name: "Анна",
      last_name: "Смирнова",
      patronymic: "Сергеевна",
      email: "anna@x.ru",
    };
    expect(fullNameWithPatronymic(anna)).toBe("Смирнова Анна Сергеевна");
    expect(shortName(anna)).toBe("Анна Смирнова");
    expect(shortName({ first_name: null, last_name: null, email: "a@x.ru" })).toBe("a@x.ru");
  });

  it("ссылка на Telegram", () => {
    expect(telegramUrl("anna_s")).toBe("https://t.me/anna_s");
  });
});
