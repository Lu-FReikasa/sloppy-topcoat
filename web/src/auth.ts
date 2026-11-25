// Dev-token storage. localStorage may be unavailable (private mode, blocked
// storage), so every access is guarded.

export const TOKEN_KEY = "topcoat.devToken";

export function loadToken(): string | null {
  try {
    return window.localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function saveToken(token: string): void {
  try {
    window.localStorage.setItem(TOKEN_KEY, token);
  } catch {
    // ignore: the token then only lives for this page load
  }
}

export function clearToken(): void {
  try {
    window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    // ignore
  }
}
