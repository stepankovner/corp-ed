/**
 * Ключи доступа (WebAuthn, ТЗ §3). Сервер отдаёт параметры в JSON
 * (py_webauthn, двоичные поля — base64url), браузеру нужны ArrayBuffer.
 * Ответ браузера сериализуем обратно в JSON того же вида.
 *
 * Свои преобразования, а не PublicKeyCredential.parse*FromJSON: этих
 * методов нет в Safari до 18.4 и в части браузеров на Android.
 */

type Json = Record<string, unknown>;

export function passkeysSupported(): boolean {
  return (
    typeof window !== "undefined" &&
    typeof window.PublicKeyCredential === "function" &&
    "credentials" in navigator
  );
}

export function fromBase64Url(value: string): ArrayBuffer {
  const base64 = value.replace(/-/g, "+").replace(/_/g, "/");
  const binary = atob(base64.padEnd(base64.length + ((4 - (base64.length % 4)) % 4), "="));
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i += 1) bytes[i] = binary.charCodeAt(i);
  return bytes.buffer;
}

export function toBase64Url(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

interface Descriptor {
  id: string;
  type: "public-key";
  transports?: AuthenticatorTransport[];
}

function descriptors(list: unknown): PublicKeyCredentialDescriptor[] | undefined {
  if (!Array.isArray(list)) return undefined;
  return (list as Descriptor[]).map((item) => ({ ...item, id: fromBase64Url(item.id) }));
}

/** Человек закрыл окно браузера, вышло время или ключ не подошёл. */
export class PasskeyError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PasskeyError";
  }
}

function explain(error: unknown, creating: boolean): PasskeyError {
  const name = error instanceof DOMException ? error.name : "";
  if (name === "NotAllowedError" || name === "AbortError") {
    return new PasskeyError(
      "Ключ не подтверждён: окно закрыто или вышло время. Попробуйте ещё раз.",
    );
  }
  if (creating && name === "InvalidStateError") {
    return new PasskeyError("Этот ключ уже добавлен к учётной записи.");
  }
  if (name === "SecurityError") {
    return new PasskeyError("Браузер не разрешил ключ на этом адресе. Откройте сайт по https.");
  }
  return new PasskeyError("Не получилось использовать ключ доступа. Попробуйте другой способ.");
}

/** Новый ключ: параметры /account/passkeys/options → ответ для /account/passkeys. */
export async function createPasskey(options: Json): Promise<Json> {
  const user = options.user as Json;
  const publicKey = {
    ...options,
    challenge: fromBase64Url(options.challenge as string),
    user: { ...user, id: fromBase64Url(user.id as string) },
    excludeCredentials: descriptors(options.excludeCredentials) ?? [],
  } as unknown as PublicKeyCredentialCreationOptions;
  let credential: Credential | null;
  try {
    credential = await navigator.credentials.create({ publicKey });
  } catch (error) {
    throw explain(error, true);
  }
  if (!(credential instanceof PublicKeyCredential)) throw explain(null, true);
  const response = credential.response as AuthenticatorAttestationResponse;
  return {
    id: credential.id,
    rawId: toBase64Url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: toBase64Url(response.clientDataJSON),
      attestationObject: toBase64Url(response.attestationObject),
      transports: response.getTransports(),
    },
    clientExtensionResults: credential.getClientExtensionResults(),
    authenticatorAttachment: credential.authenticatorAttachment ?? undefined,
  };
}

/** Вход ключом: параметры /auth/mfa/passkey-options → ответ для /auth/mfa/verify. */
export async function requestPasskey(options: Json): Promise<Json> {
  const publicKey = {
    ...options,
    challenge: fromBase64Url(options.challenge as string),
    allowCredentials: descriptors(options.allowCredentials),
  } as unknown as PublicKeyCredentialRequestOptions;
  let credential: Credential | null;
  try {
    credential = await navigator.credentials.get({ publicKey });
  } catch (error) {
    throw explain(error, false);
  }
  if (!(credential instanceof PublicKeyCredential)) throw explain(null, false);
  const response = credential.response as AuthenticatorAssertionResponse;
  return {
    id: credential.id,
    rawId: toBase64Url(credential.rawId),
    type: credential.type,
    response: {
      clientDataJSON: toBase64Url(response.clientDataJSON),
      authenticatorData: toBase64Url(response.authenticatorData),
      signature: toBase64Url(response.signature),
      userHandle: response.userHandle ? toBase64Url(response.userHandle) : null,
    },
    clientExtensionResults: credential.getClientExtensionResults(),
    authenticatorAttachment: credential.authenticatorAttachment ?? undefined,
  };
}
