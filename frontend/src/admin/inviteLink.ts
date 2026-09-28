/** Ссылка на присоединение: токен после «#» не уходит на сервер и в журналы. */
export function inviteLink(
  companyCode: string,
  token: string,
  origin: string = window.location.origin,
): string {
  return `${origin}/join/${encodeURIComponent(companyCode)}#${token}`;
}
