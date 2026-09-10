/* Roadmap as a transit map under construction: five themed lines run from shipped stations
 * (solid) across "today" into planned stations (dotted). Every station is an item in
 * assets/explore/data/roadmap.json; ids in each station's details match that file. */
(function () {
  var S = function (id, x, y, label, sub, line, title, tasks, kind) {
    return { id: id, x: x, y: y, label: label, sub: sub, line: line, kind: kind,
      info: { title: title, sub: kind === "planned" ? "Planned · " + id : "Shipped · " + id, tasks: tasks } };
  };
  var X1 = 130, X2 = 370, P1 = 630, P2 = 860, P3 = 1090;
  XM.register("roadmap", {
    title: "ExaMLOps roadmap",
    description: "Five lines — governance, compute, serving, data and training, platform — each run from shipped stations on the left, across today, into planned stations drawn as dotted track on the right.",
    viewBox: [0, 30, 1210, 620],
    zones: [
      { x: 16, y: 50, w: 480, h: 590, label: "Shipped", line: "control" },
      { x: 516, y: 50, w: 686, h: 590, label: "Under construction", line: "planned", planned: true }
    ],
    nodes: [
      S("P11", X1, 110, "CI approval gate", "model changes wait", "human", "Sysadmin approval gate", ["Model changes from CI wait for an operator to approve or reject them, from the CLI or the dashboard"]),
      S("AIDC-W2", X2, 110, "Governed autonomy", "contracts · autonomy levels", "human", "Governed autonomy prerequisites", ["Autonomous behaviours publish blast-radius contracts", "Each behaviour can be set to autonomous, review or disabled, and interrupted"]),
      S("OPEN-1", P1, 110, "Approval on drift", "retrains", "human", "Approval on drift-triggered retrains", ["Route drift-triggered retrains through the human approval queue; today the queue gates CI-driven changes"], "planned"),
      S("ER-2", P2, 110, "Identity and tenancy", "SSO · per-user audit", "human", "Identity, tenancy and governance", ["Single sign-on, per-user identity in every audit record, cloud key management and a dedicated authorisation service"], "planned"),
      S("AIDC-W3", P3, 110, "Capability broker", "for agents", "human", "Governance spine and capability broker", ["In design: agent identities with scoped, just-in-time grants and re-authorisation on every hop"], "planned"),

      S("P23", X1, 230, "Scheduler abstraction", "mock · Slurm · Flux", "hpc", "HPC scheduler abstraction", ["Submit training to Slurm or Flux (or a mock) through one scheduler-neutral interface over SSH"]),
      S("P36", X2, 230, "HPC fleet", "discover · approve · place", "hpc", "HPC fleet management", ["Discover clusters, approve them before use, place jobs on the best one, preflight and account GPU-hours"]),
      S("OPEN-2", P1, 230, "Live Flux retrain", "measured queue wait", "hpc", "Live Flux-scheduled retrain", ["Run one complete drift-to-retrain loop as a real Flux-scheduled job and measure the queue wait; the adapter exists"], "planned"),
      S("AIDC-W5", P2, 230, "Suspend and resume", "one service", "hpc", "Suspend and resume", ["In design: one suspend/resume service used by preemption, backfill, carbon shifting and fault recovery"], "planned"),
      S("ER-5", P3, 230, "Fleet digital twin", "predictive autopilot", "hpc", "Futuristic differentiation", ["Fleet digital twin with a spatial view, predictive autopilot and carbon- and cost-aware bursting"], "planned"),

      S("P10", X1, 350, "Inference pipeline", "ingress · router", "data", "Composable inference pipeline", ["Requests flow through ingress, feature transformation and a model router before prediction"]),
      S("R-VLLM", X2, 350, "vLLM serving", "LLM · vision-language", "data", "vLLM and vision-language serving", ["Serve LLMs and vision-language models through vLLM with streaming, health checks and metrics"]),
      S("NG-E5", P1, 350, "Autoscaling", "act on decisions", "data", "Autoscaling actuation", ["Apply scaling decisions automatically, including scale to zero; decisions are computed today"], "planned"),
      S("NG-E1", P2, 350, "KServe activation", "on Kubernetes", "data", "Live KServe activation", ["Deploy models to KServe on Kubernetes; the manifests are generated today"], "planned"),
      S("ADR-0117", P3, 350, "Topology as policy", "prefill · decode", "data", "LLM serving topology as policy", ["Prefill/decode topology and routing as runtime policy with paired latency SLOs"], "planned"),

      S("P40-W1", X1, 470, "Dataset versioning", "pinned revisions", "control", "Data versioning and reproducibility", ["Record immutable dataset revisions and pin a training run to one"]),
      S("P40-W4-6", X2, 470, "Feature store", "versioned features", "control", "Feature store", ["A versioned training-feature store and a serving feature store"]),
      S("NG-A3", P1, 470, "Online features", "read at serve time", "control", "Online feature store for serving", ["Serving that reads feature views directly, removing train/serve skew, with scheduled materialisation"], "planned"),
      S("NG-B7", P2, 470, "Real fine-tuning", "PEFT / LoRA jobs", "control", "Real fine-tuning jobs", ["Run actual PEFT/LoRA fine-tuning on the scheduler with measured evaluation scores"], "planned"),
      S("NG-A8", P3, 470, "One-command reproduction", "code · data · env", "control", "One-command reproduction", ["Rebuild code, data and environment and re-run training from a reproducibility bundle"], "planned"),

      S("R-PG", X1, 590, "Postgres backend", "multi-writer state", "observe", "Postgres datastore backend", ["Run platform state on Postgres instead of SQLite for multi-writer deployments"]),
      S("ER-P1-5", X2, 590, "Event outbox", "admission · OIDC · WORM", "observe", "Enterprise foundations, first slices", ["Helm chart, event outbox, admission queue, OIDC token validation, key rotation and a WORM audit anchor"]),
      S("ER-1", P1, 590, "HA and scale-out", "survive a host loss", "observe", "HA and horizontal scale-out", ["HA Postgres and object storage on Kubernetes, live event-broker fan-out, Redis coordination"], "planned"),
      S("ER-3", P2, 590, "Fleet observability", "thousands of nodes", "observe", "Fleet observability and reporting", ["Node and GPU telemetry across thousands of nodes, long-term metrics and scheduled SLA reports"], "planned"),
      S("ER-4", P3, 590, "One versioned API", "typed settings", "observe", "Cohesion, contract and UI scale", ["One versioned API across domains, typed settings and load and chaos test tiers"], "planned")
    ],
    edges: [
      { id: "g1", from: "P11", to: "AIDC-W2", line: "human" },
      { id: "g2", from: "AIDC-W2", to: "OPEN-1", line: "human", planned: true },
      { id: "g3", from: "OPEN-1", to: "ER-2", line: "human", planned: true },
      { id: "g4", from: "ER-2", to: "AIDC-W3", line: "human", planned: true },
      { id: "c1", from: "P23", to: "P36", line: "hpc" },
      { id: "c2", from: "P36", to: "OPEN-2", line: "hpc", planned: true },
      { id: "c3", from: "OPEN-2", to: "AIDC-W5", line: "hpc", planned: true },
      { id: "c4", from: "AIDC-W5", to: "ER-5", line: "hpc", planned: true },
      { id: "s1", from: "P10", to: "R-VLLM", line: "data" },
      { id: "s2", from: "R-VLLM", to: "NG-E5", line: "data", planned: true },
      { id: "s3", from: "NG-E5", to: "NG-E1", line: "data", planned: true },
      { id: "s4", from: "NG-E1", to: "ADR-0117", line: "data", planned: true },
      { id: "d1", from: "P40-W1", to: "P40-W4-6", line: "control" },
      { id: "d2", from: "P40-W4-6", to: "NG-A3", line: "control", planned: true },
      { id: "d3", from: "NG-A3", to: "NG-B7", line: "control", planned: true },
      { id: "d4", from: "NG-B7", to: "NG-A8", line: "control", planned: true },
      { id: "p1", from: "R-PG", to: "ER-P1-5", line: "observe" },
      { id: "p2", from: "ER-P1-5", to: "ER-1", line: "observe", planned: true },
      { id: "p3", from: "ER-1", to: "ER-3", line: "observe", planned: true },
      { id: "p4", from: "ER-3", to: "ER-4", line: "observe", planned: true }
    ]
  });
})();
