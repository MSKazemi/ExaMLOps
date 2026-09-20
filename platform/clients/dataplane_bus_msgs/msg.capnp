# Cap'n'Proto message types for ExaMLOps ↔ Dataplane bus integration.
# These types are defined locally in ai-productions so the dataplane-bus repo
# remains unmodified. The payloadType integers (5-10) match the agreed
# message type numbering used by the dataplane-bus message envelope.

@0xd7a3f51e9c82b064;

struct VectorReqV1
{
    values @0 :List(Float64);
}

struct VectorResV1
{
    results @0 :List(Float64);
}

struct HpcJobV1
{
    jobId        @0 :Text;
    userId       @1 :UInt32;
    numNodes     @2 :UInt32;
    numCpus      @3 :UInt32;
    partition    @4 :Text;
    walltimeSecs @5 :UInt64;
    timestamp    @6 :UInt64;
    embedding    @7 :List(Float64);
    modelName    @8 :Text;
    alias        @9 :Text;
}

struct HpcInferenceResV1
{
    jobId        @0 :Text;
    modelName    @1 :Text;
    modelVersion @2 :Text;
    alias        @3 :Text;
    prediction   @4 :Float64;
    errorMsg     @5 :Text;
    timestamp    @6 :UInt64;
    runId        @7 :Text;
}

struct RetrainReqV1
{
    modelName   @0 :Text;
    datasetName @1 :Text;
    backendName @2 :Text;
    isDummy     @3 :Bool;
}

struct RetrainResV1
{
    flowRunId @0 :Text;
    statusUrl @1 :Text;
    errorMsg  @2 :Text;
}
