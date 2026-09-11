import { useQuery } from '@tanstack/react-query'
import { apiFetch } from './api'

// Site feature profile (ADR 0128): which platform modules this centre runs.
//
// The server owns the decision — `GET /api/v1/modules` resolves the site profile and lists the
// page routes of every module that is off, and the API routes of those modules already answer 404
// `module_disabled`. The navigation only has to hide those pages, so this file holds no mapping
// of its own: the module → page table lives in the platform's module catalog, next to the CLI
// commands and services the same module owns.

export interface ModuleRow {
  id: string
  title: string
  description: string
  enabled: boolean
  why: string
  flags: string[]
  api: string[]
  pages: string[]
}

export interface ModulesView {
  available: boolean
  preset?: string
  spec?: string
  sources?: string[]
  warnings?: string[]
  modules: ModuleRow[]
  disabled_pages: string[]
}

export const useModules = () =>
  useQuery<ModulesView>({
    queryKey: ['modules'],
    queryFn: () => apiFetch<ModulesView>('/api/v1/modules'),
    staleTime: 30_000,
  })

/**
 * Whether a page may be shown: false only when `path` is, or sits under, a page of a module the
 * site switched off. An unknown or missing profile allows everything — the platform's default
 * is every module on, and the server still refuses a disabled module's API either way. Pure.
 */
export function pageAllowed(path: string, disabledPages: readonly string[] | undefined): boolean {
  if (!disabledPages || disabledPages.length === 0) return true
  return !disabledPages.some((page) => path === page || path.startsWith(`${page}/`))
}
