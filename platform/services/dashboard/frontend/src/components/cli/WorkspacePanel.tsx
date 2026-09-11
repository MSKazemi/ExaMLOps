import { useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { Download, Trash2, Upload } from 'lucide-react'
import { deleteWorkspaceFile, downloadWorkspaceFile, uploadWorkspaceFile, useWorkspace } from '@/lib/cli'

const fmtSize = (n: number) =>
  n < 1024 ? `${n} B` : n < 1024 ** 2 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1024 ** 2).toFixed(1)} MB`

/**
 * The CLI workspace (admin): the one directory command path arguments may name. Upload inputs
 * (e.g. the JSONL `exa rag ingest --docs` reads), collect outputs (`exa audit export --out`).
 */
export function WorkspacePanel() {
  const qc = useQueryClient()
  const { data, isLoading, error } = useWorkspace(true)
  const fileRef = useRef<HTMLInputElement>(null)
  const [target, setTarget] = useState('')
  const [overwrite, setOverwrite] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const refresh = () => qc.invalidateQueries({ queryKey: ['cli', 'workspace'] })

  const upload = async () => {
    const file = fileRef.current?.files?.[0]
    if (!file) {
      setMsg('Choose a file first.')
      return
    }
    setMsg(null)
    try {
      const r = await uploadWorkspaceFile(file, target.trim(), overwrite)
      setMsg(`Uploaded ${r.path}.`)
      setTarget('')
      if (fileRef.current) fileRef.current.value = ''
      refresh()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Upload failed')
    }
  }

  const remove = async (path: string) => {
    if (!window.confirm(`Delete ${path} from the CLI workspace?`)) return
    try {
      await deleteWorkspaceFile(path)
      refresh()
    } catch (e) {
      setMsg(e instanceof Error ? e.message : 'Delete failed')
    }
  }

  return (
    <section aria-label="CLI workspace" className="space-y-2 text-xs">
      <p className="text-muted-foreground">
        Path arguments are relative to this directory and cannot leave it. Upload a command&rsquo;s input here;
        files a command writes appear here to download.
      </p>
      <div className="flex flex-wrap items-end gap-2">
        <input ref={fileRef} type="file" aria-label="File to upload" className="text-xs" />
        <label className="text-muted-foreground">
          Save as
          <input
            value={target}
            onChange={(e) => setTarget(e.target.value)}
            placeholder="inputs/docs.jsonl"
            aria-label="Workspace path"
            className="mt-1 block w-44 rounded-md border border-border bg-transparent px-2 py-1 font-mono"
          />
        </label>
        <label className="inline-flex items-center gap-1 text-muted-foreground">
          <input type="checkbox" checked={overwrite} onChange={(e) => setOverwrite(e.target.checked)} /> overwrite
        </label>
        <button type="button" onClick={upload} className="inline-flex items-center gap-1 rounded-md border border-border px-2 py-1 hover:bg-muted">
          <Upload className="size-3" aria-hidden="true" /> Upload
        </button>
      </div>
      {msg && <p className="text-muted-foreground">{msg}</p>}
      {error && <p className="text-muted-foreground">Could not list the workspace.</p>}
      {isLoading && <p className="text-muted-foreground">Loading…</p>}
      {data && data.files.length === 0 && <p className="text-muted-foreground">The workspace is empty.</p>}
      {data && data.files.length > 0 && (
        <ul className="divide-y divide-border rounded-md border border-border">
          {data.files.map((f) => (
            <li key={f.path} className="flex items-center gap-2 px-2 py-1">
              <code className="flex-1 truncate">{f.path}</code>
              <span className="text-muted-foreground">{fmtSize(f.size)}</span>
              <button type="button" aria-label={`Download ${f.path}`} onClick={() => downloadWorkspaceFile(f.path).catch(() => {})} className="rounded p-1 hover:bg-muted">
                <Download className="size-3" aria-hidden="true" />
              </button>
              <button type="button" aria-label={`Delete ${f.path}`} onClick={() => remove(f.path)} className="rounded p-1 hover:bg-muted">
                <Trash2 className="size-3" aria-hidden="true" />
              </button>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}
