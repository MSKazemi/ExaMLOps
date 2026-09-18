"""Request and response models of the control-plane HTTP API."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ChangeNotification(BaseModel):
    model_ids: list[str]
    commit_sha: str | None = None
    commit_msg: str | None = None
    changed_files: list[str] = []


class ApprovalEntry(BaseModel):
    id: str
    model_id: str
    commit_sha: str | None
    commit_msg: str | None
    changed_files: list[str]
    status: str
    prefect_run_id: str | None
    reject_reason: str | None
    tenant: str
    requested_by: str
    resolved_by: str | None
    requested_at: str
    resolved_at: str | None


class RejectRequest(BaseModel):
    reason: str | None = None


class RetrainRequest(BaseModel):
    model_name: str = Field(..., description="Registered model name (e.g. 'JPCP')")
    dataset_name: str = Field(..., description="Dataset class name (e.g. 'PM100Dataset')")
    backend_name: str | None = Field(default=None)
    is_dummy: bool = Field(default=False)
    parameters: dict[str, Any] = Field(default_factory=dict)


class RetrainResponse(BaseModel):
    flow_run_id: str
    deployment: str
    status_url: str
    parameters: dict[str, Any]


class FlowRunStatus(BaseModel):
    flow_run_id: str
    state_type: str | None
    state_name: str | None
    is_terminal: bool


class ModelEntry(BaseModel):
    model_name: str
    datasets: list[str]


class CommandView(BaseModel):
    command_id: str
    kind: str
    state: str
    attempts: int
    result: dict[str, Any] | None
    last_error: str | None
    created_at: str
    updated_at: str
    status_url: str
    run_state: str | None = None


class CommandPage(BaseModel):
    items: list[CommandView]
    next_cursor: str | None
