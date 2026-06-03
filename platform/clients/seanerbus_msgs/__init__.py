"""
ExaMLOps SeanerBUS message types.

Defines Cap'n'Proto message wrappers for HPC job inference and retrain
requests. The payloadType integers match the agreed seanerbus MessageTypes
enum (5-10) without requiring any changes to the seanerbus repository.
"""

from __future__ import annotations

import os
import time

import capnp

capnp.remove_import_hook()
_schema = capnp.load(os.path.join(os.path.dirname(os.path.abspath(__file__)), "msg.capnp"))

# payloadType integer constants — must match the seanerbus MessageTypes enum ordering.
TYPE_VECTOR_REQ_V1        = 5
TYPE_VECTOR_RES_V1        = 6
TYPE_HPC_JOB_V1           = 7
TYPE_HPC_INFERENCE_RES_V1 = 8
TYPE_RETRAIN_REQ_V1       = 9
TYPE_RETRAIN_RES_V1       = 10


class VectorReqV1:
    def __init__(self, values: list[float]) -> None:
        self.values = values

    @staticmethod
    def from_capnp(msg) -> "VectorReqV1":
        if msg.payloadType != TYPE_VECTOR_REQ_V1:
            raise RuntimeError(f"Expected VectorReqV1 (type {TYPE_VECTOR_REQ_V1}), got {msg.payloadType}")
        with _schema.VectorReqV1.from_bytes(msg.payload) as raw:
            return VectorReqV1(values=list(raw.values))

    def to_capnp(self) -> tuple[int, bytes]:
        msg = _schema.VectorReqV1.new_message()
        msg.init("values", len(self.values))
        for i, v in enumerate(self.values):
            msg.values[i] = v
        return (TYPE_VECTOR_REQ_V1, msg.to_bytes())


class VectorResV1:
    def __init__(self, results: list[float]) -> None:
        self.results = results

    @staticmethod
    def from_capnp(msg) -> "VectorResV1":
        if msg.payloadType != TYPE_VECTOR_RES_V1:
            raise RuntimeError(f"Expected VectorResV1 (type {TYPE_VECTOR_RES_V1}), got {msg.payloadType}")
        with _schema.VectorResV1.from_bytes(msg.payload) as raw:
            return VectorResV1(results=list(raw.results))

    def to_capnp(self) -> tuple[int, bytes]:
        msg = _schema.VectorResV1.new_message()
        msg.init("results", len(self.results))
        for i, v in enumerate(self.results):
            msg.results[i] = v
        return (TYPE_VECTOR_RES_V1, msg.to_bytes())


class HpcJobV1:
    def __init__(
        self,
        job_id: str,
        user_id: int,
        num_nodes: int,
        num_cpus: int = 0,
        partition: str = "",
        walltime_secs: int = 0,
        timestamp: int | None = None,
        embedding: list[float] | None = None,
        model_name: str = "",
        alias: str = "",
    ) -> None:
        self.job_id = job_id
        self.user_id = user_id
        self.num_nodes = num_nodes
        self.num_cpus = num_cpus
        self.partition = partition
        self.walltime_secs = walltime_secs
        self.timestamp = timestamp if timestamp is not None else int(time.time() * 1000)
        self.embedding = embedding if embedding is not None else []
        self.model_name = model_name
        self.alias = alias

    @staticmethod
    def from_capnp(msg) -> "HpcJobV1":
        if msg.payloadType != TYPE_HPC_JOB_V1:
            raise RuntimeError(f"Expected HpcJobV1 (type {TYPE_HPC_JOB_V1}), got {msg.payloadType}")
        with _schema.HpcJobV1.from_bytes(msg.payload) as raw:
            return HpcJobV1(
                job_id=raw.jobId,
                user_id=raw.userId,
                num_nodes=raw.numNodes,
                num_cpus=raw.numCpus,
                partition=raw.partition,
                walltime_secs=raw.walltimeSecs,
                timestamp=raw.timestamp,
                embedding=list(raw.embedding),
                model_name=raw.modelName,
                alias=raw.alias,
            )

    def to_capnp(self) -> tuple[int, bytes]:
        msg = _schema.HpcJobV1.new_message()
        msg.jobId = self.job_id
        msg.userId = self.user_id
        msg.numNodes = self.num_nodes
        msg.numCpus = self.num_cpus
        msg.partition = self.partition
        msg.walltimeSecs = self.walltime_secs
        msg.timestamp = self.timestamp
        msg.init("embedding", len(self.embedding))
        for i, v in enumerate(self.embedding):
            msg.embedding[i] = v
        msg.modelName = self.model_name
        msg.alias = self.alias
        return (TYPE_HPC_JOB_V1, msg.to_bytes())


class HpcInferenceResV1:
    def __init__(
        self,
        job_id: str = "",
        model_name: str = "",
        model_version: str = "",
        alias: str = "",
        prediction: float = 0.0,
        error_msg: str = "",
        timestamp: int | None = None,
        run_id: str = "",
    ) -> None:
        self.job_id = job_id
        self.model_name = model_name
        self.model_version = model_version
        self.alias = alias
        self.prediction = prediction
        self.error_msg = error_msg
        self.timestamp = timestamp if timestamp is not None else int(time.time() * 1000)
        self.run_id = run_id

    @staticmethod
    def from_capnp(msg) -> "HpcInferenceResV1":
        if msg.payloadType != TYPE_HPC_INFERENCE_RES_V1:
            raise RuntimeError(
                f"Expected HpcInferenceResV1 (type {TYPE_HPC_INFERENCE_RES_V1}), got {msg.payloadType}"
            )
        with _schema.HpcInferenceResV1.from_bytes(msg.payload) as raw:
            return HpcInferenceResV1(
                job_id=raw.jobId,
                model_name=raw.modelName,
                model_version=raw.modelVersion,
                alias=raw.alias,
                prediction=raw.prediction,
                error_msg=raw.errorMsg,
                timestamp=raw.timestamp,
                run_id=raw.runId,
            )

    def to_capnp(self) -> tuple[int, bytes]:
        msg = _schema.HpcInferenceResV1.new_message()
        msg.jobId = self.job_id
        msg.modelName = self.model_name
        msg.modelVersion = self.model_version
        msg.alias = self.alias
        msg.prediction = self.prediction
        msg.errorMsg = self.error_msg
        msg.timestamp = self.timestamp
        msg.runId = self.run_id
        return (TYPE_HPC_INFERENCE_RES_V1, msg.to_bytes())


class RetrainReqV1:
    def __init__(
        self,
        model_name: str,
        dataset_name: str,
        backend_name: str = "zenodo",
        is_dummy: bool = False,
    ) -> None:
        self.model_name = model_name
        self.dataset_name = dataset_name
        self.backend_name = backend_name
        self.is_dummy = is_dummy

    @staticmethod
    def from_capnp(msg) -> "RetrainReqV1":
        if msg.payloadType != TYPE_RETRAIN_REQ_V1:
            raise RuntimeError(f"Expected RetrainReqV1 (type {TYPE_RETRAIN_REQ_V1}), got {msg.payloadType}")
        with _schema.RetrainReqV1.from_bytes(msg.payload) as raw:
            return RetrainReqV1(
                model_name=raw.modelName,
                dataset_name=raw.datasetName,
                backend_name=raw.backendName,
                is_dummy=raw.isDummy,
            )

    def to_capnp(self) -> tuple[int, bytes]:
        msg = _schema.RetrainReqV1.new_message()
        msg.modelName = self.model_name
        msg.datasetName = self.dataset_name
        msg.backendName = self.backend_name
        msg.isDummy = self.is_dummy
        return (TYPE_RETRAIN_REQ_V1, msg.to_bytes())


class RetrainResV1:
    def __init__(
        self,
        flow_run_id: str = "",
        status_url: str = "",
        error_msg: str = "",
    ) -> None:
        self.flow_run_id = flow_run_id
        self.status_url = status_url
        self.error_msg = error_msg

    @staticmethod
    def from_capnp(msg) -> "RetrainResV1":
        if msg.payloadType != TYPE_RETRAIN_RES_V1:
            raise RuntimeError(f"Expected RetrainResV1 (type {TYPE_RETRAIN_RES_V1}), got {msg.payloadType}")
        with _schema.RetrainResV1.from_bytes(msg.payload) as raw:
            return RetrainResV1(
                flow_run_id=raw.flowRunId,
                status_url=raw.statusUrl,
                error_msg=raw.errorMsg,
            )

    def to_capnp(self) -> tuple[int, bytes]:
        msg = _schema.RetrainResV1.new_message()
        msg.flowRunId = self.flow_run_id
        msg.statusUrl = self.status_url
        msg.errorMsg = self.error_msg
        return (TYPE_RETRAIN_RES_V1, msg.to_bytes())
