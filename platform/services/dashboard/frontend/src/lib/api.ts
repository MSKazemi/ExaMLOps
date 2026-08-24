import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { clearAuth, getToken, type Role } from './auth'
import { ApiError, parseProblem } from './errors'
import {
  listContainers,
  startContainer,
  stopContainer,
  restartContainer,
} from "./containers"

// `unknown` is not a fourth degree of broken. The backend reports it for the entries it
// never probes — a scheduler it cannot reach from here, a bus whose bridge does not report
// its connection — so the UI can say "not measured" instead of showing a verdict nobody took.
export type ServiceStatus = 'ok' | 'degraded' | 'down' | 'unknown'

export interface ServiceInfo {
  status: ServiceStatus
  url: string
  /** Why this entry is not measured. Present only alongside `unknown`. */
  note?: string
}

export interface HealthResponse {
  status: ServiceStatus
  checked_at: string
  services: Record<string, ServiceInfo>
  /** The keys of `services` that are reported but never probed; they do not move `status`. */
  unmeasured?: string[]
}

export interface ModelInfo {
  model_name: string
  model_version: string | null
  run_id: string | null
  status: string
}

export async function apiFetch<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = getToken()
  const res = await fetch(path, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...init.headers,
    },
  })
  if (res.status === 401 || res.status === 403) {
    clearAuth()
    window.location.reload()
    throw new ApiError(res.status, { title: res.status === 403 ? 'Forbidden' : 'Unauthorized' })
  }
  if (!res.ok) {
    let body: unknown = null
    try {
      body = await res.json()
    } catch { /* non-JSON error body — parseProblem falls back to a generic title */ }
    throw new ApiError(res.status, parseProblem(res.status, body))
  }
  return res.json() as Promise<T>
}

export const useHealth = () =>
  useQuery<HealthResponse>({
    queryKey: ['health'],
    queryFn: () => apiFetch<HealthResponse>('/api/health'),
    refetchInterval: 10_000,
  })

export const useModels = () =>
  useQuery<ModelInfo[]>({
    queryKey: ['models'],
    queryFn: () => apiFetch<ModelInfo[]>('/api/proxy/ray/models'),
  })

export const useConfig = () =>
  useQuery<Record<string, string>>({
    queryKey: ['config'],
    queryFn: () => apiFetch<Record<string, string>>('/api/config'),
  })

export const useUpdateConfig = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (updates: Record<string, string>) =>
      apiFetch<Record<string, string>>('/api/config', {
        method: 'PUT',
        body: JSON.stringify(updates),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['config'] }),
  })
}

export const useReloadRay = () =>
  useMutation({
    mutationFn: () =>
      apiFetch<{ reloaded: string[]; count: number }>('/api/proxy/ray/reload', {
        method: 'POST',
      }),
  })

/** Downloads .env.dashboard from the server as a file. Returns true on success. */
export async function exportEnv(): Promise<void> {
  const token = getToken()
  const res = await fetch('/api/config/export-env', {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
  })
  if (res.status === 401 || res.status === 403) {
    clearAuth()
    window.location.reload()
    return
  }
  if (!res.ok) throw new Error(`Export failed: ${res.status}`)
  const blob = await res.blob()
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = '.env.dashboard'
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  URL.revokeObjectURL(url)
}

export interface ImportEnvResult {
  imported: string[]
  skipped: string[]
}

/** Upload a .env file and import matching keys into the config store. */
export async function importEnv(file: File): Promise<ImportEnvResult> {
  const token = getToken()
  const form = new FormData()
  form.append('file', file)
  const res = await fetch('/api/config/import-env', {
    method: 'POST',
    headers: token ? { Authorization: `Bearer ${token}` } : {},
    body: form,
  })
  if (res.status === 401 || res.status === 403) {
    clearAuth()
    window.location.reload()
    throw new Error('Unauthorized')
  }
  if (!res.ok) throw new Error(`Import failed: ${res.status}`)
  return res.json()
}

export interface DocFile {
  path: string
  title: string
}

export interface DocSection {
  key: string
  title: string
  files: DocFile[]
}

export const useDocsTree = () =>
  useQuery<DocSection[]>({
    queryKey: ['docs', 'tree'],
    queryFn: () => apiFetch<DocSection[]>('/api/documents/tree'),
    staleTime: 60_000,
  })

export const useDocContent = (path: string | null) =>
  useQuery<string>({
    queryKey: ['docs', 'content', path],
    queryFn: async () => {
      const token = getToken()
      const res = await fetch(`/api/documents/content?path=${encodeURIComponent(path!)}`, {
        headers: {
          ...(token ? { Authorization: `Bearer ${token}` } : {}),
        },
      })
      if (res.status === 401 || res.status === 403) {
        clearAuth()
        window.location.reload()
        throw new Error(res.status === 403 ? 'Forbidden' : 'Unauthorized')
      }
      if (!res.ok) throw new Error(`API error ${res.status}`)
      return res.text()
    },
    enabled: !!path,
    staleTime: 60_000,
  })

export interface LoginResponse {
  token: string
  role: Role
  expires_at: string
}

export const login = (password: string): Promise<LoginResponse> =>
  apiFetch<LoginResponse>('/api/auth/login', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })

export interface MeResponse {
  role: Role
  expires_at: string
  tenant?: string
  capabilities?: string[]
}

export const useMe = () =>
  useQuery<MeResponse>({
    queryKey: ['me'],
    queryFn: () => apiFetch<MeResponse>('/api/auth/me'),
    staleTime: 60_000,
  })

export interface AuditRow {
  id: number
  at: string
  role: string
  action: string
  key: string
}

export interface AuditPage {
  items: AuditRow[]
  total: number
}

export const useAudit = (limit = 50, offset = 0) =>
  useQuery<AuditPage>({
    queryKey: ['audit', limit, offset],
    queryFn: () =>
      apiFetch<AuditPage>(`/api/audit?limit=${limit}&offset=${offset}`),
  })

export interface ConfigKeyMeta {
  key: string
  is_secret: boolean
  has_value: boolean
  updated_at: string
}

export const useConfigKeys = () =>
  useQuery<ConfigKeyMeta[]>({
    queryKey: ['config', 'keys'],
    queryFn: () => apiFetch<ConfigKeyMeta[]>('/api/config/keys'),
  })

// ─── Model Registry & Detail ───────────────────────────────────────────────

export interface ModelRegistryItem {
  name: string
  task_type: string
  supported_datasets: string[]
}

export interface ModelFrontmatter {
  display_name: string | null
  summary: string | null
  paper: { url?: string; title?: string } | null
  use_cases: string[] | null
  maintainers: string[] | null
  tags: string[] | null
  status: 'stable' | 'experimental' | 'deprecated' | 'archived' | null
  last_reviewed: string | null
}

export interface ModelDescription {
  body: string
  source: 'filesystem' | 'override' | 'empty'
  upstream_drift: boolean
  updated_at: string | null
}

export interface ModelStageInfo {
  version: string
  run_id: string
  alias: string
  created_at: number
  updated_at: number
}

export interface ModelDetailResponse {
  name: string
  task_type: string
  frontmatter: ModelFrontmatter
  frontmatter_warnings: string[]
  description: ModelDescription
  technical: {
    estimator_class: string
    supported_datasets: string[]
    input_schema: Record<string, string>
    output_schema: Record<string, string>
    promotion: { metric: string; threshold: number; direction: string; model_id: string }
    hyperparameters?: Record<string, unknown>
  }
  lifecycle_gates?: Array<{
    name: string
    metric: string
    threshold: number
    direction: string
  }>
  retraining?: {
    schedule: string | null
    deployment_name: string | null
    work_pool: string | null
    concurrency_limit: number | null
  }
  seanerbus_uuid?: string | null
  stages: {
    production: ModelStageInfo | null
    canary: ModelStageInfo | null
    staging: ModelStageInfo | null
  }
  links: Record<string, string>
  images: Array<{
    id: string | null
    url: string
    placeholder: string
    source: 'filesystem' | 'uploaded'
    original_name?: string
  }>
}

export interface ModelVersion {
  version: string
  run_id: string
  alias: string | null
  aliases: string[]
  framework: string
  metrics: Record<string, number>
  created_at: number
  updated_at: number
}

export interface UploadedImage {
  id: string
  placeholder: string
  url: string
  size_bytes: number
}

export const useModelRegistry = () =>
  useQuery<ModelRegistryItem[]>({
    queryKey: ['models', 'registry'],
    queryFn: () => apiFetch<ModelRegistryItem[]>('/api/models/registry'),
  })

export const useModelDetail = (name: string) =>
  useQuery<ModelDetailResponse>({
    queryKey: ['models', 'detail', name],
    queryFn: () => apiFetch<ModelDetailResponse>(`/api/models/${name}`),
    enabled: !!name,
  })

export const useModelVersions = (name: string) =>
  useQuery<ModelVersion[]>({
    queryKey: ['model-versions', name],
    queryFn: () => apiFetch<ModelVersion[]>(`/api/models/${name}/versions`),
    enabled: !!name,
  })

export const useSetAlias = (name: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ version, alias }: { version: string; alias: string }) =>
      apiFetch<ModelVersion[]>(`/api/models/${name}/versions/${version}/alias`, {
        method: 'PUT',
        body: JSON.stringify({ alias }),
      }),
    onSuccess: (data) => {
      qc.setQueryData(['model-versions', name], data)
      qc.invalidateQueries({ queryKey: ['models', 'detail', name] })
    },
  })
}

export const useDeleteAlias = (name: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ version, alias }: { version: string; alias: string }) =>
      apiFetch<ModelVersion[]>(`/api/models/${name}/versions/${version}/alias/${alias}`, {
        method: 'DELETE',
      }),
    onSuccess: (data) => {
      qc.setQueryData(['model-versions', name], data)
      qc.invalidateQueries({ queryKey: ['models', 'detail', name] })
    },
  })
}

export const useUpdateDescription = (name: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (markdown: string) =>
      apiFetch<{ ok: boolean }>(`/api/models/${name}/description`, {
        method: 'PUT',
        body: JSON.stringify({ markdown }),
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['models', 'detail', name] }),
  })
}

export const useRevertDescription = (name: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: () =>
      fetch(`/api/models/${name}/description`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${getToken()}` },
      }).then(r => { if (!r.ok) throw new Error(`${r.status}`) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['models', 'detail', name] }),
  })
}

export const useUploadImage = (name: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (file: File) => {
      const fd = new FormData()
      fd.append('file', file)
      const token = getToken()
      return fetch(`/api/models/${name}/images`, {
        method: 'POST',
        headers: token ? { Authorization: `Bearer ${token}` } : {},
        body: fd,
      }).then(async r => {
        if (!r.ok) throw new Error(`${r.status}`)
        return r.json() as Promise<UploadedImage>
      })
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['models', 'detail', name] }),
  })
}

export const useDeleteImage = (name: string) => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (imageId: string) =>
      fetch(`/api/models/${name}/images/${imageId}`, {
        method: 'DELETE',
        headers: { Authorization: `Bearer ${getToken() ?? ''}` },
      }).then(r => { if (r.status !== 204) throw new Error(`${r.status}`) }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['models', 'detail', name] }),
  })
}

// ─── ModelZoo GitLab stats ─────────────────────────────────────────────────

export interface ModelzooLastCommit {
  sha: string
  message: string
  author_name: string
  committed_date: string
}

export interface ModelzooStats {
  configured: boolean
  models_count?: number
  datasets_count?: number
  task_categories?: string[]
  last_commit?: ModelzooLastCommit | null
  branch?: string
  repo_url?: string | null
}

export interface ModelzooDataset {
  class_name: string
  filename: string
  file_path: string
  file_url: string | null
}

export const useModelzooStats = () =>
  useQuery<ModelzooStats>({
    queryKey: ['modelzoo', 'stats'],
    queryFn: () => apiFetch<ModelzooStats>('/api/modelzoo/stats'),
    staleTime: 120_000,
    retry: false,
  })

export const useModelzooDatasets = () =>
  useQuery<ModelzooDataset[]>({
    queryKey: ['modelzoo', 'datasets'],
    queryFn: () => apiFetch<ModelzooDataset[]>('/api/modelzoo/datasets'),
    staleTime: 120_000,
    retry: false,
  })

export interface ModelzooModel {
  name: string
  task_category: string
  dir_path: string
  file_url: string | null
}

export const useModelzooModels = () =>
  useQuery<ModelzooModel[]>({
    queryKey: ['modelzoo', 'models'],
    queryFn: () => apiFetch<ModelzooModel[]>('/api/modelzoo/models'),
    staleTime: 120_000,
  })

export const usePredict = (name: string) =>
  useMutation({
    mutationFn: ({ features, stage, version }: { features: unknown; stage?: string; version?: string }) => {
      const params = new URLSearchParams()
      if (stage) params.set('stage', stage)
      if (version) params.set('version', version)
      const qs = params.toString()
      return apiFetch<unknown>(`/api/models/${name}/predict${qs ? `?${qs}` : ''}`, {
        method: 'POST',
        body: JSON.stringify(features),
      })
    },
  })

// ─── CI Pipeline trigger ───────────────────────────────────────────────────

export async function triggerPipeline(): Promise<{ pipeline_id: number; status: string; web_url: string }> {
  return apiFetch('/api/modelzoo/trigger-pipeline', { method: 'POST' })
}

export async function getPipelineStatus(pipelineId: number): Promise<{
  status: string
  web_url: string
  duration_seconds: number | null
  created_at: string | null
  finished_at: string | null
}> {
  return apiFetch(`/api/modelzoo/pipeline-status/${pipelineId}`)
}

// ── SeanerBUS types ──────────────────────────────────────────────────────────

export interface SeanerbusConfig {
  [key: string]: string | undefined
}

export interface BridgeHealth {
  status: string
  mode: string
}

export interface BridgeStats {
  mode: string
  inferences_total: number
  retrains_total: number
  per_model: Record<string, { inferences: number; errors: number }>
}

export interface BridgeStatus {
  reachable: boolean
  status_url: string
  health?: BridgeHealth
  stats?: BridgeStats
  error?: string
}

// ── SeanerBUS hooks ──────────────────────────────────────────────────────────

export const useSeanerbusConfig = () =>
  useQuery<SeanerbusConfig>({
    queryKey: ['seanerbus', 'config'],
    queryFn: () => apiFetch<SeanerbusConfig>('/api/seanerbus/config'),
    staleTime: 30_000,
  })

export const useSeanerbusStatus = () =>
  useQuery<BridgeStatus>({
    queryKey: ['seanerbus', 'status'],
    queryFn: () => apiFetch<BridgeStatus>('/api/seanerbus/status'),
    staleTime: 10_000,
    refetchInterval: 15_000,
  })

export interface ModelUuids {
  [model: string]: string | null
}

export const useSeanerbusModelUuids = () =>
  useQuery<ModelUuids>({
    queryKey: ['seanerbus', 'model-uuids'],
    queryFn: () => apiFetch<ModelUuids>('/api/seanerbus/model-uuids'),
    staleTime: 60_000,
  })

export interface GrafanaPanels {
  grafana_url: string
  dashboard_uid: string
  panels: {
    bridge_up: number
    inference_rate: number
    error_rate: number
    latency: number
  }
}

export const useSeanerbusGrafanaPanels = () =>
  useQuery<GrafanaPanels>({
    queryKey: ['seanerbus', 'grafana-panels'],
    queryFn: () => apiFetch<GrafanaPanels>('/api/seanerbus/grafana-panels'),
    staleTime: 300_000,
  })

// ─── Containers ────────────────────────────────────────────────────────────────

export function useContainers() {
  return useQuery({
    queryKey: ["containers"],
    queryFn: listContainers,
    refetchInterval: 30_000,
    retry: false,
  })
}

export function useContainerAction() {
  const queryClient = useQueryClient()
  return useMutation({
    mutationFn: ({
      name,
      action,
    }: {
      name: string
      action: "start" | "stop" | "restart"
    }) => {
      if (action === "start") return startContainer(name)
      if (action === "stop") return stopContainer(name)
      return restartContainer(name)
    },
    onSuccess: () => {
      setTimeout(
        () => queryClient.invalidateQueries({ queryKey: ["containers"] }),
        2000,
      )
    },
  })
}

// ─── Approvals ─────────────────────────────────────────────────────────────

export interface ApprovalEntry {
  id: string
  model_id: string
  commit_sha: string
  commit_msg: string
  changed_files: string[]
  status: 'pending' | 'approved' | 'rejected'
  prefect_run_id: string | null
  reject_reason: string | null
  requested_at: string
  resolved_at: string | null
}

export const useApprovals = (status?: string) =>
  useQuery<ApprovalEntry[]>({
    queryKey: ['approvals', status ?? 'all'],
    queryFn: () => {
      const qs = status ? `?status=${encodeURIComponent(status)}` : ''
      return apiFetch<ApprovalEntry[]>(`/api/approvals${qs}`)
    },
    refetchInterval: 30_000,
  })

export const useApprovalsCount = () =>
  useQuery<ApprovalEntry[]>({
    queryKey: ['approvals', 'pending'],
    queryFn: () => apiFetch<ApprovalEntry[]>('/api/approvals?status=pending'),
    refetchInterval: 30_000,
  })

export const useApproveModel = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (modelId: string) =>
      apiFetch<unknown>(`/api/approvals/approve/${encodeURIComponent(modelId)}`, { method: 'POST' }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['approvals'] })
    },
  })
}

export const useRejectModel = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ modelId, reason }: { modelId: string; reason: string }) =>
      apiFetch<unknown>(`/api/approvals/reject/${encodeURIComponent(modelId)}`, {
        method: 'POST',
        body: JSON.stringify({ reason }),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['approvals'] })
    },
  })
}

// ─── ModelZoo Freshness (Phase 11 B2) ─────────────────────────────────────

export interface ModelFreshness {
  model_id: string
  status: 'current' | 'stale' | 'unknown'
  latest_modelzoo_commit: string | null
  last_retrain_commit: string | null
  stale_since: string | null
  retrain_triggered_at: string | null
}

export interface ModelzooEvent {
  id: number
  commit_sha: string
  branch: string
  pushed_by: string | null
  timestamp: string
  source: 'webhook' | 'poll'
}

export interface ModelzooFreshnessResponse {
  models: ModelFreshness[]
  last_event: { commit_sha: string; timestamp: string; source: string } | null
}

export const useModelzooFreshness = () =>
  useQuery<ModelzooFreshnessResponse>({
    queryKey: ['modelzoo', 'freshness'],
    queryFn: () => apiFetch<ModelzooFreshnessResponse>('/api/proxy/control_plane/modelzoo/status'),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })

export const useModelzooEvents = (limit = 20) =>
  useQuery<ModelzooEvent[]>({
    queryKey: ['modelzoo', 'events', limit],
    queryFn: () =>
      apiFetch<ModelzooEvent[]>(`/api/proxy/control_plane/modelzoo/events?limit=${limit}`),
    refetchInterval: 60_000,
    staleTime: 30_000,
  })

// ── Pipelines ─────────────────────────────────────────────────────────────────

export interface PrefectDeployment {
  id: string
  name: string
  flow_name: string
  paused: boolean
  status: string
  schedules?: { cron: string; active: boolean }[]
}

export interface PrefectRun {
  id: string
  name: string
  state_type: string
  state_name: string
  deployment_id: string | null
  start_time: string | null
  end_time: string | null
}

export interface TriggerRunRequest {
  model_name: string
  dataset_name?: string
  dummy?: boolean
}

export const usePipelineDeployments = () =>
  useQuery<PrefectDeployment[]>({
    queryKey: ['pipeline-deployments'],
    queryFn: () => apiFetch<PrefectDeployment[]>('/api/pipelines/deployments'),
    refetchInterval: 30_000,
  })

export const usePipelineRuns = (limit = 20) =>
  useQuery<PrefectRun[]>({
    queryKey: ['pipeline-runs', limit],
    queryFn: () => apiFetch<PrefectRun[]>(`/api/pipelines/runs?limit=${limit}`),
    refetchInterval: 15_000,
  })

export const useTriggerRun = () => {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (body: TriggerRunRequest) =>
      apiFetch('/api/pipelines/trigger', { method: 'POST', body: JSON.stringify(body) }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['pipeline-runs'] }) },
  })
}

// ── Scaffold ──────────────────────────────────────────────────────────────────

export interface ScaffoldBody {
  name: string
  task: string
  task_type: string
  promotion_metric: string
  promotion_threshold: number
  promotion_direction: string
  force: boolean
}

export interface ScaffoldPreview {
  [filePath: string]: string
}

export const useScaffoldPreview = () =>
  useMutation<ScaffoldPreview, Error, ScaffoldBody>({
    mutationFn: (body) =>
      apiFetch('/api/scaffold/preview', { method: 'POST', body: JSON.stringify(body) }),
  })

export const useScaffoldCreate = () => {
  const qc = useQueryClient()
  return useMutation<{ message: string }, Error, ScaffoldBody>({
    mutationFn: (body) =>
      apiFetch('/api/scaffold/create', { method: 'POST', body: JSON.stringify(body) }),
    onSuccess: () => { qc.invalidateQueries({ queryKey: ['model-registry'] }) },
  })
}
