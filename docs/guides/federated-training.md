# Federated & Privacy-Preserving Training (E7)

> Next-Gen 40 · feature **E7** · ADR 0040 · spec `design/vision/specs/E7-federated-privacy-training.md`

Some data can't move. Hospital records, per-facility telemetry, and partner datasets often
**cannot leave the site** for legal or contractual reasons — so you can't pool them into one
training set. Federated learning trains **where the data lives**: each site trains locally and
sends back only a **model update**, and a coordinator aggregates those updates into a global
model. Raw data never crosses a boundary.

E7 is a pure-Python, testable core of that loop — you can run a round, track the differential-
privacy budget, enforce secure aggregation, and reject an unauthorized site with no Flower,
Opacus, or mTLS stack. In production the same interfaces drive a real federated runtime.

## What is (and isn't) private

Honesty matters here — a privacy claim you can't back is worse than none:

| Mechanism | When on | What it gives you | What it does **not** give you |
|---|---|---|---|
| Federated aggregation | always | raw data stays at the site | updates can still leak information |
| **Differential privacy** | `--dp` | a tracked (ε, δ) budget bounding per-round leakage | privacy if you spend ε without limit |
| **Secure aggregation** | `--secure-agg` | coordinator sees only the summed update | protection against a malicious aggregator colluding with sites |

With DP **off**, `exa federated budget` says so and claims no ε/δ. With secure aggregation
**off**, per-site updates are visible to the coordinator — the API returns them rather than
pretending they're hidden.

## Aggregation strategies

| Strategy | Behaviour |
|---|---|
| `fedavg` (default) | sample-weighted mean of site updates |
| `fedprox` | FedAvg aggregation with a proximal term applied site-side |
| `robust` | coordinate-wise **trimmed mean** — drops the min+max per coordinate, tolerating a Byzantine/poisoned site |

## CLI

```bash
# Initialize a run across two sites, DP on, secure aggregation on:
exa federated init --site siteA --site siteB \
    --strategy fedavg --dp --epsilon-per-round 0.5 --delta 1e-5 --secure-agg

# Register a site but do NOT authorize it (it will be rejected if it submits):
exa federated init --site siteA --site siteB --unauthorized siteB

# Aggregate one round — each update is  site:w1,w2,…:num_samples[:loss]
exa federated round fed-fedavg-2sites \
    --update siteA:0.1,0.2:100 \
    --update siteB:0.3,0.4:150

# Mark a site's update unsigned (it will be rejected + audited):
exa federated round fed-fedavg-2sites --update siteA:0.1:100 --unsigned siteA

# Inspect the differential-privacy budget spent so far:
exa federated budget fed-fedavg-2sites
#   epsilon: 0.5   delta: 1e-05   rounds: 1

# Full run status — config, sites, and each completed round:
exa federated status fed-fedavg-2sites
```

Unauthorized or unsigned sites are rejected and written to the tamper-evident audit log
(`source = exa-federated`), so a participation attempt is always accountable.

## Python API

```python
from examlops.federated import federated_init, run_round, SiteUpdate, privacy_budget

run = federated_init(
    ["siteA", "siteB"],
    strategy="fedavg",
    dp={"epsilon_per_round": 0.5, "delta": 1e-5},
    secure_agg=True,
)
result = run_round(run.run_id, [
    SiteUpdate("siteA", weights=[0.1, 0.2], num_samples=100, loss=0.4),
    SiteUpdate("siteB", weights=[0.3, 0.4], num_samples=150, loss=0.5),
])
result.global_weights      # sample-weighted aggregate
result.epsilon             # DP budget spent to date
result.per_site            # None under secure aggregation

privacy_budget(run.run_id) # {"dp_enabled": True, "epsilon": 0.5, "delta": 1e-5, "rounds": 1}
```

## Graceful degradation

The whole loop runs with the standard library — aggregation, the DP accountant, secure-
aggregation gating, and site authorization need no optional dependency. Where a real
federated runtime (Flower) or DP-SGD library (Opacus) is present, the same interfaces map onto
it; where they aren't, the pure-Python core still enforces the governance and privacy
bookkeeping.

## Related

- **D3** supply-chain signing — site updates are signed; unsigned participation is refused.
- **D4** tamper-evident audit — every rejection/round is chained into the audit log.
- **E6** distributed training — intra-site multi-GPU training under a federated round.
- **D5** policy-as-code — a policy engine can gate which sites/strategies are permitted.
