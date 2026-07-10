import { useState, type FormEvent } from 'react'
import { MessageSquare, Send } from 'lucide-react'
import { sanitizeMarkdown } from '@/lib/sanitize'
import { useComments, useAddComment, extractMentions } from '@/lib/collab'

/**
 * Comment thread for an entity (F22 / ADR 0073, R1). Comments are tenant-scoped + audited server-side and
 * rendered sanitized (F16). @-mentions are highlighted and notify the mentioned user (F12).
 */
export function CommentThread({ entityType, entityId }: { entityType: string; entityId: string }) {
  const { data, isLoading } = useComments(entityType, entityId)
  const add = useAddComment(entityType, entityId)
  const [draft, setDraft] = useState('')
  const comments = data?.comments ?? []
  const mentions = extractMentions(draft)

  const submit = (e: FormEvent) => {
    e.preventDefault()
    const body = draft.trim()
    if (!body || add.isPending) return
    setDraft('')
    add.mutate(body)
  }

  return (
    <section className="space-y-3" aria-label="Comments">
      <h2 className="flex items-center gap-2 text-xs font-semibold uppercase tracking-widest text-muted-foreground">
        <MessageSquare className="size-4" aria-hidden="true" />
        Discussion
      </h2>

      {isLoading ? (
        <p className="text-sm text-muted-foreground">Loading comments…</p>
      ) : comments.length === 0 ? (
        <p className="text-sm text-muted-foreground">No comments yet. Start the discussion — use @name to mention someone.</p>
      ) : (
        <ul className="space-y-2">
          {comments.map((c) => (
            <li key={c.id} className="rounded-lg border border-border px-3 py-2 text-sm">
              <div className="flex items-center justify-between text-xs text-muted-foreground">
                <span className="font-medium text-foreground">{c.author}</span>
                <span>{c.created_at}</span>
              </div>
              <p className="mt-1 whitespace-pre-wrap">{sanitizeMarkdown(c.body)}</p>
              {c.mentions.length > 0 && (
                <p className="mt-1 text-xs text-primary">{c.mentions.map((m) => `@${m}`).join(' ')}</p>
              )}
            </li>
          ))}
        </ul>
      )}

      <form onSubmit={submit} className="space-y-1.5">
        <div className="flex items-center gap-2">
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Add a comment… use @name to mention"
            aria-label="Add a comment"
            className="flex-1 rounded-md border border-border bg-transparent px-3 py-2 text-sm outline-none focus:border-primary"
          />
          <button
            type="submit"
            disabled={add.isPending || draft.trim() === ''}
            aria-label="Post comment"
            className="rounded-md border border-border p-2 text-muted-foreground hover:bg-muted disabled:opacity-40"
          >
            <Send className="size-4" aria-hidden="true" />
          </button>
        </div>
        {mentions.length > 0 && (
          <p className="text-xs text-muted-foreground">
            Will notify: {mentions.map((m) => `@${m}`).join(', ')}
          </p>
        )}
      </form>
    </section>
  )
}
