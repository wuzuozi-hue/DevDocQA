# Gateway incident handbook for HTTP 503

When the question endpoint returns HTTP 503, first save the response body and timestamp. The status code alone does not identify a failed dependency or tell you the state of a preceding job. Queue saturation and cache outage are two possible explanations. The next sections explain which observations to collect and list the precise response markers.

A previously accepted job has its own ID and lifecycle. Inspect that ID through the job endpoint if its outcome matters. New questions can be rejected while an earlier job continues to run.

## Immediate troubleshooting checklist

Check the question scheduler for running, queued, and reserved work. Compare those counts with the configured admission capacity and allow active work to drain before retrying a saturated queue.

For a possible cache outage, inspect the dependency readiness check and service logs around the same timestamp. A successful liveness check shows that the API process is responding. Use the error code in the question endpoint response to classify the rejected request.

## Exact markers in the public response

An HTTP 503 with queue_full means the question queue exhausted admission capacity. An HTTP 503 with redis_unavailable means the API could not reach Redis. An existing job can continue even when new submissions are rejected.
