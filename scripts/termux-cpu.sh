#!/data/data/com.termux/files/usr/bin/sh
# CPU topology and the inference budget derived from it. Sourced, not run.
#
#   . "$HOME/bin/onb-cpu"
#   cpu_threads chat
#
# Why this is a sourced library
# -----------------------------
# Three things need the same numbers and must not disagree: the chat server's
# thread flags, the embedder's, and the supervisor's reporting. A second
# derivation in any of them is a second answer, and this repo has spent a
# session on how well that goes.
#
# The constraint it encodes
# -------------------------
# "Do not use the whole CPU as a whole." Not "avoid the fast cores" - the fast
# cores are there to be used. The budget is a fraction of *total* cores, so the
# same derivation holds on an 8-core floor of Cortex-A53s and on a 4x A73 +
# 4x A53 phone, and on both leaves the same share for Android, the UI, the
# four guest services and the browser.
#
# What makes a better phone better here
# --------------------------------------
# Same budget, faster cores. Threads are pinned to the big cluster when
# `taskset` is available, so the work that does run is worth more per thread
# rather than there being more of it. Multitasking stays feasible because the
# load is identical - the hardware underneath it is what changed.

set -u

# Cores kept for everything that is not inference: Android, the WebView, the
# browser, and the guest's surreal/uvicorn/worker/next. Half the device, and
# never fewer than two - a single-core floor has to leave one core spare at any
# cost, and a device this small cannot afford to be unusable.
CPU_SYSTEM_MIN_CORES=2

# Decode is memory-bandwidth bound: past roughly four threads the extra ones
# wait on the same bus, so more threads cost contention and buy little. Capped
# here rather than tuned per device, because the measurement that would justify
# a higher cap has not been taken yet.
CPU_MAX_DECODE_THREADS=4

# Same reasoning, tighter, because the embedder is a background class that
# shares the budget rather than owning it.
CPU_MAX_EMBED_THREADS=2

# Where the topology is read from. Overridable so the derivation can be tested
# against a simulated floor without a second implementation - the numbers here
# are the ones that decide how hard the phone works, and a derivation that can
# only be exercised on the target is a derivation nobody will re-derive
# correctly.
CPU_SYSFS="${CPU_SYSFS:-/sys/devices/system/cpu}"

# Read the topology. Sets CPU_CORES, CPU_BIG_CORES, CPU_MAX_KHZ, CPU_FAST_MASK.
cpu_detect() {
  CPU_CORES=0
  CPU_BIG_CORES=0
  CPU_MAX_KHZ=0
  CPU_FAST_MASK=""
  for _d in "$CPU_SYSFS"/cpu[0-9]*; do
    [ -d "$_d" ] || continue
    CPU_CORES=$((CPU_CORES + 1))
    _khz=$(cat "$_d/cpufreq/cpuinfo_max_freq" 2>/dev/null) || _khz=0
    case "$_khz" in '' | *[!0-9]*) _khz=0 ;; esac
    [ "$_khz" -gt "$CPU_MAX_KHZ" ] && CPU_MAX_KHZ=$_khz
  done
  [ "$CPU_CORES" -gt 0 ] || CPU_CORES=1

  # The big cluster is the top frequency tier, derived rather than assumed.
  #
  # Not `cpu4-7`: core numbering is a platform convention, not a guarantee, and
  # on the phone in hand cpu0-3 are the A53s and cpu4-7 the A73s. Reading the
  # per-core ceiling costs one `cat` per core and is correct wherever the
  # clusters are numbered.
  CPU_BIG_CORES=0
  CPU_FAST_MASK=""
  if [ "$CPU_MAX_KHZ" -gt 0 ]; then
    # 90% of the top tier: tight enough that a modest big/little gap still
    # separates the clusters, loose enough to survive a vendor that reports
    # the two tiers a few percent apart.
    _floor=$((CPU_MAX_KHZ * 9 / 10))
    for _d in "$CPU_SYSFS"/cpu[0-9]*; do
      [ -d "$_d" ] || continue
      _khz=$(cat "$_d/cpufreq/cpuinfo_max_freq" 2>/dev/null) || _khz=0
      case "$_khz" in '' | *[!0-9]*) _khz=0 ;; esac
      if [ "$_khz" -ge "$_floor" ]; then
        CPU_BIG_CORES=$((CPU_BIG_CORES + 1))
        CPU_FAST_MASK="${CPU_FAST_MASK}${CPU_FAST_MASK:+,}${_d##*cpu}"
      fi
    done
  fi
  # Uniform device - every core is a big core, which is the same answer and
  # means no affinity is worth setting.
  [ "$CPU_BIG_CORES" -eq "$CPU_CORES" ] && CPU_FAST_MASK=""
  export CPU_CORES CPU_BIG_CORES CPU_MAX_KHZ CPU_FAST_MASK CPU_SYSFS
}

# Total threads inference may use at once, across every class.
cpu_inference_budget() {
  _sys=$((CPU_SYSTEM_MIN_CORES))
  _half=$((CPU_CORES / 2))
  [ "$_half" -gt "$_sys" ] && _sys=$_half
  _budget=$((CPU_CORES - _sys))
  [ "$_budget" -ge 1 ] || _budget=1
  printf '%s' "$_budget"
}

# Threads for one role.
#
#   chat   - decode threads
#   embed  - embedding threads
#   stt    - whisper transcription threads
#   tts    - piper synthesis threads
#
# Split evenly from the shared budget, which is what makes "chat and embed
# together" bounded rather than each class independently claiming whatever it
# likes. On an 8-core device that is 2 and 2 - the same as the values that were
# hardcoded for the SD680, arrived at rather than inherited.
#
# `stt` and `tts` take the embedder's allocation, not chat's. Both are short,
# single-request media jobs rather than an interactive decode stream, so decode's
# wider ceiling buys nothing here; what matters is that a transcription arriving
# during a podcast render cannot take the whole machine. They are given names
# rather than folded into `*)` so that a typo in a launcher's role argument
# falls through to one thread and is visible, instead of silently inheriting the
# embedder's budget.
cpu_threads() {
  _budget=$(cpu_inference_budget)
  case "${1:-chat}" in
    chat)
      _t=$((_budget / 2))
      [ "$_t" -ge 1 ] || _t=1
      [ "$_t" -le "$CPU_MAX_DECODE_THREADS" ] || _t="$CPU_MAX_DECODE_THREADS"
      ;;
    embed)
      _t=$((_budget / 2))
      [ "$_t" -ge 1 ] || _t=1
      [ "$_t" -le "$CPU_MAX_EMBED_THREADS" ] || _t="$CPU_MAX_EMBED_THREADS"
      ;;
    stt | tts)
      _t=$((_budget / 2))
      [ "$_t" -ge 1 ] || _t=1
      [ "$_t" -le "$CPU_MAX_EMBED_THREADS" ] || _t="$CPU_MAX_EMBED_THREADS"
      ;;
    *)
      _t=1
      ;;
  esac
  printf '%s' "$_t"
}

# Threads for prompt ingestion (`--threads-batch`).
#
# Prefill is compute bound rather than bandwidth bound, so it scales where
# decode does not, and it gets the whole inference share. It is also the one
# consumer that genuinely wants spare capacity - a long source being embedded
# should use the machine when nothing else is asking for it - so taking the full
# share here is what leaves the decode budget intact when it matters.
cpu_batch_threads() {
  _budget=$(cpu_inference_budget)
  [ "$_budget" -le "$CPU_MAX_DECODE_THREADS" ] ||
    _budget="$CPU_MAX_DECODE_THREADS"
  printf '%s' "$_budget"
}

# A `taskset` prefix confining work to the big cluster, or empty.
#
# Empty when there is no big/little split to exploit, when `taskset` is not
# installed, or when the device is homogeneous - in all three cases the kernel
# scheduler already does the right thing, and pinning to every core would be
# no better than not pinning while making the intent harder to see.
cpu_affinity_prefix() {
  [ -n "${CPU_FAST_MASK:-}" ] || { printf ''; return 0; }
  command -v taskset >/dev/null 2>&1 || { printf ''; return 0; }
  # Fail closed: a `taskset` that does not exist as an executable must not
  # become a command word in front of llama-server.
  printf 'taskset -c %s' "$CPU_FAST_MASK"
}

# One-line summary for the operator.
cpu_summary() {
  if [ -n "${CPU_FAST_MASK:-}" ]; then
    printf 'cores %s (%s big: %s) budget %s  affinity: taskset -c %s' \
      "$CPU_CORES" "$CPU_BIG_CORES" "$CPU_FAST_MASK" \
      "$(cpu_inference_budget)" "$CPU_FAST_MASK"
  else
    printf 'cores %s budget %s  chat %s/-b%s  embed %s  stt %s  tts %s' \
      "$CPU_CORES" "$(cpu_inference_budget)" \
      "$(cpu_threads chat)" "$(cpu_batch_threads)" "$(cpu_threads embed)" \
      "$(cpu_threads stt)" "$(cpu_threads tts)"
  fi
}

cpu_detect
