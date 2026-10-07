#!/usr/bin/env bash
# Throttle the local Docker stack to emulate the ADR-009 low-end target
# (2 vCPU / 2 GB-class box), so latency and memory baselines recorded on a
# big dev box stay comparable to what self-hosters on small x86 actually feel.
#
# Design (see docs/7-DEVELOPMENT/perf-audit.md):
#   - CPU capping via `docker update --cpus`. The API + worker + Next.js node
#     server share one container, so capping the container caps all three.
#   - Memory capping via `docker update --memory`. Set at container level;
#     SurrealDB and Ollama get separate (lower) allocations, mirroring a real
#     2 GB box where the app, DB and inference engine compete for RAM.
#   - Ollama runs on the host and is NOT throttled here (it owns the local
#     embedding/LLM inference); document that in the audit doc as a known
#     emulation caveat.
#
# Usage:
#   scripts/throttle.sh apply [cpus]         # clamp stack to <cpus> vCPUs (default 2)
#   scripts/throttle.sh restore              # restore the limits captured at apply time
#   scripts/throttle.sh status               # show current per-container limits
#
# Environment overrides:
#   THROTTLE_APP_CPUS / THROTTLE_DB_CPUS / THROTTLE_OLLAMA_CPUS (default 2 / 1 / 2)
#   THROTTLE_APP_MEM / THROTTLE_DB_MEM / THROTTLE_OLLAMA_MEM (default 1280m / 512m / 768m)
#
# NOTE: limits are applied to the LIVE running stack and reset on `restore`.
# The original limits are captured in a state file on apply.

set -euo pipefail

APP="open-notebook-open_notebook-1"
DB="open-notebook-surrealdb-1"
STATE="/tmp/opencode-throttle-state.txt"

APP_CPUS="${THROTTLE_APP_CPUS:-2}"
DB_CPUS="${THROTTLE_DB_CPUS:-1}"
APP_MEM="${THROTTLE_APP_MEM:-1280m}"
DB_MEM="${THROTTLE_DB_MEM:-512m}"

container_exists() {
    docker inspect "$1" >/dev/null 2>&1
}

snapshot() {
    # Capture current --cpus and --memory so `restore` brings them back.
    docker inspect -f '{{.HostConfig.CpuQuota}} {{.HostConfig.CpuPeriod}} {{.HostConfig.Memory}}' "$1"
}

apply_limits() {
    local container="$1" cpus="$2" mem="$3"
    echo "[throttle] $container -> cpus=$cpus mem=$mem"
    # Set --memory-swap equal to --memory (swap disabled, matching a small box
    # with little/no swap). Passing only --memory fails on a container whose
    # swap limit was never set: "Memory limit should be smaller than already
    # set memoryswap limit, update the memoryswap at the same time".
    docker update --cpus "$cpus" --memory "$mem" --memory-swap "$mem" "$container" >/dev/null
}

case "${1:-status}" in
    apply)
        for c in "$APP" "$DB"; do
            if container_exists "$c"; then
                snapshot "$c" >>"$STATE"
                echo "$c" >>"$STATE"
            else
                echo "[throttle] WARNING: $c not running; skipping" >&2
            fi
        done
        apply_limits "$APP" "$APP_CPUS" "$APP_MEM"
        if container_exists "$DB"; then
            apply_limits "$DB" "$DB_CPUS" "$DB_MEM"
        fi
        echo "[throttle] applied. Stack clamped to ~$((APP_CPUS + DB_CPUS)) vCPUs total."
        ;;
    restore)
        if [[ ! -f "$STATE" ]]; then
            echo "[throttle] no saved state; nothing to restore" >&2
            exit 1
        fi
        # State file alternates <quota> <period> <memory> lines and <container> lines.
        # NOTE: `docker update --memory 0` is a no-op (0 means "unchanged"), so a
        # container that was unlimited at apply time must be recreated to drop the
        # cap. CPU and swap are still reset in place.
        NEED_RECREATE=""
        while IFS= read -r line1 && IFS= read -r container; do
            # shellcheck disable=SC2086
            set -- $line1
            local_quota="$1" local_period="$2" local_mem="$3"
            if ! container_exists "$container"; then
                continue
            fi
            echo "[throttle] restoring $container (quota=$local_quota mem=$local_mem)"
            if [[ "$local_quota" -gt 0 ]]; then
                docker update --cpu-quota "$local_quota" --cpu-period "$local_period" \
                    --memory "$local_mem" --memory-swap "$local_mem" "$container" >/dev/null
            elif [[ "$local_mem" -gt 0 ]]; then
                # No CPU cap originally, but a real memory limit (SurrealDB): restore it.
                docker update --cpu-quota -1 --cpu-period 0 \
                    --memory "$local_mem" --memory-swap "$local_mem" "$container" >/dev/null
            else
                # Unlimited originally (the app): a recreate is the only way back.
                docker update --cpu-quota -1 --cpu-period 0 "$container" >/dev/null
                NEED_RECREATE="$container"
            fi
        done <"$STATE"
        rm -f "$STATE"
        if [[ -n "$NEED_RECREATE" ]]; then
            echo "[throttle] recreating $NEED_RECREATE to clear its memory limit (docker update can't)"
            REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
            ( cd "$REPO_ROOT" && docker compose -p open-notebook up -d --force-recreate open_notebook >/dev/null )
        fi
        echo "[throttle] restored."
        ;;
    status)
        for c in "$APP" "$DB"; do
            if container_exists "$c"; then
                docker stats --no-stream --format "{{.Name}}: mem={{.MemUsage}} cpu={{.CPUPerc}}" "$c"
            fi
        done
        ;;
    *)
        echo "usage: throttle.sh {apply|cpus|restore|status}" >&2
        exit 2
        ;;
esac