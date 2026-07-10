// Feature-flag seam (F23 R7 / groundwork for F25 / ADR 0052).
//
// New surfaces ship behind a flag so they can be rolled out (or killed) without a redeploy. This is
// the minimal, dependency-free seam: a typed flag registry with a default, overridable at runtime
// via `localStorage` (`flag:<name>`) or a build-time env (`VITE_FLAG_<NAME>`). The full staged
// rollout / targeting engine is F25 — this is the read side every new page uses.

export type FlagName = 'mlopsConsole' | 'facilityConsole' | 'commandPalette'

interface FlagDef {
  default: boolean
  description: string
}

export const FLAGS: Record<FlagName, FlagDef> = {
  mlopsConsole: { default: true, description: 'F9 MLOps console' },
  facilityConsole: { default: true, description: 'F6 exascale facility console' },
  commandPalette: { default: true, description: 'F2 ⌘K command palette' },
}

/** Resolve a flag: localStorage override → env override → registry default. Pure given its inputs. */
export function resolveFlag(
  name: FlagName,
  env: Record<string, string | undefined> = {},
  storage: Pick<Storage, 'getItem'> | null = null,
): boolean {
  const stored = storage?.getItem(`flag:${name}`)
  if (stored === 'true') return true
  if (stored === 'false') return false
  const envVal = env[`VITE_FLAG_${name.toUpperCase()}`]
  if (envVal === 'true') return true
  if (envVal === 'false') return false
  return FLAGS[name].default
}

/** Runtime flag check (reads `import.meta.env` + `localStorage`). */
export function isEnabled(name: FlagName): boolean {
  const env = (import.meta as unknown as { env?: Record<string, string | undefined> }).env ?? {}
  const storage = typeof localStorage !== 'undefined' ? localStorage : null
  return resolveFlag(name, env, storage)
}
