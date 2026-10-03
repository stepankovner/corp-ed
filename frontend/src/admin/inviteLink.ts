/**
 * Ссылка на вступление: токен после «#» не уходит на сервер, в журналы и
 * в Referer. Кода компании в адресе нет — компанию знает само приглашение.
 */
export function inviteLink(token: string, origin: string = window.location.origin): string {
  return `${origin}/join#${token}`;
}
