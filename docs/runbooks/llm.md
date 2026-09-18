# Runbooks: LLM serving

Alerts about vLLM endpoints: the Compose `vllm` server and any fleet endpoint whose target
`exa hpc prometheus-sd` wrote. Background: [LLM serving engines](../guides/llm-serving-engines.md).

`exa serve llm health` probes the configured endpoints; vLLM's own metrics carry the
`vllm:` prefix.

## VLLMEndpointDown {#vllmendpointdown}

**Meaning:** Prometheus has not scraped a vLLM endpoint (`instance` in the alert) for 5 minutes.

**Impact:** requests routed to it through the gateway are likely failing.

**Check:** `exa serve llm health`; for a fleet endpoint, is its batch job still running on the
cluster (`exa hpc jobs`)? A job that ended leaves its target behind until
`exa hpc prometheus-sd` is run again.

**Fix:** restart the endpoint, or re-run `exa hpc prometheus-sd` to drop targets that no longer
exist.

## VLLMKVCacheNearFull {#vllmkvcachenearfull}

**Meaning:** an endpoint's KV-cache usage is above 90 %, sustained for 10 minutes.

!!! note "`vllm:kv_cache_usage_perc` is a fraction, not a percentage"
    Despite the `_perc` suffix vLLM reports `0`–`1`, which is why the rule compares against `0.9`.
    That is vLLM's own convention and this repo cannot verify it — so the replay case
    (`alert_rules_test.yml`) states the assumption: `0.95` fires and `0.85` does not. If a future
    vLLM reported `0`–`100`, the alert would fire at 0.9 % usage and the quiet case is what would
    start looking wrong.

**Impact:** vLLM starts preempting requests, which makes latency spiky and time-to-first-token long.

**Check:** `vllm:num_requests_running` and `vllm:num_requests_waiting` for that instance; the
requests' context lengths.

**Fix:** add capacity (another replica, or a GPU with more memory). Otherwise lower
`--max-num-seqs` or the maximum context length so fewer sequences compete for the cache.

## VLLMQueueBacklog {#vllmqueuebacklog}

**Meaning:** more than 20 requests are waiting on an endpoint, sustained for 10 minutes.

**Impact:** the endpoint is saturated; every new request waits behind the queue.

**Check:** is traffic up, or throughput down (see [VLLMKVCacheNearFull](#vllmkvcachenearfull))?

**Fix:** scale out, or route part of the traffic elsewhere with the gateway's routing rules.

## VLLMHighTTFT {#vllmhighttft}

**Meaning:** the p95 time-to-first-token is above 5 s, sustained for 10 minutes.

**Impact:** users wait seconds before anything appears.

**Check:** queue ([VLLMQueueBacklog](#vllmqueuebacklog)) and cache
([VLLMKVCacheNearFull](#vllmkvcachenearfull)) first; then prompt lengths, since long prompts mean
long prefill.

**Fix:** follow whichever of the two it is. For long prompts, enable prefix caching or shorten the
retrieved context.
