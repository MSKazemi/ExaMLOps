from examlops import observability


def test_pipeline_exposes_tracer():
    import serving.inference_pipeline.app as pipeline

    assert hasattr(pipeline, "_tracer")


def test_setup_tracing_noop_under_test(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "true")
    assert observability.setup_tracing("ray-serving") is False
