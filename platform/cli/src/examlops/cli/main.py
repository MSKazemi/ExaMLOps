from __future__ import annotations

import typer

from examlops.cli import _output
from examlops.cli.commands import (
    approvals,
    config_cmd,
    drift,
    models,
    modelzoo,
    pipeline,
    predict,
    production,
    retrain,
    scaffold,
    seanerbus_cmd,
    serve,
    stack,
    status,
)
from examlops.cli.commands import (
    audit as audit_cmd,
)
from examlops.platform_db import init_db as _init_platform_db

app = typer.Typer(
    name="exa",
    help="ExaMLOps platform CLI — manage models, training, inference, and services.",
    no_args_is_help=True,
    rich_markup_mode="rich",
    context_settings={"help_option_names": ["-h", "--help"]},
)


@app.callback()
def main(json: bool = typer.Option(False, "--json", help="Output raw JSON (for scripting)")):
    _output.json_mode = json
    try:
        _init_platform_db()
    except Exception:
        pass  # non-fatal: DB may not be writable in some envs


app.add_typer(approvals.app,  name="approvals", help="Sysadmin approval gate")
app.add_typer(drift.app,      name="drift",     help="Prediction drift detection")
app.add_typer(models.app,     name="models",    help="MLflow model registry")
app.add_typer(modelzoo.app,   name="modelzoo",  help="ModelZoo repository freshness and events")
app.add_typer(production.app, name="production", help="Production deployment and verification workflows")
app.add_typer(serve.app,      name="serve",     help="Ray Serve operations")
app.add_typer(pipeline.app,   name="pipeline",  help="Prefect training pipeline")
app.add_typer(stack.app,      name="stack",     help="Docker Compose stack")
app.add_typer(config_cmd.app,    name="config",     help="CLI configuration")
app.add_typer(seanerbus_cmd.app, name="seanerbus", help="SeanerBUS bridge UUID management")

app.command("retrain",  epilog=retrain._EXAMPLES)(retrain.retrain)
app.command("predict",  epilog=predict._EXAMPLES)(predict.predict)
app.command("scaffold", epilog=scaffold._EXAMPLES)(scaffold.scaffold)
app.command("status",   epilog=status._EXAMPLES)(status.status)
app.command("audit",    epilog=audit_cmd._EXAMPLES)(audit_cmd.audit)
