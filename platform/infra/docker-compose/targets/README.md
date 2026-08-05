# Prometheus `file_sd` targets

Generated, not hand-edited. `exa hpc prometheus-sd --out targets/fleet.json` writes the
fleet's scrape targets here — `node_exporter` and DCGM per registered HPC node, plus any
running vLLM endpoint from the `llm_endpoints` registry (Track V / ADR 0107).

Prometheus re-reads this directory every 30s (`refresh_interval`), so a newly-launched
endpoint on a scheduler-allocated node starts being scraped without a config change or a
reload.

    exa serve llm start qwen-vl --launcher slurm --nodes 2 --gpus 4
    exa hpc prometheus-sd --out platform/infra/docker-compose/targets/fleet.json
