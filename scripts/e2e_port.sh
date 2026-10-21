#!/usr/bin/env bash
# e2e_port.sh — preview-port reservation for the web-ci e2e shards on the SHARED runner host.
#
# Why this exists (#1151): every shard runs its own `vite preview --strictPort`, and the host
# runs shards from MANY PRs concurrently. A port picked by ANY pure formula (run id, job pid,
# modulo arithmetic) can collide — two live jobs can land on one port and each kills the
# other's server at birth ("Port … already in use", observed twice), and a job killed mid-shard
# (runner restart, cancelled wave) leaks its server as a squatter. A formula cannot fix either;
# a RESERVATION held for the job's lifetime can.
#
# The protocol (all cooperating jobs run as the same CI user):
#   acquire lock  flock on $LOCK_ROOT/.acquire.lock — a stable file that is NEVER unlinked.
#                The ENTIRE candidate evaluation (fresh mkdir, stale-holder reclamation, the
#                busy probe) runs inside it: every step of the reclaim path (read pid → dead →
#                rm → mkdir) would otherwise be a check-then-act race between two reclaimers
#                that can both pass the dead check and end up owning one port (reproduced on
#                the unsynchronized version — review 5313). Serializing the whole acquire costs
#                milliseconds and closes every interleaving.
#   lock dir      $LOCK_ROOT/<port>, created with atomic `mkdir` — the reservation itself.
#   pid file      $LOCK_ROOT/<port>/pid — the RESERVING SHELL's pid (see the $$ note below),
#                so a later acquire can reclaim the leak of a run killed before its trap ran.
#                Liveness is read from /proc (existence is visible cross-process; a `kill -0`
#                false-negative on a live pid would steal a live lock).
#   busy probe    a /dev/tcp connect — a port already serving a NON-participant (zombie server,
#                foreign process) is skipped, not fought over.
#
# USAGE — source this file from the job's step (a CLI dispatcher is provided for humans, but
# reservations made through a `$(…)` subshell die with the subshell's pid, and that pid is the
# reservation's liveness anchor):
#
#     source ../scripts/e2e_port.sh
#     E2E_PORT="$(e2e_port_acquire "$SHARD")"   # set -e sees a failed acquire (empty output)
#     export E2E_PORT                          # export SEPARATELY: `export X="$(…)"` returns
#                                             # export's status and masks the failure
#     trap 'e2e_port_release "$E2E_PORT"' EXIT
#
# `$$` inside the functions then resolves to the SOURCING shell — the workflow step itself,
# alive for the step's whole life, which is exactly the reservation's lifetime.
#
# Candidates start at E2E_PORT_BASE + (seed % 4000) * 4 + (shard - 1) and advance by 4 (staying
# in the shard's residue class keeps one run's four shards spread across the range), bounded by
# E2E_PORT_SEEK (default 50). The seed is the sourcing shell's pid unless E2E_PORT_SEED
# overrides it — the override exists so tests can force colliding candidates on purpose; the
# seed only picks where the search STARTS, it is never the uniqueness guarantee.
#
# The default range is 16000..31996 — deliberately DISJOINT from every other port protocol on
# this host: the legacy run-id allocation this replaces (46000..61996, still live on every
# branch that predates this change — mixed protocols on one range collided during rollout,
# observed in CI), the playwright config's own fallback (41001-45000), and the kernel's
# ephemeral range (32768+). A reserved port therefore cannot be claimed by a legacy shard or
# a fallback derivation even mid-transition; only a non-participant that happens to bind into
# the range can appear, and the busy probe skips those.

set -euo pipefail

E2E_PORT_LOCK_ROOT="${E2E_PORT_LOCK_ROOT:-/tmp/agent-sessions-e2e-ports}"
E2E_PORT_SEEK="${E2E_PORT_SEEK:-50}"
E2E_PORT_BASE="${E2E_PORT_BASE:-16000}"

# True when something already LISTENS on the port (connect probe; no listener → refused).
e2e_port_busy() {
  (exec 3<>"/dev/tcp/127.0.0.1/$1") >/dev/null 2>&1
}

# Print a reserved preview port for SHARD (1-4) and hold the reservation until
# e2e_port_release. Fails (non-zero, nothing on stdout) when no candidate is free.
#
# The whole search runs under the acquire lock — see the header for why the reclaim path must
# be serialized. flock is released on every exit path (the fd closes with the shell, too).
e2e_port_acquire() {
  local shard="${1:?shard}" seed base cand lock holder pidfile
  seed="${E2E_PORT_SEED:-$$}"
  mkdir -p "$E2E_PORT_LOCK_ROOT"
  local guard="$E2E_PORT_LOCK_ROOT/.acquire.lock"
  local guard_fd
  exec {guard_fd}>"$guard"
  flock "$guard_fd"

  base=$(( E2E_PORT_BASE + (seed % 4000) * 4 + shard - 1 ))
  local i=0
  while (( i < E2E_PORT_SEEK )); do
    cand=$(( base + i * 4 ))
    lock="$E2E_PORT_LOCK_ROOT/$cand"
    pidfile="$lock/pid"
    if mkdir "$lock" 2>/dev/null; then
      echo "$$" > "$pidfile"
      if e2e_port_busy "$cand"; then
        rm -rf "$lock" # occupied by a non-participant: the reservation alone is not enough
      else
        flock -u "$guard_fd"
        echo "$cand"
        return 0
      fi
    else
      # Held. Reclaim only a DEAD holder's leak (a killed run whose trap never fired) — and
      # only inside the acquire lock, where no other reclaimer or fresh mkdir can interleave
      # between the liveness check and the replacement.
      holder="$(cat "$pidfile" 2>/dev/null || true)"
      if [ -n "$holder" ] && [ ! -e "/proc/$holder" ] && ! e2e_port_busy "$cand"; then
        rm -rf "$lock"
        continue # retry the SAME candidate — the atomic mkdir picks exactly one taker
      fi
    fi
    (( i += 1 ))
  done
  flock -u "$guard_fd"
  echo "e2e_port.sh: no free preview port within ${E2E_PORT_SEEK} candidates from ${base}" >&2
  return 1
}

# Release the reservation taken by e2e_port_acquire. Safe on an already-gone lock.
e2e_port_release() {
  [ -n "${1:-}" ] || return 0
  rm -rf "${E2E_PORT_LOCK_ROOT:?}/$1"
}

# Direct execution (humans, ad-hoc probing). CI steps MUST source instead — see the header.
if [ "${BASH_SOURCE[0]}" != "${0}" ]; then
  return 0 2>/dev/null || true
fi

case "${1:-}" in
  acquire) e2e_port_acquire "${2:?shard}" ;;
  release) e2e_port_release "${2:?port}" ;;
  *)
    echo "usage: source e2e_port.sh  |  e2e_port.sh acquire <shard>  |  e2e_port.sh release <port>" >&2
    exit 2
    ;;
esac
