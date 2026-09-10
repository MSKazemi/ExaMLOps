/* Follow a cluster job: the HPC fleet layer. Sources: exa hpc detect/nodes/connect/approve/
 * place/preflight/jobs/capacity (platform/cli/.../hpc_cmd.py), examlops/hpc_registry.py,
 * hpc_placement.py, hpc_capacity.py, the scheduler adapters and pipeline_generator's
 * slurm_submit/slurm_wait tasks. */
XM.register("hpc", {
  title: "Follow a cluster job",
  description: "An operator discovers and registers a cluster, a sysadmin approves it, placement picks the best approved cluster, a preflight checks it, the pipeline submits the training job through the scheduler adapter, the job is recorded, and its GPU-hours feed cost and carbon accounting.",
  viewBox: [0, 20, 1400, 640],
  zones: [
    { x: 14, y: 40, w: 420, h: 590, label: "Admit a cluster", line: "human" },
    { x: 452, y: 40, w: 520, h: 590, label: "Place and run", line: "hpc" },
    { x: 990, y: 40, w: 396, h: 590, label: "Account", line: "observe" }
  ],
  nodes: [
    { id: "op", x: 90, y: 150, label: "Operator", kind: "person", line: "human" },
    { id: "detect", x: 300, y: 110, label: "Detect", sub: "exa hpc detect · nodes", line: "hpc",
      info: { title: "Discover a scheduler", sub: "Read-only", tasks: ["Probes Slurm, Flux or NVIDIA GPUs over SSH or locally", "Prints capabilities and a suggested configuration; saves nothing", "exa hpc nodes --save stores a node inventory used by placement"],
        cli: ["exa hpc detect login.example.org", "exa hpc nodes --host login.example.org --save --cluster cluster-a", "exa hpc gpus"] } },
    { id: "connect", x: 300, y: 250, label: "Connect", sub: "registers as PENDING", line: "hpc",
      info: { title: "Register a cluster", tasks: ["Probes the host and records a fingerprint of the SSH client key given with --key", "Writes the connection to clusters.yaml and the cluster as PENDING", "Audits cluster_connect_requested"],
        cli: ["exa hpc connect login.example.org --name cluster-a --user me --key ~/.ssh/id_ed25519"] } },
    { id: "registry", x: 300, y: 400, w: 190, label: "Cluster registry", sub: "PENDING · ACTIVE · REJECTED", kind: "store", line: "hpc",
      info: { title: "Cluster registry", sub: "clusters.yaml (connection) + hpc_clusters (governance state)",
        tasks: ["Only ACTIVE clusters can be placed on", "Re-probing a cluster never revokes an approval"], cli: ["exa hpc clusters"] } },
    { id: "sysadmin", x: 90, y: 540, label: "Sysadmin", kind: "person", line: "human" },
    { id: "approve", x: 300, y: 540, label: "Approve", icon: "✓", kind: "gate", line: "human", labelSide: "right",
      info: { title: "Approve or reject", tasks: ["Moves the cluster to ACTIVE (or REJECTED) and records who approved it", "Audited as cluster_approved"], human: ["Decide whether the platform may use this cluster"],
        cli: ["exa hpc approve cluster-a", "exa hpc reject cluster-a --reason \"…\""] } },

    { id: "place", x: 560, y: 400, label: "Place", sub: "choose_cluster", line: "hpc",
      info: { title: "Placement", tasks: ["Keeps ACTIVE clusters that can fit the request (GPUs, nodes)", "Scores them by headroom: idle GPUs first, then idle nodes", "The scoring function is a swappable provider"], cli: ["exa hpc place --gpus 2"] } },
    { id: "preflight", x: 560, y: 250, label: "Preflight", sub: "exa hpc preflight", line: "hpc",
      info: { title: "Preflight", tasks: ["Requires an ACTIVE cluster", "Runs discovery checks over SSH; exits 1 on any failure", "A separate step: run it before, or as a CI gate"], cli: ["exa hpc preflight cluster-a --gpus 2"] } },
    { id: "run", x: 760, y: 400, label: "Pipeline run", sub: "--cluster name | auto", line: "control",
      info: { title: "Run training on a cluster", tasks: ["--cluster auto asks placement for the best cluster", "Refuses any cluster that is not ACTIVE", "Exports the scheduler, transport and SSH settings, then runs the flow"],
        cli: ["exa pipeline run --model JPCP --dataset PM100Dataset --cluster auto"] } },
    { id: "adapter", x: 760, y: 250, label: "Scheduler adapter", sub: "sbatch · flux batch", line: "hpc",
      info: { title: "Submit and wait", tasks: ["Slurm: sbatch, then squeue and sacct", "Flux: flux batch, then flux jobs", "Polls every 10 s; gives up after 24 h or 5 unknown states in a row", "Every scheduler command is capped at 30 s"], links: [{ text: "Slurm adapter", href: "components/slurm-adapter.md" }] } },
    { id: "cluster", x: 760, y: 100, label: "HPC cluster", sub: "login + compute nodes", kind: "external", line: "hpc" },

    { id: "jobs", x: 1080, y: 250, label: "Job record", sub: "hpc_jobs", kind: "store", line: "observe",
      info: { title: "Job record", tasks: ["Job id, scheduler, flow run, model, dataset, nodes, GPUs and CPUs", "State, start, end and exit code as the job runs", "The MLflow run is tagged with the HPC job id"], cli: ["exa hpc jobs", "exa hpc queue --cluster cluster-a"] } },
    { id: "capacity", x: 1080, y: 400, label: "Capacity and cost", sub: "GPU-hours · $", line: "observe",
      info: { title: "Capacity and cost", tasks: ["Utilisation from the node inventory", "GPU-hours summed from job records, priced per GPU-hour", "exa models cost --record reads scheduler accounting for each model version"], cli: ["exa hpc capacity", "exa models cost jpcp --record"] } },
    { id: "carbon", x: 1290, y: 400, label: "Carbon", sub: "kWh · CO₂e", line: "observe",
      info: { title: "Carbon accounting", tasks: ["Converts GPU- and CPU-hours to energy and emissions with a swappable provider (grid intensity, PUE, TDP)", "Records the estimate with the provider that produced it"], cli: ["exa finops carbon record JPCP --gpu-hours 12", "exa finops carbon providers"], links: [{ text: "FinOps providers", href: "guides/finops-providers.md" }] } },
    { id: "prom", x: 1080, y: 550, label: "Fleet metrics", sub: "Prometheus targets", line: "observe",
      info: { title: "Fleet metrics", tasks: ["Writes Prometheus file-discovery targets: node and GPU exporters from saved node inventories, vLLM from the LLM endpoint registry", "Prometheus re-reads its targets directory every 30 s; re-run the command to refresh it"], cli: ["exa hpc prometheus-sd --out platform/infra/docker-compose/targets/fleet.json"] } }
  ],
  edges: [
    { id: "op-detect", from: "op", to: "detect", line: "human", start: [118, 150], via: [[170, 150], [170, 110]] },
    { id: "detect-cluster", from: "detect", to: "cluster", line: "hpc", dashed: true, start: [300, 84], via: [[300, 47], [680, 47], [680, 100]], end: [684, 100], label: "probe over SSH", labelAt: 0.45 },
    { id: "op-connect", from: "op", to: "connect", line: "human", start: [118, 150], via: [[170, 150], [170, 250]] },
    { id: "connect-registry", from: "connect", to: "registry", line: "hpc", label: "PENDING", labelDx: 36, labelDy: 4 },
    { id: "sysadmin-approve", from: "sysadmin", to: "approve", line: "human" },
    { id: "approve-registry", from: "approve", to: "registry", line: "human", label: "ACTIVE", labelDx: 32, labelDy: 4 },
    { id: "registry-place", from: "registry", to: "place", line: "hpc", label: "ACTIVE only" },
    { id: "place-preflight", from: "place", to: "preflight", line: "hpc", both: true, label: "check", labelDx: 26, labelDy: 4 },
    { id: "place-run", from: "place", to: "run", line: "hpc", label: "best fit" },
    { id: "run-adapter", from: "run", to: "adapter", line: "control", label: "submit", labelDx: 28, labelDy: 4 },
    { id: "adapter-cluster", from: "adapter", to: "cluster", line: "hpc", both: true, label: "job", labelDx: 20, labelDy: 4 },
    { id: "adapter-jobs", from: "adapter", to: "jobs", line: "observe", label: "record" },
    { id: "jobs-capacity", from: "jobs", to: "capacity", line: "observe", label: "GPU-hours", labelDx: 42, labelDy: 4 },
    { id: "capacity-carbon", from: "capacity", to: "carbon", line: "observe" },
    { id: "registry-prom", from: "registry", to: "prom", line: "observe", start: [300, 426], via: [[300, 600], [1080, 600]], end: [1080, 574], label: "exporters", labelAt: 0.6, weight: 3.5 }
  ]
});
