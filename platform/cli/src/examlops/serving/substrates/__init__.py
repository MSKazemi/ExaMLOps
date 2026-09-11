"""Where a servable runs, and how it is rendered there (ADR 0142).

Increment I0 ships the pieces the Kubernetes path needs to stop lying: registry resolution to an
immutable artifact (:mod:`.resolve`), the KServe renderers (:mod:`.kserve`) and offline validation
against the pinned KServe CRD schemas (:mod:`.k8s_schema`). The ``Substrate`` Protocol that merges
``ServingBackend`` and ``EndpointLauncher`` lands here in I1.
"""
