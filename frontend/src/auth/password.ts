export const MIN_PASSWORD = 12;

/** Проверки, которые сервер всё равно повторит; здесь — чтобы не гонять форму. */
export function passwordProblem(next: string, repeat: string, email: string): string | null {
  if (next.length < MIN_PASSWORD) return `Не короче ${MIN_PASSWORD} символов.`;
  if (next.length > 128) return "Не длиннее 128 символов.";
  const local = email.split("@")[0]?.toLowerCase() ?? "";
  if (local.length >= 3 && next.toLowerCase().includes(local)) {
    return "Пароль не должен содержать почту.";
  }
  if (next !== repeat) return "Пароли не совпадают.";
  return null;
}
