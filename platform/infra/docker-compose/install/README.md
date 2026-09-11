# ExaMLOps — single-node install bundle

Runs ExaMLOps on one Linux host from the released images. Nothing is built, and the source tree
isn't needed.

```bash
./install.sh init        # .env with generated credentials + ./state (add --version X.Y.Z in a checkout)
./install.sh check       # no placeholder left, compose file valid
docker compose up -d
```

Dashboard: `http://localhost:18099`. Sign in as `admin` with `DASHBOARD_ADMIN_PASSWORD` from
`.env`.

| File | Purpose |
|---|---|
| `docker-compose.yml` | The stack. Images are pinned to `EXAMLOPS_VERSION`, upstream images by digest. |
| `env.template` | Every setting. `install.sh init` turns it into `.env`. |
| `install.sh` | `init`, `upgrade-env` (after unpacking a newer bundle) and `check`. POSIX sh. |
| `monitoring/` | Config for the optional `monitoring` profile (Prometheus, Alertmanager, Grafana, Loki, Tempo). |
| `state/` | Created by `init`. The instance-data root (ADR 0128); back it up. |
| `secrets/` | Created by `init`. Alertmanager receiver secrets; never part of a release. |

The full guide is `docs/guides/install-compose-bundle.md`: requirements, exposing the stack,
adding your models, backups, upgrades, air-gapped mirrors and known limits.
