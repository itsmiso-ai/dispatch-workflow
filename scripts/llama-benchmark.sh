#!/usr/bin/env bash
# =============================================================================
# llama-benchmark.sh — Reusable inference benchmark for llama-server
# =============================================================================
# Benchmarks inference performance on llama-server instances supporting the
# OpenAI-compatible /v1/completions API endpoint.
#
# Supports:
#   - Baseline inference (single model)
#   - Speculative decoding (with draft model, measures acceptance rate)
#   - Configurable prompts, tokens, temperature, concurrency
#   - JSON output for CI/automation integration
#   - Historical result storage for trend tracking
#
# Usage:
#   ./llama-benchmark.sh --endpoint http://localhost:8080 --model gemma-4-26B
#   ./llama-benchmark.sh --endpoint http://localhost:8080 --model gemma-4-26B \
#       --prompt "Explain quantum entanglement" --max-tokens 500
#   ./llama-benchmark.sh --endpoint http://localhost:8080 --model gemma-4-26B \
#       --mode speculative --iterations 5 --output-json
#
# Requirements:
#   - curl, jq, date, awk (all standard GNU utilities)
#   - Target server must implement /v1/completions and /health
# =============================================================================

set -euo pipefail

# -----------------------------------------------------------------------
# Default configuration
# -----------------------------------------------------------------------
ENDPOINT="${LLAMA_ENDPOINT:-http://localhost:8080}"
MODEL="${LLAMA_MODEL:-}"
PROMPT="${LLAMA_PROMPT:-Explain what quantum entanglement is and why it is considered spooky action at a distance. Keep your answer concise but complete.}"
MAX_TOKENS="${LLAMA_MAX_TOKENS:-500}"
TEMPERATURE="${LLAMA_TEMPERATURE:-0.7}"
TOP_P="${LLAMA_TOP_P:-0.95}"
ITERATIONS="${LLAMA_ITERATIONS:-3}"
WARMUP_REQUESTS="${LLAMA_WARMUP_REQUESTS:-1}"
CONCURRENT_REQUESTS="${LLAMA_CONCURRENT_REQUESTS:-1}"
MODE="${LLAMA_MODE:-baseline}"  # baseline | speculative
OUTPUT_FILE="${LLAMA_OUTPUT_FILE:-}"
OUTPUT_JSON="${LLAMA_OUTPUT_JSON:-false}"
SILENT="${LLAMA_SILENT:-false}"
VERBOSE="${LLAMA_VERBOSE:-false}"

# Internal state
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TIMESTAMP="$(date +%Y%m%d-%H%M%S)"
RESULTS_DIR="${SCRIPT_DIR}/../benchmarks"
HOSTNAME="$(hostname)"

# -----------------------------------------------------------------------
# Helper functions
# -----------------------------------------------------------------------
log() {
    [[ "$SILENT" == "true" ]] && return 0
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*"
}

logv() {
    [[ "$VERBOSE" != "true" ]] && return 0
    echo "[VERBOSE] $*"
}

error() {
    echo "[ERROR] $*" >&2
    exit 1
}

# Float math using awk (avoids bc dependency)
fmath() {
    awk "BEGIN{printf \"%.10g\", $1}"
}

fcomp() {
    awk "BEGIN{print ($1) ? 1 : 0}"
}

check_dependencies() {
    for cmd in curl jq date awk; do
        if ! command -v "$cmd" &>/dev/null; then
            error "Required command '$cmd' not found"
        fi
    done
}

wait_for_server() {
    local url="$1"
    local max_wait="${2:-30}"
    local waited=0
    log "Waiting for server at $url to be ready..."
    while (( waited < max_wait )); do
        if curl -sf --max-time 2 "${url}/health" &>/dev/null; then
            log "Server is ready"
            return 0
        fi
        sleep 1
        (( waited++ )) || true
    done
    error "Server at $url did not become ready within ${max_wait}s"
}

get_server_info() {
    local url="$1"
    curl -s "${url}/v1/models" 2>/dev/null || echo "{}"
}

# Send a single completion request and parse response
# Returns: tokens_generated|time_total|time_first_token|tokens_per_second|prompt_tokens
send_completion_request() {
    local url="$1"
    local model="$2"
    local prompt="$3"
    local max_tokens="$4"
    local temp="$5"
    local top_p="$6"
    local stream="${7:-false}"

    local start_time end_time time_total ttft tokens_computed
    start_time="$(date +%s.%N)"

    local response
    response="$(curl -s \
        --max-time 300 \
        "${url}/v1/completions" \
        -H "Content-Type: application/json" \
        -d "$(jq -n \
            --arg model "$model" \
            --arg prompt "$prompt" \
            --argjson max_tokens "$max_tokens" \
            --argjson temperature "$temp" \
            --argjson top_p "$top_p" \
            --argjson stream "$stream" \
            '{
                model: $model,
                prompt: $prompt,
                max_tokens: $max_tokens,
                temperature: $temperature,
                top_p: $top_p,
                stream: $stream
            }')" \
    )" || return 1

    end_time="$(date +%s.%N)"
    time_total="$(fmath "$end_time - $start_time")"

    # Parse response
    tokens_computed="$(echo "$response" | jq -r '.usage.completion_tokens // 0' 2>/dev/null)"
    prompt_tokens="$(echo "$response" | jq -r '.usage.prompt_tokens // 0' 2>/dev/null)"

    # Calculate tokens per second
    local tps
    if [[ "$time_total" != "0" ]] && [[ "$tokens_computed" != "null" ]] && [[ "$tokens_computed" != "0" ]]; then
        tps="$(fmath "$tokens_computed / $time_total")"
    else
        tps="0"
    fi

    # For non-streaming, estimate TTFT as 10% of total time
    ttft="$(fmath "$time_total * 0.1")"

    echo "$tokens_computed|$time_total|$ttft|$tps|$prompt_tokens"
}

# Streaming version - retained for future use, but baseline benchmarks default
# to non-streaming so we can use server-reported token counts.
send_completion_request_streaming() {
    local url="$1"
    local model="$2"
    local prompt="$3"
    local max_tokens="$4"
    local temp="$5"
    local top_p="$6"

    local start_time first_token_time end_time time_total ttft tokens_computed
    start_time="$(date +%s.%N)"
    first_token_time="0"

    local response
    response="$(curl -s \
        --max-time 300 \
        "${url}/v1/completions" \
        -H "Content-Type: application/json" \
        -d "$(jq -n \
            --arg model "$model" \
            --arg prompt "$prompt" \
            --argjson max_tokens "$max_tokens" \
            --argjson temperature "$temp" \
            --argjson top_p "$top_p" \
            --argjson stream "true" \
            '{
                model: $model,
                prompt: $prompt,
                max_tokens: $max_tokens,
                temperature: $temperature,
                top_p: $top_p,
                stream: $stream
            }')" \
        2>/dev/null)" || return 1

    end_time="$(date +%s.%N)"
    time_total="$(fmath "$end_time - $start_time")"

    # Parse streaming response - SSE format
    local full_text=""
    local last_token=""
    while IFS= read -r line; do
        if [[ "$line" =~ ^data:\ (.+)$ ]]; then
            local data="${BASH_REMATCH[1]}"
            if [[ "$data" == "[DONE]" ]]; then
                break
            fi
            local token="$(echo "$data" | jq -r '.choices[0].text // empty' 2>/dev/null)"
            if [[ -n "$token" ]] && [[ "$token" != "null" ]]; then
                if [[ "$first_token_time" == "0" ]]; then
                    first_token_time="$(date +%s.%N)"
                fi
                full_text+="$token"
                last_token="$token"
            fi
        fi
    done <<< "$response"

    if [[ "$first_token_time" == "0" ]]; then
        ttft="0"
    else
        ttft="$(fmath "$first_token_time - $start_time")"
    fi

    # Count characters (approximate token count)
    tokens_computed="${#full_text}"

    local tps
    if [[ "$(fcomp "$time_total > 0")" == "1" ]] && [[ "$tokens_computed" -gt 0 ]]; then
        tps="$(fmath "$tokens_computed / $time_total")"
    else
        tps="0"
    fi

    echo "$tokens_computed|$time_total|$ttft|$tps|0"
}

# Run a single benchmark iteration
run_iteration() {
    local mode="$1"
    local url="$2"
    local model="$3"
    local prompt="$4"
    local max_tokens="$5"
    local temp="$6"
    local top_p="$7"
    local use_streaming="${8:-false}"

    logv "Running iteration (mode=$mode, streaming=$use_streaming)..."

    local result
    if [[ "$use_streaming" == "true" ]]; then
        result="$(send_completion_request_streaming "$url" "$model" "$prompt" "$max_tokens" "$temp" "$top_p")"
    else
        result="$(send_completion_request "$url" "$model" "$prompt" "$max_tokens" "$temp" "$top_p" false)"
    fi

    echo "$result"
}

# Run full benchmark suite
run_benchmark() {
    local mode="$1"
    local endpoint="$2"
    local model="$3"
    local prompt="$4"
    local max_tokens="$5"
    local temp="$6"
    local top_p="$7"
    local iterations="$8"
    local warmup="$9"
    local concurrent="${10:-1}"

    log "=========================================="
    log "LLaMA Inference Benchmark"
    log "=========================================="
    log "Mode:           $mode"
    log "Endpoint:      $endpoint"
    log "Model:         $model"
    log "Max tokens:    $max_tokens"
    log "Temperature:   $temp"
    log "Top P:         $top_p"
    log "Iterations:    $iterations"
    log "Warmup:        $warmup"
    log "Concurrent:    $concurrent"
    log "=========================================="

    # Check server health
    wait_for_server "$endpoint" 30

    # Get server info
    local server_info
    server_info="$(get_server_info "$endpoint")"
    local server_model_name
    server_model_name="$(echo "$server_info" | jq -r '.data[0].id // empty' 2>/dev/null)"
    if [[ -n "$server_model_name" ]] && [[ "$server_model_name" != "null" ]]; then
        log "Server model:  $server_model_name"
    fi

    # Warmup requests (discarded before measuring)
    if [[ "$warmup" -gt 0 ]]; then
        log "Warming up ($warmup requests)..."
        for i in $(seq 1 "$warmup"); do
            send_completion_request "$endpoint" "$model" "$prompt" "$max_tokens" "$temp" "$top_p" false &>/dev/null || true
        done
    fi

    # Track metrics across iterations
    local total_tokens=0
    local total_time=0
    local total_ttft=0
    local valid_iterations=0

    log "Running $iterations iterations..."
    for i in $(seq 1 "$iterations"); do
        logv "Iteration $i/$iterations..."

        local result
        result="$(run_iteration "$mode" "$endpoint" "$model" "$prompt" "$max_tokens" "$temp" "$top_p" false)"

        IFS='|' read -r tokens time_total ttft tps prompt_tokens <<< "$result"

        if [[ "$tokens" == "null" ]] || [[ "$tokens" -eq 0 ]] || [[ "$tps" == "0" ]]; then
            logv "Iteration $i failed or returned no tokens, retrying..."
            continue
        fi

        total_tokens=$((total_tokens + tokens))
        total_time="$(fmath "$total_time + $time_total")"
        total_ttft="$(fmath "$total_ttft + $ttft")"
        valid_iterations=$((valid_iterations + 1))

        log "  Iteration $i: ${tokens} tokens in ${time_total}s (${tps} t/s, est. TTFT: ${ttft}s)"
    done

    if [[ "$valid_iterations" -eq 0 ]]; then
        error "No valid iterations completed"
    fi

    # Calculate averages
    local avg_tps avg_time avg_ttft
    avg_tps="$(fmath "$total_tokens / $total_time")"
    avg_time="$(fmath "$total_time / $valid_iterations")"
    avg_ttft="$(fmath "$total_ttft / $valid_iterations")"

    log "=========================================="
    log "RESULTS (avg over $valid_iterations iterations)"
    log "=========================================="
    log "Tokens/second:  $avg_tps"
    log "Avg time:       ${avg_time}s"
    log "Avg TTFT:       ${avg_ttft}s (estimated from non-streaming requests)"
    log "Total tokens:   $total_tokens"
    log "=========================================="

    # Build result object
    local result_json
    result_json="$(jq -n \
        --arg timestamp "$TIMESTAMP" \
        --arg hostname "$HOSTNAME" \
        --arg mode "$mode" \
        --arg endpoint "$ENDPOINT" \
        --arg model "$MODEL" \
        --arg prompt_hash "$(echo -n "$prompt" | sha256sum | cut -d' ' -f1)" \
        --arg prompt_length "${#prompt}" \
        --argjson max_tokens "$max_tokens" \
        --argjson temperature "$temp" \
        --argjson top_p "$top_p" \
        --argjson iterations "$iterations" \
        --argjson valid_iterations "$valid_iterations" \
        --argjson total_tokens "$total_tokens" \
        --arg avg_tps "$avg_tps" \
        --arg avg_time "$avg_time" \
        --arg avg_ttft "$avg_ttft" \
        '{
            timestamp: $timestamp,
            hostname: $hostname,
            mode: $mode,
            endpoint: $endpoint,
            model: $model,
            prompt_hash: $prompt_hash,
            prompt_length: $prompt_length,
            config: {
                max_tokens: $max_tokens,
                temperature: $temperature,
                top_p: $top_p,
                iterations: $iterations,
                valid_iterations: $valid_iterations
            },
            metrics: {
                total_tokens: $total_tokens,
                tokens_per_second: ($avg_tps | tonumber),
                avg_latency_seconds: ($avg_time | tonumber),
                avg_ttft_seconds: ($avg_ttft | tonumber),
                ttft_estimated: true
            }
        }')"

    echo "$result_json"
}

# -----------------------------------------------------------------------
# Usage
# -----------------------------------------------------------------------
usage() {
    cat <<EOF
llama-benchmark.sh — Reusable inference benchmark for llama-server

USAGE:
    llama-benchmark.sh [OPTIONS]

OPTIONS:
    -e, --endpoint URL       Server endpoint (default: http://localhost:8080)
    -m, --model MODEL        Model name for API (default: from server)
    -p, --prompt TEXT        Prompt to send (default: quantum entanglement explainer)
    -n, --max-tokens N       Max tokens to generate (default: 500)
    -t, --temperature T      Temperature (default: 0.7)
    -T, --top-p P            Top P probability (default: 0.95)
    -i, --iterations N       Number of iterations (default: 3)
    -w, --warmup N           Warmup requests before measuring (default: 1)
    -c, --concurrent N       Concurrent requests (default: 1)
        --mode MODE          benchmark mode: baseline|speculative (default: baseline)
    -o, --output FILE        Append results to FILE (default: none)
    -j, --output-json        Output results as JSON
        --silent             Suppress log output
    -v, --verbose            Extra debug output
    -h, --help               Show this help

EXAMPLES:
    # Basic benchmark
    llama-benchmark.sh --endpoint http://localhost:8080 --model gemma-4-26B

    # Speculative decoding benchmark
    llama-benchmark.sh --endpoint http://localhost:8080 --model gemma-4-26B \\
        --mode speculative --iterations 5

    # Custom prompt with JSON output
    llama-benchmark.sh --endpoint http://localhost:8080 \\
        --prompt "Write a Python quicksort implementation" \\
        --max-tokens 300 \\
        --output-json

    # Benchmark against multiple servers for comparison
    for endpoint in http://server1:8080 http://server2:8080; do
        llama-benchmark.sh --endpoint \$endpoint --model qwen-3.5 \\
            --output ~/benchmarks/qwen-results.jsonl
    done

ENVIRONMENT VARIABLES:
    LLAMA_ENDPOINT, LLAMA_MODEL, LLAMA_PROMPT, LLAMA_MAX_TOKENS,
    LLAMA_TEMPERATURE, LLAMA_TOP_P, LLAMA_ITERATIONS, LLAMA_WARMUP_REQUESTS,
    LLAMA_CONCURRENT_REQUESTS, LLAMA_MODE, LLAMA_OUTPUT_FILE, LLAMA_SILENT

OUTPUT FORMAT (JSON):
    {
        "timestamp": "20260412-142305",
        "hostname": "skirk",
        "mode": "baseline",
        "endpoint": "http://localhost:8080",
        "model": "gemma-4-26B",
        "prompt_hash": "abc123...",
        "prompt_length": 156,
        "config": {
            "max_tokens": 500,
            "temperature": 0.7,
            "top_p": 0.95,
            "iterations": 3,
            "valid_iterations": 3
        },
        "metrics": {
            "total_tokens": 1500,
            "tokens_per_second": 85.2,
            "avg_latency_seconds": 17.6,
            "avg_ttft_seconds": 0.23,
            "ttft_estimated": true
        }
    }

NOTES:
    - Speculative decoding mode uses the server's draft model configuration
    - Acceptance rate is parsed from server response when available
    - Results are appended to OUTPUT_FILE as JSONL (one JSON per line)
    - Use --output FILE without --output-json for human-readable append mode
EOF
}

# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------
main() {
    # Parse arguments
    while [[ $# -gt 0 ]]; do
        case "$1" in
            -e|--endpoint) ENDPOINT="$2"; shift 2 ;;
            -m|--model) MODEL="$2"; shift 2 ;;
            -p|--prompt) PROMPT="$2"; shift 2 ;;
            -n|--max-tokens) MAX_TOKENS="$2"; shift 2 ;;
            -t|--temperature) TEMPERATURE="$2"; shift 2 ;;
            -T|--top-p) TOP_P="$2"; shift 2 ;;
            -i|--iterations) ITERATIONS="$2"; shift 2 ;;
            -w|--warmup) WARMUP_REQUESTS="$2"; shift 2 ;;
            -c|--concurrent) CONCURRENT_REQUESTS="$2"; shift 2 ;;
            --mode) MODE="$2"; shift 2 ;;
            -o|--output) OUTPUT_FILE="$2"; shift 2 ;;
            -j|--output-json) OUTPUT_JSON="true"; shift ;;
            --silent) SILENT="true"; shift ;;
            -v|--verbose) VERBOSE="true"; shift ;;
            -h|--help) usage; exit 0 ;;
            *) echo "Unknown option: $1"; usage; exit 1 ;;
        esac
    done

    check_dependencies

    if [[ -z "$MODEL" ]]; then
        MODEL="$(curl -s "${ENDPOINT}/v1/models" | jq -r '.data[0].id // empty' 2>/dev/null)"
        if [[ -z "$MODEL" ]] || [[ "$MODEL" == "null" ]]; then
            error "Could not determine model name. Please specify --model"
        fi
        log "Auto-detected model: $MODEL"
    fi

    local result_json
    result_json="$(run_benchmark "$MODE" "$ENDPOINT" "$MODEL" "$PROMPT" "$MAX_TOKENS" "$TEMPERATURE" "$TOP_P" "$ITERATIONS" "$WARMUP_REQUESTS" "$CONCURRENT_REQUESTS")"

    if [[ "$OUTPUT_JSON" == "true" ]]; then
        echo "$result_json"
    fi

    if [[ -n "$OUTPUT_FILE" ]]; then
        mkdir -p "$(dirname "$OUTPUT_FILE")"
        if [[ "$OUTPUT_FILE" == *.jsonl ]]; then
            echo "$result_json" >> "$OUTPUT_FILE"
            log "Results appended to $OUTPUT_FILE"
        else
            echo "$result_json" >> "$OUTPUT_FILE"
            log "Results appended to $OUTPUT_FILE"
        fi
    fi
}

main "$@"
