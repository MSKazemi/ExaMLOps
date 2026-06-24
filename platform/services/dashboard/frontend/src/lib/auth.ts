const KEY = 'dashboard_auth'

export type Role = 'viewer' | 'admin'

export interface AuthBlob {
  token: string
  role: Role
  expiresAt: string  // ISO8601
}

export const getAuth = (): AuthBlob | null => {
  const raw = localStorage.getItem(KEY)
  if (!raw) return null
  try {
    const blob = JSON.parse(raw) as AuthBlob
    if (new Date(blob.expiresAt).getTime() <= Date.now()) {
      localStorage.removeItem(KEY)
      return null
    }
    return blob
  } catch {
    localStorage.removeItem(KEY)
    return null
  }
}

export const setAuth = (blob: AuthBlob): void => {
  localStorage.setItem(KEY, JSON.stringify(blob))
}

export const clearAuth = (): void => {
  localStorage.removeItem(KEY)
}

export const getToken = (): string | null => getAuth()?.token ?? null
export const getRole = (): Role | null => getAuth()?.role ?? null
export const isAdmin = (): boolean => getRole() === 'admin'

// Back-compat shim used by lib/api.ts during migration; remove once api.ts no
// longer imports clearToken (already updated in this task — kept only as a
// safety net if any other file still imports it).
export const clearToken = clearAuth
