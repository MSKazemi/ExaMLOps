# Resources: manage everything the platform manages

**Platform → Resources** (`/platform/resources`) turns what `exa` manages into tables. Projects,
connections, workbenches, prompts, SLOs, gateway keys, HPC clusters, secrets, backups and more
each get a table with a **New** button and **View / Edit / Delete** on every row. A **⋯** menu on
the row holds every other action. The [CLI Console](dashboard-cli-console.md) runs any single
command. This page is how you manage the *things* those commands act on, the way an enterprise
console does. The design is recorded in ADR 0119.

## Using it

1. Pick a resource on the left (grouped like `exa --help`), search for one, or press **⌘K** and
   type its name ("Manage Projects").
2. The table is filled by the resource's list command (e.g. `exa project list`). That command's
   own options appear as filters above the table; press **Apply**. If a filter is required (for
   example the dataset for dataset revisions), the table asks for it first. **Search rows**
   filters what is already loaded.
3. **New** opens the create command's form. The row buttons open the right command with the row
   already filled in:
   - **View** loads the item's details as soon as it opens.
   - **Edit** opens the update command (e.g. `exa project set-quota`).
   - **Delete** is destructive: you type the command back to confirm.
   - **⋯** lists every other action for that item (members, storage, cost, test, rotate, promote…).
     Stopping or disabling something (an A/B test, a shadow, an auto-retrain policy) is here, not
     behind the bin icon, which is reserved for commands that remove the item.
4. The field that identifies the row (its name, id or path) is read-only and marked *from the
   selected row*, so an action can't be pointed at a different item by accident. The other
   fields are the command's normal form, with the same validation, workspace file paths and
   equivalent terminal line as the CLI Console.
5. After a change succeeds, the table refreshes. The dialog stays open on the command's output
   so you can see what happened.

Viewers see every table and can View. Actions that change state are disabled with the reason.
Every run is audited, exactly as in the CLI Console.

## What is covered

48 resources, reaching 209 of the 444 commands the dashboard can run. The rest are reports,
checks and one-off operations such as `exa status`, `exa audit verify` or `exa fleet simulate`.
They have no "item" to put in a table, and they live in the CLI Console.

| Area | Resources |
|---|---|
| Projects & Workspaces | Projects · Connections · Workbenches · Namespaces |
| Models & Registry | Models (retrain, cost, lineage, rollback, cards, gates, drift baseline…) · Serving engines · Embedding encoders |
| Training & Pipelines | Distributed training runs · Reproducibility bundles |
| Data & Features | Dataset revisions · Feature views · Data assets · Dataplane sources |
| Serving & Inference | LLM endpoints · Challengers · Traffic splits · Shadow deployments · A/B tests · Adapters · Batch jobs · Gateway keys · Knowledge bases |
| GenAI & LLMOps | Prompts |
| Agents & Automation | Agent sessions · Memory reviews · MCP tools |
| Monitoring & Quality | SLOs · Auto-retrain policies · Judge calibrations |
| HPC, Fleet & FinOps | HPC clusters · HPC jobs · Device pools · Placement decisions · Project budgets |
| Governance & Security | AI systems (EU AI Act register) · Approvals · Secrets (metadata only) · Policies · Policy bundles · Calculation providers · Audit checkpoints · Audit reviews |
| Platform & Integrations | Backups · Backup bundles · SeanerBUS models · Config contexts · Control-plane commands · Modules (site feature profile: enable / disable) |

## How a resource is defined

Resources are declared once, in the platform package (`examlops/cli/resources.py`), not in
the dashboard:

```python
R("connection", "Connections", "Named connections …",
  list="connection list", key="name",
  create="connection create", show="connection show",
  delete="connection delete", actions=("connection test",))
```

- `key` is the field in a listed row that identifies the item.
- Each row action receives the key in the parameter named like it, else in its first positional
  argument.
- Any other parameter named like a row field is pre-filled too (a connection's `project`), but
  only when the value fits the parameter's type. A number field is never filled with a display
  string.
- `bind` covers the cases where that default would be wrong. For example, `exa slo burn` takes
  an SLO's *model*, not its name.

`tests/unit/test_cli_resources.py` checks every command and binding against the live CLI tree.
It fails when a new `list` command is neither a resource nor explicitly listed as not being one.
It also runs create → list → act-on-the-listed-row → delete round-trips against the real CLI, so
a wrong binding is caught before it can act on the wrong item.
