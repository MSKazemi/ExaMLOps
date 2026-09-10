/* Run a model server: the `exa serve llm` lifecycle. Sources: cli/commands/llm_serve_cmd.py,
 * examlops/llm_endpoints.py (the four launchers, resolve_address), engines/config.py
 * (to_vllm_args), slurm-adapter/templates/vllm_serve.sh.tmpl, the compose `vllm` profile,
 * prometheus.yml and the examlops-vllm alert group. */
XM.register("llmserve", {
  title: "Run a model server",
  description: "An operator starts an LLM endpoint. The model's engine block is rendered into vllm serve flags, one of four launchers runs or registers the server, and the endpoint is recorded in the registry. An HPC job publishes its own address, a health probe marks the endpoint ready, and the registry turns it into a gateway route and a Prometheus target.",
  viewBox: [0, 20, 1420, 500],
  zones: [
    { x: 14, y: 40, w: 400, h: 470, label: "Start", line: "human" },
    { x: 426, y: 40, w: 560, h: 470, label: "Substrates", line: "hpc" },
    { x: 998, y: 40, w: 408, h: 470, label: "Registry and use", line: "control" }
  ],
  nodes: [
    { id: "op", x: 80, y: 270, label: "Operator", kind: "person", line: "human" },
    { id: "start", x: 280, y: 270, label: "exa serve llm start", sub: "dry-run · confirm · audit", line: "human",
      info: { title: "Start an endpoint", tasks: ["--dry-run previews the launcher and the vllm flags", "Asks for confirmation, then writes an audit event", "A vision model must set --max-images: an unbounded image count is refused", "--project attributes the endpoint to a workspace"],
        cli: ["exa serve llm start qwen --base-url http://gpu01:8000 --hf-model Qwen/Qwen3-8B", "exa serve llm start qwen --launcher slurm --nodes 2 --gpus 4 --tp 4 --pp 2 --hf-model Qwen/Qwen3-8B --dry-run"] } },
    { id: "engine", x: 280, y: 420, label: "Engine block", sub: "model YAML → vllm flags", line: "control",
      info: { title: "One flag renderer", tasks: ["The engine: block in the model's YAML, overridden by command-line flags", "Rendered into vllm serve flags by one function, used by the Compose service, the HPC job script and the KServe manifest"], cli: ["exa serve llm args qwen"] } },

    { id: "ext", x: 560, y: 120, label: "External", sub: "a server you point at", line: "hpc",
      info: { title: "External (the default)", tasks: ["Registers a server someone else runs; starts nothing", "READY at once; stopping only marks it STOPPED", "Works on a host with no GPU"] } },
    { id: "compose", x: 560, y: 210, label: "Compose", sub: "GPU profile · :18011", line: "hpc",
      info: { title: "Docker Compose", tasks: ["Starts the vllm service behind the vllm profile", "Published on host port 18011", "STARTING until a health probe says READY; the model can take minutes to load"] } },
    { id: "kserve", x: 560, y: 300, label: "KServe", sub: "manifest + server dry run", line: "hpc",
      info: { title: "KServe", tasks: ["Builds an LLMInferenceService manifest with the same vllm flags", "Validates it with a server-side dry run; never applies it", "Apply and delete it with kubectl"], links: [{ text: "Kubernetes serving", href: "guides/kubernetes-serving.md" }] } },
    { id: "hpcjob", x: 560, y: 390, label: "Slurm or Flux job", sub: "Apptainer · Ray · TP/PP", line: "hpc",
      info: { title: "HPC allocation", tasks: ["--launcher slurm submits with sbatch (asking for --gpus per node), --launcher flux with flux batch", "The job script uses srun and scontrol, so the server starts only under Slurm today", "Apptainer runs the vLLM image; several nodes form a Ray cluster; --tp and --pp set the parallelism", "Recorded in hpc_jobs as a serving job; exa serve llm stop cancels it"],
        cli: ["exa hpc jobs"] } },

    { id: "vllm", x: 800, y: 255, label: "vLLM server", sub: "/health · /metrics", kind: "external", line: "hpc",
      info: { title: "vLLM server", tasks: ["OpenAI-compatible chat API", "/health for readiness, /metrics for Prometheus"] } },
    { id: "epfile", x: 800, y: 455, label: "Endpoint file", sub: "<work_dir>/<model>.endpoint", kind: "store", line: "hpc",
      info: { title: "The job publishes its address", tasks: ["Written as soon as the head node is known, named after the endpoint in lower case", "Start and stop remove any file a previous job left", "health, status and chat read it, or fetch it over SSH, and record the address", "The gateway reads it only when the file is visible on its own host", "EXAMLOPS_VLLM_WORK_DIR must be on a filesystem the compute nodes share"] } },
    { id: "health", x: 940, y: 255, label: "Health", icon: "✓", kind: "gate", line: "observe", labelSide: "above",
      info: { title: "Health probe", tasks: ["Probes /health and lists the models the server serves", "Records READY or FAILED", "Exits 1 when not ready, so it works as a deploy gate"], cli: ["exa serve llm health qwen", "exa serve llm status qwen"] } },

    { id: "registry", x: 1140, y: 255, label: "Endpoint registry", sub: "llm_endpoints", kind: "store", line: "control",
      info: { title: "Endpoint registry", tasks: ["One row per endpoint: address, state, launcher, job id, project, modality, engine block", "The dashboard's LLMOps console reads it"], cli: ["exa serve llm list", "exa serve llm stop qwen --dry-run"] } },
    { id: "gateway", x: 1320, y: 140, label: "Gateway route", sub: "named after the endpoint", line: "control",
      info: { title: "A gateway route", tasks: ["Keys, budgets, guardrails, caching and cost now apply to the model", "Stopped, disabled and addressless endpoints are left out"], cli: ["exa gateway chat qwen --message \"hello\""], links: [{ text: "Model gateway", href: "guides/model-gateway.md" }] } },
    { id: "prom", x: 1320, y: 390, label: "Prometheus", sub: "vLLM /metrics · alerts", line: "observe",
      info: { title: "Metrics and alerts", tasks: ["Scrapes the Compose server as the vllm job", "exa hpc prometheus-sd writes a target for each other READY or STARTING endpoint with a recorded, non-loopback address; re-run it when that changes", "Alerts: endpoint down, KV cache near full, queue backlog, high time to first token"],
        cli: ["exa hpc prometheus-sd --out platform/infra/docker-compose/targets/fleet.json"] } }
  ],
  edges: [
    { id: "op-start", from: "op", to: "start", line: "human" },
    { id: "engine-start", from: "engine", to: "start", line: "control", label: "flags", labelDx: 24, labelDy: 4 },
    { id: "start-ext", from: "start", to: "ext", line: "hpc", start: [367, 270], via: [[440, 270], [440, 120]], end: [483, 120] },
    { id: "start-compose", from: "start", to: "compose", line: "hpc", start: [367, 270], via: [[440, 270], [440, 210]], end: [483, 210] },
    { id: "start-kserve", from: "start", to: "kserve", line: "hpc", start: [367, 270], via: [[440, 270], [440, 300]], end: [475, 300] },
    { id: "start-hpc", from: "start", to: "hpcjob", line: "hpc", start: [367, 270], via: [[440, 270], [440, 390]], end: [481, 390] },
    { id: "ext-vllm", from: "ext", to: "vllm", line: "hpc", dashed: true, start: [637, 120], via: [[680, 120], [680, 255]], end: [723, 255] },
    { id: "compose-vllm", from: "compose", to: "vllm", line: "hpc", start: [637, 210], via: [[680, 210], [680, 255]], end: [723, 255] },
    { id: "kserve-vllm", from: "kserve", to: "vllm", line: "hpc", dashed: true, start: [645, 300], via: [[680, 300], [680, 255]], end: [723, 255] },
    { id: "hpc-vllm", from: "hpcjob", to: "vllm", line: "hpc", start: [639, 390], via: [[680, 390], [680, 255]], end: [723, 255] },
    { id: "hpc-epfile", from: "hpcjob", to: "epfile", line: "hpc", start: [560, 418], via: [[560, 455]], end: [709, 455], label: "writes its address", labelAt: 0.72 },
    { id: "start-registry", from: "start", to: "registry", line: "control", start: [280, 243], via: [[280, 78], [1140, 78]], end: [1140, 223], label: "record: state · launcher · job id", labelAt: 0.55 },
    { id: "epfile-registry", from: "epfile", to: "registry", line: "hpc", start: [891, 455], via: [[1140, 455]], end: [1140, 287], label: "first read records it", labelAt: 0.3 },
    { id: "vllm-health", from: "vllm", to: "health", line: "observe", both: true },
    { id: "health-registry", from: "health", to: "registry", line: "observe", label: "READY" },
    { id: "registry-gateway", from: "registry", to: "gateway", line: "control", start: [1219, 245], via: [[1320, 245]], end: [1320, 167], label: "route", labelAt: 0.3 },
    { id: "registry-prom", from: "registry", to: "prom", line: "observe", start: [1219, 268], via: [[1320, 268]], end: [1320, 363], label: "targets", labelAt: 0.7 }
  ]
});
