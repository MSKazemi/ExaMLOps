# Command Palette & Global Search

The **command palette** is the dashboard's keyboard-first entry point. Press **⌘K** (macOS) or
**Ctrl-K** (Linux/Windows) anywhere to open it, then type to fuzzy-navigate to any page or entity, run
a role-scoped action, or search across models, HPC jobs, and the audit log.

- **Feature:** F2 · **Design:** ADR 0056 (`design/adr/0056-dashboard-command-palette-search.md`) ·
  **Spec:** `design/vision/specs/F2-command-palette-search.md`
- **Backend:** `platform/services/dashboard/backend/search.py` + `routers/search.py`
- **Frontend:** `platform/services/dashboard/frontend/src/lib/commands.ts`, `lib/search.ts`,
  `components/CommandPalette.tsx`

## Keyboard

| Key | Action |
|---|---|
| **⌘K** / **Ctrl-K** | Toggle the palette |
| **↑ / ↓** | Move the selection |
| **Enter** | Run the selected row (navigate, or copy an action's CLI equivalent) |
| **Esc** | Close the palette |

## What's in it

### Commands (rank first)

Registered navigation and action commands from the command registry (`lib/commands.ts`):

- **Navigate** — jump to any page (Overview, Models, MLOps Console, Facility Console, …).
- **Actions** — expose the equivalent `exa` command for **GUI↔CLI parity** (F2 R6). Selecting an
  action copies its command (e.g. `exa drift status`) to the clipboard.

Commands are **role-scoped** (F15): a viewer never sees admin-only entries like *Go to Audit* or
*Go to Approvals*. Destructive actions still route through the page's own confirm/approval gate — the
palette never performs one-keystroke destruction (F2 R5).

### Search results (stream in below)

As you type, the palette queries the federated `GET /api/v1/search` endpoint (debounced and canceled
per keystroke) and lists typed, ranked results grouped by source:

| Source | Entity | Links to |
|---|---|---|
| `mlflow` | Model | `/models/{name}` |
| `scheduler` | HPC job | `/facility` |
| `audit` | Audit event | `/audit` |
| `docs` | Page | the page route |

Ranking (server and client use the same scoring): exact match > prefix > word-boundary > substring >
fuzzy subsequence.

## Endpoint

```
GET /api/v1/search?q=<query>&limit=20        # viewer role; BFF-composed, partial-failure safe
```

Returns `{search: {query, count, results[], groups{}}}`. See
[`docs/reference/api.md`](../reference/api.md) for the full shape and
[`docs/dashboard/architecture.md`](../dashboard/architecture.md#command-palette--global-search-f2)
for the component diagram.

## Notes & limits

- Search reads from `platform.db` (`PLATFORM_DB`) plus the static page set; a missing table degrades
  to no results for that source, never an error.
- This slice covers ⌘K, fuzzy command navigation, role-scoping, copy-as-CLI, and federated
  models/jobs/audit/pages search. The richer F2 surfaces (a dedicated search page, the `?` shortcut
  overlay, `g _` go-to chords, `j/k` list navigation) build on this and are tracked in the
  dashboard-nextgen plan.
