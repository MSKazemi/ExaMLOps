import { useState, useEffect, useRef } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { BookOpen, ChevronDown, ChevronRight, Search, FileText, ArrowUp, ExternalLink } from 'lucide-react'
import { cn } from '@/lib/utils'
import { useDocsTree, useDocContent } from '@/lib/api'
import type { DocSection, DocFile } from '@/lib/api'

function SidebarSection({
  section,
  selectedPath,
  onSelect,
  defaultOpen,
}: {
  section: DocSection
  selectedPath: string | null
  onSelect: (file: DocFile) => void
  defaultOpen: boolean
}) {
  const hasSelected = section.files.some(f => f.path === selectedPath)
  const [open, setOpen] = useState(defaultOpen || hasSelected)

  useEffect(() => {
    if (hasSelected) setOpen(true)
  }, [hasSelected])

  return (
    <div className="mb-1">
      <button
        onClick={() => setOpen(o => !o)}
        className="flex w-full items-center gap-1.5 rounded-md px-2 py-1.5 text-xs font-semibold uppercase tracking-wider text-muted-foreground hover:text-foreground transition-colors"
      >
        {open ? <ChevronDown className="h-3 w-3 shrink-0" /> : <ChevronRight className="h-3 w-3 shrink-0" />}
        {section.title}
      </button>
      {open && (
        <ul className="mt-0.5 space-y-0.5">
          {section.files.map(file => (
            <li key={file.path}>
              <button
                onClick={() => onSelect(file)}
                className={cn(
                  'flex w-full items-center gap-2 rounded-md px-3 py-1.5 text-sm text-left transition-colors',
                  selectedPath === file.path
                    ? 'bg-primary text-primary-foreground font-medium'
                    : 'text-muted-foreground hover:text-foreground hover:bg-accent'
                )}
              >
                <FileText className="h-3.5 w-3.5 shrink-0 opacity-70" />
                {file.title}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

const mdComponents = {
  h1: ({ children }: { children?: React.ReactNode }) => (
    <h1 className="scroll-mt-20 text-3xl font-bold mb-6 mt-2 pb-3 border-b border-border tracking-tight">
      {children}
    </h1>
  ),
  h2: ({ children }: { children?: React.ReactNode }) => (
    <h2 className="scroll-mt-20 text-xl font-semibold mb-4 mt-10 pb-2 border-b border-border/60">
      {children}
    </h2>
  ),
  h3: ({ children }: { children?: React.ReactNode }) => (
    <h3 className="scroll-mt-20 text-lg font-semibold mb-3 mt-7">{children}</h3>
  ),
  h4: ({ children }: { children?: React.ReactNode }) => (
    <h4 className="scroll-mt-20 text-base font-medium mb-2 mt-5 text-foreground/80">{children}</h4>
  ),
  p: ({ children }: { children?: React.ReactNode }) => (
    <p className="mb-4 leading-7 text-foreground/85">{children}</p>
  ),
  ul: ({ children }: { children?: React.ReactNode }) => (
    <ul className="mb-4 ml-6 list-disc space-y-1.5 marker:text-primary/60">{children}</ul>
  ),
  ol: ({ children }: { children?: React.ReactNode }) => (
    <ol className="mb-4 ml-6 list-decimal space-y-1.5 marker:text-primary/60">{children}</ol>
  ),
  li: ({ children }: { children?: React.ReactNode }) => (
    <li className="leading-7 text-foreground/85">{children}</li>
  ),
  pre: ({ children }: { children?: React.ReactNode }) => (
    <pre
      className="mb-6 overflow-x-auto rounded-xl text-sm font-mono leading-relaxed"
      style={{
        background: 'var(--code-bg)',
        border: '1px solid var(--code-border)',
        color: 'var(--code-fg)',
        padding: '1.125rem 1.5rem',
      }}
    >
      {children}
    </pre>
  ),
  code: ({ children, className }: { children?: React.ReactNode; className?: string }) => {
    // Block code: has language class, OR content spans multiple lines (un-tagged fenced block)
    const isBlock = !!className || (typeof children === 'string' && children.includes('\n'))
    if (isBlock) {
      return <code className={cn('font-mono text-sm', className)}>{children}</code>
    }
    return (
      <code
        className="rounded-md text-[0.875em] font-mono px-1.5 py-0.5"
        style={{ background: 'var(--code-inline-bg)', color: 'var(--code-inline-fg)' }}
      >
        {children}
      </code>
    )
  },
  blockquote: ({ children }: { children?: React.ReactNode }) => (
    <blockquote
      className="mb-4 rounded-r-lg"
      style={{
        borderLeft: '3px solid var(--primary)',
        background: 'oklch(0.64 0.20 265 / 7%)',
        padding: '0.75rem 1rem 0.75rem 1.25rem',
      }}
    >
      <div className="text-muted-foreground leading-relaxed [&>p:last-child]:mb-0">{children}</div>
    </blockquote>
  ),
  table: ({ children }: { children?: React.ReactNode }) => (
    <div className="mb-6 overflow-x-auto rounded-xl border border-border">
      <table className="w-full text-sm">{children}</table>
    </div>
  ),
  thead: ({ children }: { children?: React.ReactNode }) => (
    <thead style={{ background: 'var(--surface-1)' }}>{children}</thead>
  ),
  th: ({ children }: { children?: React.ReactNode }) => (
    <th className="border-b border-border px-4 py-3 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground">
      {children}
    </th>
  ),
  tbody: ({ children }: { children?: React.ReactNode }) => (
    <tbody className="divide-y divide-border">{children}</tbody>
  ),
  tr: ({ children }: { children?: React.ReactNode }) => (
    <tr className="transition-colors hover:bg-muted/25">{children}</tr>
  ),
  td: ({ children }: { children?: React.ReactNode }) => (
    <td className="px-4 py-2.5 text-foreground/85">{children}</td>
  ),
  a: ({ href, children }: { href?: string; children?: React.ReactNode }) => {
    const isExternal = href?.startsWith('http')
    return (
      <a
        href={href}
        target={isExternal ? '_blank' : undefined}
        rel={isExternal ? 'noopener noreferrer' : undefined}
        className="font-medium text-primary underline underline-offset-4 decoration-primary/40 hover:decoration-primary transition-all inline-flex items-center gap-0.5"
      >
        {children}
        {isExternal && <ExternalLink className="h-3 w-3 ml-0.5 shrink-0" />}
      </a>
    )
  },
  hr: () => <hr className="my-8 border-border" />,
  strong: ({ children }: { children?: React.ReactNode }) => (
    <strong className="font-semibold text-foreground">{children}</strong>
  ),
  em: ({ children }: { children?: React.ReactNode }) => (
    <em className="italic text-foreground/80">{children}</em>
  ),
  img: ({ src, alt }: { src?: string; alt?: string }) => (
    <img
      src={src}
      alt={alt ?? ''}
      className="rounded-xl border border-border max-w-full my-4 shadow-sm"
    />
  ),
}

function WelcomeScreen({
  sections,
  onSelect,
}: {
  sections: DocSection[]
  onSelect: (file: DocFile) => void
}) {
  return (
    <div>
      <div className="mb-8 pb-6 border-b border-border">
        <h1 className="text-3xl font-bold mb-2 tracking-tight">ExaMLOps Documentation</h1>
        <p className="text-muted-foreground text-base leading-relaxed max-w-xl">
          End-to-end MLOps platform for HPC systems — training pipelines, model versioning,
          HPC orchestration, and multi-model serving.
        </p>
      </div>
      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {sections.map(section => (
          <div
            key={section.key}
            className="rounded-xl border border-border p-4 transition-colors hover:border-primary/40 card-hover"
            style={{ background: 'var(--surface-0)' }}
          >
            <h3 className="font-semibold mb-3 text-xs uppercase tracking-widest text-muted-foreground">
              {section.title}
            </h3>
            <ul className="space-y-1">
              {section.files.map(file => (
                <li key={file.path}>
                  <button
                    onClick={() => onSelect(file)}
                    className="flex items-center gap-2 text-sm text-primary/80 hover:text-primary hover:underline underline-offset-4 text-left w-full transition-colors"
                  >
                    <FileText className="h-3.5 w-3.5 shrink-0 opacity-70" />
                    {file.title}
                  </button>
                </li>
              ))}
            </ul>
          </div>
        ))}
      </div>
    </div>
  )
}

function SkeletonLoader() {
  return (
    <div className="space-y-4 animate-pulse">
      <div className="h-8 bg-muted rounded-md w-3/4" />
      <div className="h-4 bg-muted rounded w-full" />
      <div className="h-4 bg-muted rounded w-5/6" />
      <div className="h-4 bg-muted rounded w-4/5" />
      <div className="h-4 bg-muted rounded w-full mt-6" />
      <div className="h-4 bg-muted rounded w-3/4" />
      <div className="h-4 bg-muted rounded w-5/6" />
    </div>
  )
}

export function Docs() {
  const { data: sections, isLoading: treeLoading } = useDocsTree()
  const [selectedFile, setSelectedFile] = useState<DocFile | null>(null)
  const [search, setSearch] = useState('')
  const contentRef = useRef<HTMLDivElement>(null)
  const [showScrollTop, setShowScrollTop] = useState(false)

  // Select the first file once sections load
  useEffect(() => {
    if (sections && sections.length > 0 && !selectedFile) {
      setSelectedFile(sections[0].files[0])
    }
  }, [sections, selectedFile])

  const { data: content, isLoading: contentLoading } = useDocContent(selectedFile?.path ?? null)

  const filteredSections = sections
    ?.map(section => ({
      ...section,
      files: section.files.filter(f =>
        f.title.toLowerCase().includes(search.toLowerCase())
      ),
    }))
    .filter(s => s.files.length > 0)

  const selectedSection = sections?.find(s =>
    s.files.some(f => f.path === selectedFile?.path)
  )

  const handleSelect = (file: DocFile) => {
    setSelectedFile(file)
    contentRef.current?.scrollTo({ top: 0 })
  }

  return (
    <div className="h-full flex">
      {/* Sidebar */}
      <aside
        className="w-60 shrink-0 border-r flex flex-col"
        style={{ background: 'var(--surface-deep)' }}
      >
        {/* Header + search */}
        <div className="p-3 border-b shrink-0" style={{ background: 'var(--surface-0)' }}>
          <div className="flex items-center gap-2 mb-2.5 px-1">
            <BookOpen className="h-4 w-4 text-primary shrink-0" />
            <span className="font-semibold text-sm">Documentation</span>
          </div>
          <div className="relative">
            <Search className="absolute left-2.5 top-2 h-3.5 w-3.5 text-muted-foreground pointer-events-none" />
            <input
              type="text"
              placeholder="Search docs…"
              value={search}
              onChange={e => setSearch(e.target.value)}
              className="w-full rounded-lg border bg-background pl-8 pr-3 py-1.5 text-sm placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring transition-shadow"
            />
          </div>
        </div>

        {/* Nav tree */}
        <nav className="flex-1 overflow-y-auto p-2">
          {treeLoading ? (
            <p className="px-2 py-4 text-sm text-muted-foreground">Loading…</p>
          ) : filteredSections?.length === 0 ? (
            <p className="px-2 py-4 text-sm text-muted-foreground">
              No results for &ldquo;{search}&rdquo;
            </p>
          ) : (
            filteredSections?.map((section, i) => (
              <SidebarSection
                key={section.key}
                section={section}
                selectedPath={selectedFile?.path ?? null}
                onSelect={handleSelect}
                defaultOpen={i === 0}
              />
            ))
          )}
        </nav>
      </aside>

      {/* Main content */}
      <div
        ref={contentRef}
        className="flex-1 overflow-y-auto"
        onScroll={e => setShowScrollTop((e.target as HTMLElement).scrollTop > 300)}
      >
        <div className="mx-auto max-w-3xl px-10 py-10">
          {/* Breadcrumb */}
          {selectedFile && selectedSection && (
            <nav className="mb-8 flex items-center gap-1.5 text-xs text-muted-foreground">
              <BookOpen className="h-3.5 w-3.5 shrink-0" />
              <span className="uppercase tracking-wide">{selectedSection.title}</span>
              <ChevronRight className="h-3.5 w-3.5 shrink-0" />
              <span className="text-foreground font-medium">{selectedFile.title}</span>
            </nav>
          )}

          {/* Content */}
          {contentLoading ? (
            <SkeletonLoader />
          ) : !selectedFile ? (
            <WelcomeScreen sections={sections ?? []} onSelect={handleSelect} />
          ) : content ? (
            <article className="min-w-0">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={mdComponents}>
                {content}
              </ReactMarkdown>
            </article>
          ) : null}
        </div>
      </div>

      {/* Scroll-to-top */}
      {showScrollTop && (
        <button
          onClick={() => contentRef.current?.scrollTo({ top: 0, behavior: 'smooth' })}
          className="fixed bottom-6 right-6 z-50 rounded-full bg-primary p-2.5 text-primary-foreground shadow-lg hover:opacity-90 transition-opacity"
          aria-label="Scroll to top"
        >
          <ArrowUp className="h-4 w-4" />
        </button>
      )}
    </div>
  )
}
