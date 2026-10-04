/** Ключи запросов настроек: учётка, а не компания — при её смене не меняются. */
export const SECURITY_KEY = ["account", "security"] as const;
export const SESSIONS_KEY = ["account", "sessions"] as const;
export const REQUESTS_KEY = ["account", "company-requests"] as const;
