"""Synthetic incident fixtures — the single source of truth.

Each incident carries (a) the ``signal`` the three tools return and (b) the
ground-truth ``label`` used by the eval harness. ``evals/cases.jsonl`` is
generated from these (see ``scripts/gen_cases.py``) so labels never drift.

difficulty:
  clear      — one dependency (or the service itself) is unambiguously at fault.
  ambiguous  — logs and metrics point at different dependencies; a keyword
               baseline that trusts the logs gets these wrong.
  no_data    — no logs, no metric anomaly; correct answer is NO_DATA.

Telemetry is per-service. Each incident always carries signal for its own
service; pass ``per_service={"dep-name": _svc(...)}`` to give an upstream
dependency its own logs and metrics. Anything not defined returns an explicit
empty result rather than the incident service's rows relabelled.
"""

from __future__ import annotations

from typing import Any


def _dep_map(service: str, owning_team: str, *upstream: tuple[str, str]) -> dict[str, Any]:
    return {
        "service": service,
        "owning_team": owning_team,
        "upstream": [{"name": n, "team": t} for n, t in upstream],
    }


def _svc(
    log_lines: list[str],
    error_terms: list[str],
    anomaly: bool,
    summary: str,
    implicated: list[str] | None = None,
) -> dict[str, Any]:
    """One dependency's own telemetry, for an incident's ``per_service`` map."""
    return {
        "logs": {"lines": log_lines, "error_terms": error_terms},
        "metrics": {"anomaly": anomaly, "summary": summary, "implicated": implicated or []},
    }


def _inc(
    id: str,
    service: str,
    scenario: str,
    dep_map: dict[str, Any],
    log_lines: list[str],
    error_terms: list[str],
    anomaly: bool,
    summary: str,
    implicated: list[str],
    expected_escalate_to: str,
    expected_root_cause: str,
    difficulty: str,
    per_service: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": id,
        "service": service,
        "window": "last_30m",
        "scenario": scenario,
        "difficulty": difficulty,
        "signal": {
            "dependency_map": dep_map,
            "logs": {"lines": log_lines, "error_terms": error_terms},
            "metrics": {"anomaly": anomaly, "summary": summary, "implicated": implicated},
            # Telemetry for upstream dependencies, keyed by service name. Absent
            # entries return an explicit empty result — see tools.py. Build these
            # with _svc(); needed for cases where the agent must investigate a
            # dependency directly rather than infer it from the parent's signal.
            "per_service": per_service or {},
        },
        "label": {
            "expected_escalate_to": expected_escalate_to,
            "expected_root_cause": expected_root_cause,
        },
    }


INCIDENTS: list[dict[str, Any]] = [
    # ---- CLEAR: an upstream dependency is unambiguously at fault ------------ #
    _inc(
        "checkout-db-pool", "checkout-api",
        "Checkout API 5xx rate jumped from 0.2% to 8% at 14:05; users can't complete orders.",
        _dep_map("checkout-api", "commerce-core",
                 ("postgres-primary", "database-platform"),
                 ("redis-cluster", "cache-redis"),
                 ("stripe-gateway", "payments")),
        ["ERROR checkout-api: could not get connection from pool; postgres-primary timeout after 5000ms",
         "ERROR checkout-api: HikariPool-1 - Connection is not available"],
        ["postgres-primary", "pool"],
        True, "error_rate 0.2%->8.1%, p99 120ms->2400ms", ["postgres-primary"],
        "database-platform",
        "postgres-primary connection pool exhaustion causing checkout 5xx",
        "clear",
    ),
    _inc(
        "cart-redis-evict", "cart-service",
        "Cart contents intermittently empty for ~15% of users since 09:40.",
        _dep_map("cart-service", "commerce-core", ("redis-cluster", "cache-redis")),
        ["WARN cart-service: redis-cluster GET returned nil after SET (evicted)",
         "ERROR cart-service: redis-cluster maxmemory reached, evicting keys"],
        ["redis-cluster", "evicted"],
        True, "cache_hit_rate 96%->61%, redis evictions 0->4k/s", ["redis-cluster"],
        "cache-redis",
        "redis-cluster hitting maxmemory and evicting active cart keys",
        "clear",
    ),
    _inc(
        "payments-stripe-timeout", "payments-worker",
        "Payment capture jobs backing up; queue depth climbing since 11:10.",
        _dep_map("payments-worker", "payments-core",
                 ("stripe-gateway", "payments"), ("ledger-service", "payments")),
        ["ERROR payments-worker: stripe-gateway request timed out after 30s",
         "ERROR payments-worker: upstream stripe-gateway 504"],
        ["stripe-gateway"],
        True, "capture_latency p50 800ms->29s, success 99%->72%", ["stripe-gateway"],
        "payments",
        "stripe-gateway upstream timeouts stalling payment captures",
        "clear",
    ),
    _inc(
        "search-es-shards", "search-api",
        "Search returns partial/empty results for many queries since 16:20.",
        _dep_map("search-api", "search-platform", ("elasticsearch", "search-platform")),
        ["ERROR search-api: elasticsearch search_phase_execution_exception; 3 shards failed",
         "WARN search-api: elasticsearch cluster health RED"],
        ["elasticsearch", "shards"],
        True, "result_completeness 100%->64%, es_unassigned_shards 0->3", ["elasticsearch"],
        "search-platform",
        "elasticsearch unassigned shards degrading search results",
        "clear",
    ),
    _inc(
        "notif-kafka-isr", "notification-service",
        "Push notifications delayed 10-20 min; delivery lag alarms firing.",
        _dep_map("notification-service", "growth-team", ("kafka-broker", "messaging-kafka")),
        ["ERROR notification-service: kafka-broker NotEnoughReplicasException",
         "WARN notification-service: kafka-broker ISR shrank for topic notifications"],
        ["kafka-broker"],
        True, "consumer_lag 0->1.2M, produce_error_rate 0->3%", ["kafka-broker"],
        "messaging-kafka",
        "kafka-broker ISR shrink blocking notification produce/consume",
        "clear",
    ),
    _inc(
        "profile-auth-401", "user-profile-api",
        "Profile pages failing to load with auth errors for logged-in users since 13:00.",
        _dep_map("user-profile-api", "growth-team", ("auth-service", "identity-auth")),
        ["ERROR user-profile-api: auth-service returned 401 for valid session",
         "ERROR user-profile-api: auth-service token introspection failing"],
        ["auth-service"],
        True, "auth_success 99.9%->78%, 401_rate spike", ["auth-service"],
        "identity-auth",
        "auth-service rejecting valid sessions (introspection failure)",
        "clear",
    ),
    _inc(
        "upload-s3-5xx", "image-upload-service",
        "Image uploads failing for ~30% of users since 10:05.",
        _dep_map("image-upload-service", "media-team", ("object-store", "storage-s3")),
        ["ERROR image-upload-service: object-store PUT returned 503 SlowDown",
         "ERROR image-upload-service: object-store InternalError"],
        ["object-store"],
        True, "upload_success 98%->69%, object-store 5xx 0->6%", ["object-store"],
        "storage-s3",
        "object-store returning 503/500 on PUT, failing uploads",
        "clear",
    ),
    _inc(
        "feed-dns-nxdomain", "feed-service",
        "Feed service can't reach several backends; broad connection failures since 15:30.",
        _dep_map("feed-service", "growth-team", ("dns-resolver", "networking")),
        ["ERROR feed-service: dns-resolver NXDOMAIN for internal.svc.local",
         "ERROR feed-service: name resolution failed via dns-resolver"],
        ["dns-resolver"],
        True, "dependency_error_rate 0.1%->22% across backends", ["dns-resolver"],
        "networking",
        "dns-resolver failing internal name resolution",
        "clear",
    ),
    _inc(
        "order-history-replica-lag", "order-history-api",
        "Order history shows stale/missing recent orders since 12:40.",
        _dep_map("order-history-api", "commerce-core", ("postgres-replica", "database-platform")),
        ["WARN order-history-api: reading from postgres-replica with 40s lag",
         "ERROR order-history-api: postgres-replica replication slot behind"],
        ["postgres-replica"],
        True, "replica_lag 1s->42s, stale_read_rate spike", ["postgres-replica"],
        "database-platform",
        "postgres-replica replication lag serving stale reads",
        "clear",
    ),
    _inc(
        "login-token-rotation", "login-api",
        "Logins failing site-wide starting right after 02:00 key rotation.",
        _dep_map("login-api", "identity-auth", ("token-service", "identity-auth")),
        ["ERROR login-api: token-service JWKS fetch returned old key id",
         "ERROR login-api: token-service signing key rotation incomplete"],
        ["token-service"],
        True, "login_success 99%->12%, invalid_signature errors spike", ["token-service"],
        "identity-auth",
        "token-service botched signing-key rotation invalidating JWTs",
        "clear",
    ),
    _inc(
        "analytics-kafka-lag", "analytics-ingest",
        "Analytics dashboards hours behind; ingestion lag growing since 08:00.",
        _dep_map("analytics-ingest", "data-platform", ("kafka-broker", "messaging-kafka")),
        ["WARN analytics-ingest: kafka-broker consumer group rebalancing repeatedly",
         "ERROR analytics-ingest: kafka-broker offsets commit failed"],
        ["kafka-broker"],
        True, "consumer_lag 5k->3.4M, rebalance_count spike", ["kafka-broker"],
        "messaging-kafka",
        "kafka-broker rebalance storm stalling analytics consumers",
        "clear",
    ),
    # ---- CLEAR: the service itself is at fault (own-team) ------------------- #
    _inc(
        "inventory-npe-deploy", "inventory-service",
        "Inventory counts returning errors right after the 14:00 deploy.",
        _dep_map("inventory-service", "commerce-core",
                 ("postgres-primary", "database-platform"), ("redis-cluster", "cache-redis")),
        ["ERROR inventory-service: NullPointerException at StockController.reserve line 88",
         "ERROR inventory-service: unhandled exception after release v2.3.1"],
        ["nullpointerexception", "release"],
        True, "error_rate 0%->14% immediately post-deploy", [],
        "commerce-core",
        "regression in inventory-service v2.3.1 deploy (NullPointerException)",
        "clear",
    ),
    _inc(
        "reco-oom", "recommendation-api",
        "Recommendation API pods restarting in a loop since the 16:00 rollout.",
        _dep_map("recommendation-api", "ml-serving", ("object-store", "storage-s3")),
        ["ERROR recommendation-api: container OOMKilled, restarting",
         "WARN recommendation-api: heap usage 98% after model load"],
        ["oomkilled", "heap"],
        True, "pod_restarts 0->37, memory_usage 60%->98%", [],
        "ml-serving",
        "recommendation-api memory leak / OOM after model rollout",
        "clear",
    ),
    _inc(
        "session-config-error", "session-service",
        "Session service throwing 500s on startup after a config push at 11:45.",
        _dep_map("session-service", "platform-core", ("redis-cluster", "cache-redis")),
        ["ERROR session-service: invalid config key 'sess.ttl_secs' — expected int got string",
         "FATAL session-service: failed to boot with new config"],
        ["invalid config", "boot"],
        True, "healthy_pods 12->2, 500_rate spike", [],
        "platform-core",
        "bad config push (sess.ttl_secs type) crashing session-service",
        "clear",
    ),
    # ---- AMBIGUOUS: logs mislead; metrics hold the real culprit (baseline wrong) #
    _inc(
        "webhook-outbox-locks", "webhook-dispatcher",
        "Outbound webhooks delayed and retrying; some duplicates since 13:20.",
        _dep_map("webhook-dispatcher", "platform-core",
                 ("redis-cluster", "cache-redis"), ("postgres-primary", "database-platform")),
        ["WARN webhook-dispatcher: redis-cluster rate-limit token wait 200ms",
         "ERROR webhook-dispatcher: retrying delivery (attempt 5)"],
        ["redis-cluster", "retrying"],  # red herring: redis is just the rate limiter
        True, "outbox_write_latency 5ms->900ms, postgres-primary lock_waits 0->3k/s",
        ["postgres-primary"],  # true culprit: DB row-lock contention on outbox table
        "database-platform",
        "postgres-primary lock contention on the webhook outbox table (redis rate-limiting is a symptom)",
        "ambiguous",
    ),
    _inc(
        "reco-featurestore-slow", "rec-ranker",
        "Ranking requests timing out; gateway showing 503s since 17:10.",
        _dep_map("rec-ranker", "ml-serving",
                 ("envoy-gateway", "networking"), ("object-store", "storage-s3")),
        ["ERROR rec-ranker: envoy-gateway 503 upstream_reset_before_response_started",
         "WARN rec-ranker: request deadline exceeded"],
        ["envoy-gateway", "deadline"],  # red herring: envoy just surfaces the slow upstream
        True, "p99 300ms->9s; object-store GET latency 20ms->6s (feature store)",
        ["object-store"],  # true culprit: feature store reads from S3 are slow
        "storage-s3",
        "object-store (feature store) read latency causing rec-ranker timeouts surfaced as envoy 503s",
        "ambiguous",
    ),
    _inc(
        "suggest-es-behind-cache", "search-suggest",
        "Autocomplete suggestions stale and slow since 18:00.",
        _dep_map("search-suggest", "search-platform",
                 ("redis-cluster", "cache-redis"), ("elasticsearch", "search-platform")),
        ["WARN search-suggest: redis-cluster cache miss rate elevated",
         "INFO search-suggest: falling back to elasticsearch on miss"],
        ["redis-cluster", "cache miss"],  # red herring: misses rise because ES is slow
        True, "suggest_latency 40ms->1.8s; elasticsearch query_time 30ms->1.6s",
        ["elasticsearch"],  # true culprit: ES slow -> more cache misses
        "search-platform",
        "elasticsearch query slowdown driving cache misses in search-suggest",
        "ambiguous",
    ),
    # ---- AMBIGUOUS: scenario reads noisy, but signal is consistent (baseline right) #
    _inc(
        "photo-s3-real", "profile-photo-api",
        "Profile photos failing to load; some users also report slow logins.",
        _dep_map("profile-photo-api", "media-team",
                 ("object-store", "storage-s3"), ("auth-service", "identity-auth")),
        ["ERROR profile-photo-api: object-store GET 500 InternalError",
         "ERROR profile-photo-api: object-store throttling GET requests"],
        ["object-store"],  # logs and metrics agree; login slowness is unrelated noise
        True, "photo_load_success 98%->70%; object-store 5xx 0->5%", ["object-store"],
        "storage-s3",
        "object-store GET errors/throttling breaking profile photo loads",
        "ambiguous",
    ),
    _inc(
        "chat-kafka-real", "chat-service",
        "Chat messages delayed; team also just finished a redis cache warm-up.",
        _dep_map("chat-service", "growth-team",
                 ("kafka-broker", "messaging-kafka"), ("redis-cluster", "cache-redis")),
        ["ERROR chat-service: kafka-broker produce timeout for topic messages",
         "WARN chat-service: kafka-broker under-replicated partitions"],
        ["kafka-broker"],  # redis warm-up is a distractor in the scenario, not the cause
        True, "message_deliver_p95 200ms->12s; kafka under_replicated 0->8", ["kafka-broker"],
        "messaging-kafka",
        "kafka-broker under-replicated partitions delaying chat message delivery",
        "ambiguous",
    ),
    _inc(
        "billing-ledger-real", "billing-api",
        "Invoices failing to finalize; stripe latency also slightly elevated.",
        _dep_map("billing-api", "payments-core",
                 ("ledger-service", "payments"), ("stripe-gateway", "payments")),
        ["ERROR billing-api: ledger-service double-entry write rejected (constraint)",
         "ERROR billing-api: ledger-service 500 on POST /entries"],
        ["ledger-service"],  # stripe latency is minor noise; ledger is the real blocker
        True, "invoice_finalize_success 99%->74%; ledger-service 5xx 0->9%", ["ledger-service"],
        "payments",
        "ledger-service rejecting double-entry writes, blocking invoice finalization",
        "ambiguous",
    ),
    # ---- NO_DATA: no logs, no metric anomaly -> refuse to guess ------------- #
    _inc(
        "quiet-slowness", "quiet-service",
        "A user reported the page 'feels slow' but no alarms are firing.",
        _dep_map("quiet-service", "platform-core", ("postgres-primary", "database-platform")),
        [], [], False, "error_rate flat 0.1%, p99 stable 110ms", [],
        "NO_DATA",
        "NO_DATA",
        "no_data",
    ),
    _inc(
        "cron-report-quiet", "cron-reporter",
        "Someone asked to 'check on the reporting job' — no specific symptom.",
        _dep_map("cron-reporter", "data-platform", ("object-store", "storage-s3")),
        [], [], False, "job_success 100%, no error logs in window", [],
        "NO_DATA",
        "NO_DATA",
        "no_data",
    ),
    _inc(
        "legacy-admin-vague", "legacy-admin",
        "Vague ticket: 'admin panel might be acting up sometimes.'",
        _dep_map("legacy-admin", "platform-core", ("auth-service", "identity-auth")),
        [], [], False, "traffic near-zero, no anomalies", [],
        "NO_DATA",
        "NO_DATA",
        "no_data",
    ),
    _inc(
        "healthcheck-probe-quiet", "healthcheck-probe",
        "Routine check requested on the healthcheck probe service.",
        _dep_map("healthcheck-probe", "platform-core"),
        [], [], False, "all green, no anomalies in window", [],
        "NO_DATA",
        "NO_DATA",
        "no_data",
    ),
    _inc(
        "ab-experiment-quiet", "ab-experiment-svc",
        "PM asks whether the experiment service is 'doing okay' before a launch.",
        _dep_map("ab-experiment-svc", "growth-team", ("redis-cluster", "cache-redis")),
        [], [], False, "error_rate 0%, latency nominal", [],
        "NO_DATA",
        "NO_DATA",
        "no_data",
    ),
    # ---- CASCADING: the implicated dependency is itself a victim ------------ #
    # The parent's signal points at a real, genuinely-degraded dependency — but
    # that dependency's OWN telemetry names the true root. Answering correctly
    # requires querying the upstream, not just reading the parent's view.
    _inc(
        "profile-auth-db-cascade", "user-profile-api",
        "Profile pages erroring since 09:15; auth calls are failing.",
        _dep_map("user-profile-api", "growth-team",
                 ("auth-service", "identity-auth"),
                 ("postgres-primary", "database-platform")),
        ["ERROR user-profile-api: auth-service returned 503",
         "ERROR user-profile-api: auth-service upstream unavailable"],
        ["auth-service"],
        True, "auth_success 99.9%->41%; auth-service p99 80ms->9s", ["auth-service"],
        "database-platform",
        "postgres-primary connection exhaustion degrading auth-service, which surfaces as 503s in user-profile-api",
        "cascading",
        per_service={
            "auth-service": _svc(
                ["ERROR auth-service: postgres-primary connection refused (pool exhausted)",
                 "WARN auth-service: falling back to degraded token validation"],
                ["postgres-primary", "pool exhausted"],
                True, "auth-service p99 80ms->9s; postgres-primary wait_time 2ms->8s",
                ["postgres-primary"],
            ),
            "postgres-primary": _svc(
                ["ERROR postgres-primary: FATAL remaining connection slots are reserved"],
                ["connection slots"],
                True, "active_connections 100/100, checkout_wait p99 8s", [],
            ),
        },
    ),
    _inc(
        "search-es-snapshot-cascade", "search-api",
        "Search latency up 20x since 03:40; results still correct but very slow.",
        _dep_map("search-api", "search-platform",
                 ("elasticsearch", "search-platform"),
                 ("object-store", "storage-s3")),
        ["WARN search-api: elasticsearch query took 8100ms",
         "WARN search-api: elasticsearch circuit breaker half-open"],
        ["elasticsearch"],
        True, "search_p99 210ms->8.4s; elasticsearch query_time 190ms->8.1s", ["elasticsearch"],
        "storage-s3",
        "object-store throttling elasticsearch's snapshot restore, saturating its IO and slowing queries",
        "cascading",
        per_service={
            "elasticsearch": _svc(
                ["WARN elasticsearch: snapshot restore from object-store throttled (503 SlowDown)",
                 "WARN elasticsearch: merge throttling, IO saturated by restore"],
                ["object-store", "throttled"],
                True, "es_io_util 30%->99%; object-store GET 503 rate 0->7%",
                ["object-store"],
            ),
        },
    ),
    _inc(
        "payments-ledger-kafka-cascade", "payments-worker",
        "Capture jobs backing up since 22:05; ledger writes timing out.",
        _dep_map("payments-worker", "payments-core",
                 ("ledger-service", "payments"),
                 ("kafka-broker", "messaging-kafka")),
        ["ERROR payments-worker: ledger-service POST /entries timed out after 15s",
         "ERROR payments-worker: ledger-service 504"],
        ["ledger-service"],
        True, "capture_success 99%->58%; ledger-service p99 300ms->15s", ["ledger-service"],
        "messaging-kafka",
        "kafka-broker under-replicated partitions stalling ledger-service's commit log, which times out payments-worker captures",
        "cascading",
        per_service={
            "ledger-service": _svc(
                ["ERROR ledger-service: kafka-broker produce timeout on topic ledger-commits",
                 "WARN ledger-service: blocking on commit-log ack, queue depth 40k"],
                ["kafka-broker", "commit-log"],
                True, "ledger p99 300ms->15s; kafka produce_ack p99 5ms->14s",
                ["kafka-broker"],
            ),
        },
    ),
    _inc(
        "checkout-cache-dns-cascade", "checkout-api",
        "Checkout slow and erroring since 07:20; the cache looks down.",
        _dep_map("checkout-api", "commerce-core",
                 ("redis-cluster", "cache-redis"),
                 ("dns-resolver", "networking")),
        ["ERROR checkout-api: redis-cluster connection failed",
         "WARN checkout-api: redis-cluster unreachable, cache bypassed"],
        ["redis-cluster"],
        True, "checkout_p99 140ms->3.1s; redis_conn_errors 0->1.2k/s", ["redis-cluster"],
        "networking",
        "dns-resolver failing to resolve redis-cluster's endpoint; redis itself is healthy but unreachable",
        "cascading",
        per_service={
            "redis-cluster": _svc(
                [], [], False,
                "redis nodes healthy: mem 40%, 0 evictions, accepting connections on the pods "
                "that can reach them", [],
            ),
            "dns-resolver": _svc(
                ["ERROR dns-resolver: SERVFAIL for redis-cluster.internal.svc",
                 "WARN dns-resolver: upstream resolver timeout"],
                ["servfail", "redis-cluster"],
                True, "dns_error_rate 0.1%->31% for *.internal.svc", [],
            ),
        },
    ),
    _inc(
        "feed-ranker-featureflag-cascade", "feed-service",
        "Feed rendering errors since the 15:00 ranker rollout; ranker calls failing.",
        _dep_map("feed-service", "growth-team",
                 ("ranking-service", "ml-serving"),
                 ("config-service", "platform-core")),
        ["ERROR feed-service: ranking-service 500 internal error",
         "WARN feed-service: ranking-service returned empty ranking"],
        ["ranking-service"],
        True, "feed_error_rate 0%->18% from 15:00; ranking-service 5xx 0->22%", ["ranking-service"],
        "platform-core",
        "config-service served a malformed feature-flag payload that crashes ranking-service on load; ranking-service is a victim of the bad config",
        "cascading",
        per_service={
            "ranking-service": _svc(
                ["ERROR ranking-service: config-service flag 'ranker.weights' failed schema validation",
                 "FATAL ranking-service: cannot initialize model with null weights"],
                ["config-service", "schema validation"],
                True, "ranking-service 5xx 0->22% starting 15:00 (config fetch)", ["config-service"],
            ),
            "config-service": _svc(
                ["WARN config-service: served ranker.weights from cache after failed publish"],
                ["ranker.weights"],
                True, "config_publish_errors 0->3 at 15:00; stale flag served", [],
            ),
        },
    ),
    _inc(
        "upload-s3-iam-cascade", "image-upload-service",
        "Uploads failing since 12:00; object storage is rejecting writes.",
        _dep_map("image-upload-service", "media-team",
                 ("object-store", "storage-s3"),
                 ("auth-service", "identity-auth")),
        ["ERROR image-upload-service: object-store PUT 403 AccessDenied",
         "ERROR image-upload-service: object-store rejected write"],
        ["object-store"],
        True, "upload_success 98%->12%; object-store 403 rate 0->88%", ["object-store"],
        "identity-auth",
        "auth-service issued expired STS credentials after the 12:00 key rotation; object-store correctly rejects them with 403 and is not itself at fault",
        "cascading",
        per_service={
            "object-store": _svc(
                ["INFO object-store: PUT denied — presented credentials expired 11:59"],
                ["accessdenied", "expired"],
                False, "object-store healthy: 5xx 0%, latency nominal; 403s are auth rejections", [],
            ),
            "auth-service": _svc(
                ["ERROR auth-service: STS token minting used rotated-out signing key",
                 "WARN auth-service: issued credentials with past expiry after 12:00 rotation"],
                ["sts", "rotation"],
                True, "expired_credential_rate 0->88% from 12:00", [],
            ),
        },
    ),
    # ---- PARTIAL SIGNAL: logs exist, but there is no incident --------------- #
    # The coarse guardrail ("no logs AND no anomaly") does NOT fire here, so the
    # model itself has to decide that present-but-benign signal isn't a fault.
    _inc(
        "gateway-client-4xx", "api-gateway",
        "Dashboard shows a wall of errors in the gateway logs; is something broken?",
        _dep_map("api-gateway", "platform-core", ("auth-service", "identity-auth")),
        ["WARN api-gateway: 401 Unauthorized for /v1/orders (missing bearer token)",
         "WARN api-gateway: 404 Not Found for /v1/typo-endpoint",
         "WARN api-gateway: 400 Bad Request — malformed JSON body"],
        ["401", "404", "400"],
        False, "5xx_rate flat 0.02%, p99 90ms stable, request_volume nominal", [],
        "NO_DATA",
        "NO_DATA",
        "partial_signal",
    ),
    _inc(
        "deploy-window-blip", "orders-api",
        "Someone noticed errors around the 02:00 deploy; is the rollout bad?",
        _dep_map("orders-api", "commerce-core", ("postgres-primary", "database-platform")),
        ["INFO orders-api: rolling update 4/4 pods ready",
         "WARN orders-api: 3 in-flight requests dropped during pod drain",
         "INFO orders-api: readiness probe passing on all pods"],
        ["drained", "rolling update"],
        False, "error_rate 0.1%->0.4% for 40s at 02:00, recovered; now flat 0.1%", [],
        "NO_DATA",
        "NO_DATA",
        "partial_signal",
    ),
    _inc(
        "single-user-500", "billing-api",
        "A customer emailed a screenshot of one 500 error. Is billing down?",
        _dep_map("billing-api", "payments-core", ("ledger-service", "payments")),
        ["ERROR billing-api: 500 on GET /invoices/9182 — unhandled null in legacy PDF path"],
        ["500"],
        False, "error_rate 0.003% (1 of 31k requests), p99 stable 140ms", [],
        "NO_DATA",
        "NO_DATA",
        "partial_signal",
    ),
    _inc(
        "scheduled-retry-noise", "sync-worker",
        "The sync worker's log is full of retry warnings; is the integration failing?",
        _dep_map("sync-worker", "data-platform", ("object-store", "storage-s3")),
        ["WARN sync-worker: retrying batch 4471 (attempt 2) — expected transient 429",
         "INFO sync-worker: backoff 2s then retry, this is normal for large batches",
         "INFO sync-worker: batch 4471 succeeded on attempt 2"],
        ["retry", "backoff"],
        False, "job_success 100%, throughput nominal, retries within normal band", [],
        "NO_DATA",
        "NO_DATA",
        "partial_signal",
    ),
    _inc(
        "healthcheck-flap-recovered", "edge-proxy",
        "One pod flapped its health check at 04:12; do we need to page networking?",
        _dep_map("edge-proxy", "platform-core", ("dns-resolver", "networking")),
        ["WARN edge-proxy: pod edge-proxy-7f readiness failed once at 04:12",
         "INFO edge-proxy: pod edge-proxy-7f back in rotation at 04:12:20"],
        ["readiness"],
        False, "availability 100% (traffic shifted to healthy pods), no dropped requests", [],
        "NO_DATA",
        "NO_DATA",
        "partial_signal",
    ),
    _inc(
        "canary-rollback-noise", "payments-api",
        "Errors spiked then vanished around the 20:00 canary; is the release bad?",
        _dep_map("payments-api", "payments-core", ("stripe-gateway", "payments")),
        ["WARN payments-api: canary pod v8.0.0 returned 500 (2% of traffic, 20:00-20:03)",
         "INFO payments-api: canary auto-rolled-back at 20:03, all pods on v7.9.4",
         "INFO payments-api: error rate normal since 20:03"],
        ["canary", "rolled-back"],
        False, "error_rate blip 0.1%->0.3% for 3min at 20:00, auto-recovered; now flat 0.1%", [],
        "NO_DATA",
        "NO_DATA",
        "partial_signal",
    ),
    # ---- CONFLICTING: the obvious suspects are exonerated on inspection ----- #
    _inc(
        "inventory-deps-clean", "inventory-service",
        "Inventory reads failing since the 11:30 release; both cache and DB look suspicious.",
        _dep_map("inventory-service", "commerce-core",
                 ("redis-cluster", "cache-redis"),
                 ("postgres-primary", "database-platform")),
        ["ERROR inventory-service: redis-cluster GET returned nil",
         "ERROR inventory-service: postgres-primary query returned 0 rows",
         "ERROR inventory-service: IllegalStateException in StockCache.warm() after v4.1.0"],
        ["redis-cluster", "postgres-primary", "illegalstateexception"],
        True, "error_rate 0%->22% at 11:30 (release v4.1.0); dependency latencies nominal", [],
        "commerce-core",
        "inventory-service v4.1.0 cache-warm regression; redis and postgres are both healthy and merely reflect the empty cache",
        "conflicting",
        per_service={
            "redis-cluster": _svc(
                [], [], False, "hit_rate 94%, evictions 0, p99 0.8ms — nominal", [],
            ),
            "postgres-primary": _svc(
                [], [], False, "p99 12ms, connections 22/100, no slow queries — nominal", [],
            ),
        },
    ),
    _inc(
        "notif-symmetric-suspects", "notification-service",
        "Delivery lag climbing since 16:00; both the queue and the push gateway look implicated.",
        _dep_map("notification-service", "growth-team",
                 ("kafka-broker", "messaging-kafka"),
                 ("push-gateway", "mobile-platform")),
        ["ERROR notification-service: kafka-broker consumer timeout",
         "ERROR notification-service: push-gateway request timeout"],
        ["kafka-broker", "push-gateway"],
        True,
        "deliver_p95 400ms->9s; kafka consumer_lag 0->600k; push-gateway p99 200ms->9s "
        "(both degraded from 16:00, neither leads the other)",
        ["kafka-broker", "push-gateway"],
        "NO_DATA",
        "NO_DATA",
        "conflicting",
        per_service={
            "kafka-broker": _svc(
                ["WARN kafka-broker: consumer group rebalancing"],
                ["rebalancing"],
                True, "consumer_lag 0->600k starting 16:00", [],
            ),
            "push-gateway": _svc(
                ["WARN push-gateway: upstream APNs latency elevated"],
                ["apns"],
                True, "p99 200ms->9s starting 16:00", [],
            ),
        },
    ),
    _inc(
        "reco-metric-artifact", "recommendation-api",
        "Metrics implicate the feature store, but the team says nothing changed there.",
        _dep_map("recommendation-api", "ml-serving",
                 ("object-store", "storage-s3")),
        ["ERROR recommendation-api: model v7 inference threw IndexError on empty feature vector",
         "INFO recommendation-api: deployed model v7 at 13:00"],
        ["indexerror", "model v7"],
        True,
        "reco_error_rate 0%->31% from 13:00; object-store GET p99 reported 20ms->4s "
        "(client-side timer includes local model-load stall)",
        ["object-store"],
        "ml-serving",
        "recommendation-api model v7 mishandles empty feature vectors; the object-store latency is a client-side measurement artifact of the stalled model load",
        "conflicting",
        per_service={
            "object-store": _svc(
                [], [], False,
                "server-side GET p99 18ms, 5xx 0%, throttling 0 — nominal", [],
            ),
        },
    ),
    _inc(
        "cart-suspects-cleared-own-bug", "cart-service",
        "Carts erroring since the 10:00 deploy; cache and DB both named in the logs.",
        _dep_map("cart-service", "commerce-core",
                 ("redis-cluster", "cache-redis"),
                 ("postgres-primary", "database-platform")),
        ["ERROR cart-service: redis-cluster SET rejected",
         "ERROR cart-service: postgres-primary write failed",
         "ERROR cart-service: NumberFormatException in CartTotal.compute() after v3.2.0"],
        ["redis-cluster", "postgres-primary", "numberformatexception"],
        True, "error_rate 0%->27% at 10:00 (deploy v3.2.0); dependency latencies nominal", [],
        "commerce-core",
        "cart-service v3.2.0 introduced a NumberFormatException that aborts the request before the healthy redis and postgres writes can commit",
        "conflicting",
        per_service={
            "redis-cluster": _svc(
                [], [], False, "hit_rate 95%, evictions 0, p99 0.7ms — nominal", [],
            ),
            "postgres-primary": _svc(
                [], [], False, "p99 9ms, connections 18/100, no errors — nominal", [],
            ),
        },
    ),
    _inc(
        "search-two-clean-suspects", "search-api",
        "Search errors since 14:00; both elasticsearch and the gateway are in the logs.",
        _dep_map("search-api", "search-platform",
                 ("elasticsearch", "search-platform"),
                 ("envoy-gateway", "networking")),
        ["ERROR search-api: elasticsearch request failed",
         "ERROR search-api: envoy-gateway 503",
         "ERROR search-api: OutOfMemoryError in QueryPlanner after v5.0.0"],
        ["elasticsearch", "envoy-gateway", "outofmemoryerror"],
        True, "error_rate 0%->19% at 14:00 (deploy v5.0.0); es and envoy latencies nominal", [],
        "search-platform",
        "search-api v5.0.0 leaks memory in the query planner and OOMs; elasticsearch and envoy-gateway are healthy and only see the aborted requests",
        "conflicting",
        per_service={
            "elasticsearch": _svc(
                [], [], False, "cluster GREEN, query_time p99 40ms, 0 rejected — nominal", [],
            ),
            "envoy-gateway": _svc(
                [], [], False, "upstream_5xx 0%, p99 6ms — the 503s are search-api resets", [],
            ),
        },
    ),
    _inc(
        "orders-undecidable-cutover", "orders-api",
        "Orders failing since 18:30, right when both a DB failover and a deploy happened.",
        _dep_map("orders-api", "commerce-core",
                 ("postgres-primary", "database-platform")),
        ["ERROR orders-api: write failed during postgres-primary failover at 18:30",
         "INFO orders-api: deployed v6.1.0 at 18:30",
         "ERROR orders-api: transaction rolled back"],
        ["postgres-primary", "failover", "rolled back"],
        True,
        "error_rate 0%->24% at 18:30; postgres-primary failover AND orders-api v6.1.0 "
        "landed in the same minute — signal cannot separate them",
        ["postgres-primary"],
        "NO_DATA",
        "NO_DATA",
        "conflicting",
        per_service={
            "postgres-primary": _svc(
                ["WARN postgres-primary: failover promoted replica at 18:30, 12s write unavailability"],
                ["failover"],
                True, "write_unavailable 12s at 18:30, recovered; now nominal", [],
            ),
        },
    ),
]


def by_id(incident_id: str) -> dict[str, Any]:
    for inc in INCIDENTS:
        if inc["id"] == incident_id:
            return inc
    raise KeyError(incident_id)
