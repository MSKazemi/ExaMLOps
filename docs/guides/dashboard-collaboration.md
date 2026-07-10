# Collaboration & Workflow

The dashboard lets teams collaborate directly on platform entities: comment with @-mentions, follow an
entity's activity trail, and share a time-frozen view via a scoped, expiring link. Everything is
tenant-scoped, sanitized, and audited.

- **Feature:** F22 · **Design:** [ADR 0073](../../design/adr/0073-dashboard-collaboration-workflow.md) ·
  **Spec:** `design/vision/specs/F22-collaboration-workflow.md`
- **Backend:** `platform/services/dashboard/backend/collab.py` + `routers/collab.py`
- **Frontend:** `lib/collab.ts` + `components/{CommentThread,ShareSnapshotButton}.tsx` (adopted on ModelDetail)

## Comments & @-mentions

Open a model (e.g. `/models/jpcp`) and scroll to **Discussion**. Comments attach to the entity and are
tenant-scoped — you only see your tenant's threads. Mention a teammate with `@name`; they are notified
over the realtime channel (F12). The composer previews who a draft will notify.

Comments are **sanitized server-side** (HTML tags and `javascript:` URIs stripped) and rendered inertly,
so a comment can never inject script into the dashboard.

## Activity trail

`GET /api/v1/collab/{type}/{id}/activity` merges an entity's comments with its audit events into one
chronological trail — so a promotion, a drift event, and the discussion around them read as a single
story. Every collaborative action is itself audited (`source=dashboard-collab`).

## Shareable snapshots

Use **Share snapshot** to freeze the current view (path + filters) and copy a shareable link. The snapshot
is stored behind a scoped, **read-only, expiring** token (default 7 days) bound to your tenant — the
recipient sees the exact frozen view and cannot write or reach another tenant's data.

```ts
POST /api/v1/collab/snapshot        { "view": { "path": "/models/jpcp", "search": "?range=24h" } }
→ { "token": "…", "expires_at": "…" }
GET  /api/v1/collab/snapshot/{token} → { "found": true, "view": {…}, "read_only": true }   // or { "found": false }
```

## Notes & limits

Shipped: entity comments with @-mentions + notifications, the activity trail, and shareable read-only
snapshots — all tenant-scoped, sanitized, and audited. Deferred (tracked in the plan): the collaboration
store graduating to cross-device sync, standalone **external share pages** that render a snapshot token,
approval **discussion threads** captured with the decision (extends the F14 approval gate), and **external
push** to Notion / issue trackers behind a feature flag (F25).

See [`docs/dashboard/architecture.md`](../dashboard/architecture.md#collaboration--workflow-f22) for the
design diagram and [`docs/reference/api.md`](../reference/api.md) for endpoint shapes.
