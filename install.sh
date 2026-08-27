#!/bin/sh
# agent-sessions installer — rootless, user-level. Idempotent: re-running upgrades in
# place using an atomic release directory + a `current` symlink (one-step rollback).
#
#   curl -fsSL <url>/install.sh | sh
#
# No sudo for the app itself: it installs under ~/.local/share/agent-sessions and runs
# as a `systemctl --user` service. Sudo is used only for optional, clearly-prompted steps:
# installing python3-venv if missing (Debian/Ubuntu, Fedora), and — if you accept the firewall
# offer for a non-localhost bind — adding the ufw/firewalld rule. Binds 127.0.0.1 by
# default; an interactive install offers to bind a chosen address / all interfaces (with a
# warning), derives the reachable origin, and offers to open the port in ufw/firewalld — put a
# reverse proxy / TLS in front (the installer does not configure nginx).
#
# Overridable via env: AGENT_SESSIONS_REPO, AGENT_SESSIONS_REF, AGENT_SESSIONS_CHANNEL
# (stable|main), AGENT_SESSIONS_HOST, AGENT_SESSIONS_PORT, AGENT_SESSIONS_HOME,
# AGENT_SESSIONS_ORIGIN. AGENT_SESSIONS_NO_SERVICE=1 installs without touching systemd.
# Automatic updates are managed in the app (Settings → System → Updates, #538) — there is
# no installer opt-in; a legacy AGENT_SESSIONS_AUTOUPDATE systemd timer is migrated to the
# in-app setting on upgrade.
set -eu

APP=agent-sessions
REPO_URL="${AGENT_SESSIONS_REPO:-https://github.com/teriansilva/agent-sessions.git}"

# ---- release signing trust root (#832) ---------------------------------------------
# EMBEDDED, and deliberately NOT read from the clone being verified. A signer list taken
# from the candidate authenticates nothing: whoever rewrote the ref supplies both the code
# and the list that vouches for it, so the check passes every time. This copy travels with
# install.sh itself — on a fresh install fetched over TLS from the landing origin (a
# different host from the git remote), and on an update carried by the already-verified
# running release.
#
# ONE entry covers every FUTURE release, which a per-release manifest cannot do.
#
# Kept byte-identical to scripts/release-signers by tests/test_release_signing.py.
# Consumed from Phase 2 onward (#832); inert here by design.
RELEASE_SIGNERS='release@agent-sessions ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOjW+gor5BTHMCjx6GWhCOJXdmR9Lei9elkzV7j++zbX agent-sessions release signing'
# Signatures are REQUIRED for any release strictly newer than this. The LAST UNSIGNED
# release is recorded rather than the first signed one because it is a fact today, whereas
# the first signed version number is a guess about a cut nobody has made yet.
RELEASE_LAST_UNSIGNED='v0.19.2'
REF="${AGENT_SESSIONS_REF:-}"
# The commit $REF must resolve to, set by the self-updater from the tag it verified
# (update.py). Empty for a hand-run install, which has no prior verification to bind to.
EXPECT_COMMIT="${AGENT_SESSIONS_EXPECT_COMMIT:-}"
# Track whether the channel was set explicitly (env) vs defaulted: the UI persists a channel
# choice in the env file (#538), and a re-run without the env var must follow that choice
# (adopt_persisted_channel) instead of silently flipping a main-channel install to stable.
CHANNEL_EXPLICIT=0; [ -n "${AGENT_SESSIONS_CHANNEL:-}" ] && CHANNEL_EXPLICIT=1
CHANNEL="${AGENT_SESSIONS_CHANNEL:-stable}"
# Track whether HOST/ORIGIN were set explicitly (env) vs defaulted: an explicit value
# suppresses the interactive bind prompt and the derived-origin recompute (choose_host).
HOST_EXPLICIT=0; [ -n "${AGENT_SESSIONS_HOST:-}" ] && HOST_EXPLICIT=1
HOST="${AGENT_SESSIONS_HOST:-127.0.0.1}"
PORT_EXPLICIT=0; [ -n "${AGENT_SESSIONS_PORT:-}" ] && PORT_EXPLICIT=1
PORT="${AGENT_SESSIONS_PORT:-8765}"
PREFIX="${AGENT_SESSIONS_HOME:-$HOME/.local/share/$APP}"
ORIGIN_EXPLICIT=0; [ -n "${AGENT_SESSIONS_ORIGIN:-}" ] && ORIGIN_EXPLICIT=1
ORIGIN="${AGENT_SESSIONS_ORIGIN:-http://$HOST:$PORT}"
KEEP_RELEASES=3
# Pinned Node used to build the React UI when the host has no new-enough Node. Vendored
# into $PREFIX/.toolchain (no sudo, self-contained) so the install "just works".
NODE_VERSION="${AGENT_SESSIONS_NODE_VERSION:-22.14.0}"
NODE_MIN_MAJOR=20
NPM=npm  # resolved by ensure_node() to the system npm or the vendored one
NODE_BIN=node  # resolved by ensure_node() to the system OR vendored node (companion to $NPM)
# Python toolchain. The app needs CPython >= 3.11. ensure_python() resolves $PY to a system
# python, else vendors a pinned, relocatable standalone CPython (python-build-standalone) into
# $PREFIX/.toolchain — the Python analogue of the vendored Node above (no sudo, no system change).
PY=python3  # resolved by ensure_python() to the system OR vendored interpreter
PY_VERSION="${AGENT_SESSIONS_PYTHON_VERSION:-3.12.13}"
PBS_TAG="${AGENT_SESSIONS_PBS_TAG:-20260602}"  # python-build-standalone release tag for PY_VERSION

RELEASES="$PREFIX/releases"
CURRENT="$PREFIX/current"
ENVF="$PREFIX/env"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/$APP.service"
# Home Free (#27): optional "stream via BattleLab" remote-access channel. OFF by default —
# a plain `curl|sh` stays self-host and never contacts a relay. Opt in with
# AGENT_SESSIONS_REMOTE=stream, or the interactive prompt on a fresh tty install.
REMOTE="${AGENT_SESSIONS_REMOTE:-}"
HOMEFREE_DIR="$PREFIX/homefree"
HOMEFREE_UNIT="$UNIT_DIR/$APP-homefree.service"
# The live credential the agent reads, plus the two off-to-the-side states the lifecycle
# flags move it through (#612). Named here so every function agrees on one spelling.
HOMEFREE_NAME_FILE="$HOMEFREE_DIR/console_name"
HOMEFREE_KEY_FILE="$HOMEFREE_DIR/access_key"
HOMEFREE_PREV_KEY="$HOMEFREE_DIR/access_key.prev"          # rotation's superseded key
HOMEFREE_DISABLED_KEY="$HOMEFREE_DIR/access_key.disabled"  # quarantined by --homefree-disable
HOMEFREE_MODE=""  # "systemd" | "none": decided once per run by homefree_select_mode
HOMEFREE_LOCKDIR="$HOMEFREE_DIR/.lifecycle.lock"   # serializes rotate/disable end to end
HOMEFREE_LOCK_HELD=""
# A Home Free agent as this user: an interpreter running the module, NOT any command line that
# merely mentions it. Defined once so the detector and the recovery command an operator is told
# to run can never drift apart — a `pkill` looser than the detector would kill bystanders.
HOMEFREE_AGENT_RE='^[^[:space:]]*python[0-9.]*[[:space:]].*-m[[:space:]]+agent_sessions[.]homefree([[:space:]]|$)'
# Streamed mode targets the BattleLab public relay + connect page by default, so a plain
# `AGENT_SESSIONS_REMOTE=stream` install is turnkey. Both are overridable via env for
# self-hosters running their own relay / connect page.
HOMEFREE_RELAY_URL="${AGENT_SESSIONS_RELAY_URL:-wss://relay.battlelab.superstatus.io/relay/ws}"
HOMEFREE_CONNECT_URL="${AGENT_SESSIONS_CONNECT_URL:-https://battlelab.superstatus.io/connect}"

log()  { printf '  %s\n' "$*"; }
note() { printf '\n%s\n' "$*"; }
die()  { printf 'error: %s\n' "$*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
_sha256() {  # print the hex SHA-256 of file $1 using whatever tool exists (empty if none)
  if   have sha256sum; then sha256sum "$1" | awk '{print $1}'
  elif have shasum;    then shasum -a 256 "$1" | awk '{print $1}'
  else echo ""
  fi
}

# --- prerequisites: auto-install everything we can, vendor what we can't ----------
# Goal: a self-contained install that "just works". A package the operator can't get
# any other way (a too-old / missing Node) is vendored into $PREFIX with no sudo.

_pkg_install() {  # best-effort distro install of the named packages; returns nonzero if it can't
  if   have apt-get; then sudo apt-get update -qq && sudo apt-get install -y "$@"
  elif have dnf;     then sudo dnf install -y "$@"
  elif have pacman;  then sudo pacman -Sy --noconfirm "$@"
  else return 1
  fi
}

ensure_node() {
  # Resolve $NPM to a Node >= $NODE_MIN_MAJOR. Order: a new-enough system Node > a distro
  # install > a vendored static Node (downloaded into $PREFIX/.toolchain, no sudo).
  _node_ok() { have node && [ "$(node -p 'process.versions.node.split(".")[0]' 2>/dev/null || echo 0)" -ge "$NODE_MIN_MAJOR" ]; }
  if _node_ok && have npm; then NPM=npm; NODE_BIN="$(command -v node)"; return; fi
  log "Node >= $NODE_MIN_MAJOR not found — trying to install it…"
  _pkg_install nodejs npm >/dev/null 2>&1 || true
  if _node_ok && have npm; then NPM=npm; NODE_BIN="$(command -v node)"; return; fi
  # Vendor a pinned static Node — fully self-contained, no sudo, no system change.
  arch="$(uname -m)"
  case "$arch" in
    x86_64|amd64) na=x64 ;;
    aarch64|arm64) na=arm64 ;;
    *) die "no prebuilt Node for arch '$arch' — install Node >= $NODE_MIN_MAJOR and re-run" ;;
  esac
  tdir="$PREFIX/.toolchain"
  ndir="$tdir/node-v$NODE_VERSION-linux-$na"
  if [ ! -x "$ndir/bin/npm" ]; then
    # Supply-chain pin, the same contract ensure_python already applies to the vendored CPython
    # (#612): the expected SHA-256 per supported asset, from that release's SHASUMS256.txt. A
    # `curl | sh` install — which auto-proceeds with no tty — must NOT trust a mutable release
    # URL on TLS alone, so the tarball is verified BEFORE it is unpacked and refused on
    # mismatch. These pins are tied to NODE_VERSION above; bump them together when it changes.
    #
    # AGENT_SESSIONS_NODE_VERSION overriding NODE_VERSION lands in the `*)` case and dies, by
    # design: an unpinned version is exactly the input this verification exists to reject. An
    # operator who wants a different Node installs it on the host — a system Node >=
    # $NODE_MIN_MAJOR is preferred over vendoring and never reaches this path.
    #
    # Resolved INSIDE this branch, not beside the arch case, so it gates only the download. A
    # host that already has some other vendored Node from before this change keeps working on
    # re-run; verification applies to bytes we are about to fetch, which is all a tarball digest
    # can speak to anyway.
    case "$NODE_VERSION-$na" in
      22.14.0-x64)   want_node_sha=9d942932535988091034dc94cc5f42b6dc8784d6366df3a36c4c9ccb3996f0c2 ;;
      22.14.0-arm64) want_node_sha=8cf30ff7250f9463b53c18f89c6c606dfda70378215b2c905d0a9a8b08bd45e0 ;;
      *) die "no pinned checksum for Node $NODE_VERSION ($na) — install Node >= $NODE_MIN_MAJOR on the host and re-run" ;;
    esac
    mkdir -p "$tdir"
    log "fetching a self-contained Node $NODE_VERSION ($na) for the UI build…"
    curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$na.tar.gz" \
      -o "$tdir/node.tar.gz" || die "could not download Node $NODE_VERSION"
    got_node_sha="$(_sha256 "$tdir/node.tar.gz")"
    [ -n "$got_node_sha" ] || { rm -f "$tdir/node.tar.gz"; die "no sha256 tool (sha256sum/shasum) to verify the Node download — install one and re-run"; }
    [ "$got_node_sha" = "$want_node_sha" ] \
      || { rm -f "$tdir/node.tar.gz"; die "Node download checksum mismatch (expected $want_node_sha, got $got_node_sha) — refusing to use it"; }
    tar -xzf "$tdir/node.tar.gz" -C "$tdir" || die "could not unpack Node"
    rm -f "$tdir/node.tar.gz"
  fi
  PATH="$ndir/bin:$PATH"; export PATH   # so the vendored node + vite are found by npm
  NPM="$ndir/bin/npm"
  NODE_BIN="$ndir/bin/node"
}

_confirm() {  # y/n on the controlling tty. Default Yes. Auto-yes via AGENT_SESSIONS_ASSUME_YES=1;
              # no tty to ask on (a non-interactive pipe) → proceed, like the vendored-Node path.
  [ "${AGENT_SESSIONS_ASSUME_YES:-0}" = 1 ] && return 0
  if [ -r /dev/tty ]; then
    printf '%s ' "$1" > /dev/tty
    read _ans < /dev/tty 2>/dev/null || _ans=""
    case "$_ans" in [Nn]*) return 1 ;; *) return 0 ;; esac
  fi
  return 0
}

# --- interactive bind-address selection (#487) ------------------------------------
# By default the app binds 127.0.0.1 and sits behind a reverse proxy (the security model:
# it launches agents with permission bypass, so access ≈ a shell on this host). But a plain
# `curl|sh` install left operators unable to reach it from another machine and unaware of the
# AGENT_SESSIONS_HOST override. choose_host offers an explicit, warned bind choice on a tty;
# non-interactive installs keep the safe localhost default byte-for-byte.

_env_file_get() {  # echo the value of KEY ($1) from the env file (empty when absent)
  [ -f "$ENVF" ] || return 0
  grep "^$1=" "$ENVF" 2>/dev/null | head -1 | cut -d= -f2-
}

_host_ips() {
  # Print this host's routable (non-loopback) IPv4 addresses, one per line, no CIDR suffix,
  # deduped. Rootless and layered: iproute2 `ip` → `hostname -I` → `ifconfig` (BSD/macOS, or an
  # old net-tools `inet addr:` Linux). IPv4-only on purpose: a raw IPv6 literal needs bracketing
  # in an origin (out of scope — set AGENT_SESSIONS_HOST/_ORIGIN by hand for v6), and `0.0.0.0`
  # already covers "all interfaces". Empty output is fine — choose_host then only offers
  # 127.0.0.1 / 0.0.0.0. The trailing awk is the last pipe stage so the function always exits 0
  # (an empty grep mustn't trip `set -e`).
  if have ip; then
    ip -o addr show scope global 2>/dev/null | awk '{print $4}' | cut -d/ -f1
  elif have hostname && hostname -I >/dev/null 2>&1; then
    hostname -I 2>/dev/null | tr ' ' '\n'
  elif have ifconfig; then
    ifconfig 2>/dev/null | awk '/inet /{print $2}' | sed 's/^addr://'
  fi | grep -E '^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$' | grep -v '^127\.' | awk '!seen[$0]++'
}

_primary_ip() {
  # The default-route source address — the IP the OS uses to reach the outside world, i.e. the
  # operator's primary reachable IPv4 on a multi-homed host (docker bridges / VPNs enumerate
  # alongside it in _host_ips, but only one is the default-route source). Empty when iproute2 is
  # absent or there's no default route. `head` is the last pipe stage so the exit status stays 0
  # on empty output (mustn't trip `set -e`).
  have ip || return 0
  ip route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -1
}

_recompute_origin() {  # re-derive ORIGIN from the address the browser will use ($1)…
  [ "$ORIGIN_EXPLICIT" = 1 ] && return 0   # …unless the operator pinned AGENT_SESSIONS_ORIGIN
  ORIGIN="http://$1:$PORT"
}

_offer_firewall() {
  # Binding beyond localhost is pointless if a host firewall drops the port. Offer (default-No,
  # mirroring the bind confirm) to open $1/tcp in whatever firewall is active — ufw (Debian/Ubuntu)
  # or firewalld (Fedora/RHEL); an nftables/iptables-only host (or macOS) just gets the manual rule
  # printed. Firewall changes run as explicit argv (no inline shell interpreter), matching the
  # installer's no-shell-layer model. Best-effort: a declined sudo or a tool error only prints the
  # command — it never fails the install. Reached only from interactive choose_host, so /dev/tty
  # is open.
  _fwport="$1"
  _fwtool=""; _fwcmd=""
  if have ufw; then
    _fwtool=ufw; _fwcmd="sudo ufw allow ${_fwport}/tcp"
  elif have firewall-cmd; then
    _fwtool=firewalld
    _fwcmd="sudo firewall-cmd --permanent --add-port=${_fwport}/tcp && sudo firewall-cmd --reload"
  fi
  if [ -z "$_fwtool" ]; then
    {
      printf '\n  No ufw / firewalld found. If a host firewall is active, allow the port, e.g.:\n'
      printf '     sudo iptables -A INPUT -p tcp --dport %s -j ACCEPT\n' "$_fwport"
    } > /dev/tty
    return 0
  fi
  {
    printf '\n  Other machines also need the host firewall to allow the port:\n'
    printf '     %s\n' "$_fwcmd"
    printf '  Add this rule now (needs sudo)? [y/N] '
  } > /dev/tty
  read _fwyn < /dev/tty 2>/dev/null || _fwyn=""
  case "$_fwyn" in
    [Yy]*) ;;
    *) log "firewall left unchanged — open it later with: $_fwcmd"; return 0 ;;
  esac
  _fwok=1
  if [ "$_fwtool" = ufw ]; then
    sudo ufw allow "${_fwport}/tcp" > /dev/tty 2>&1 || _fwok=0
  else
    { sudo firewall-cmd --permanent --add-port="${_fwport}/tcp" \
        && sudo firewall-cmd --reload; } > /dev/tty 2>&1 || _fwok=0
  fi
  if [ "$_fwok" = 1 ]; then
    log "firewall: opened ${_fwport}/tcp"
  else
    log "firewall: could not add the rule automatically — run it by hand: $_fwcmd"
  fi
}

adopt_persisted_bind() {
  # Re-run / upgrade / autoupdate: the systemd unit bakes `--host`/`--port` from the install-time
  # shell vars, but `serve` only *defaults* to $AGENT_SESSIONS_HOST/_PORT — so a re-run with
  # neither in the environment would regenerate the unit with 127.0.0.1:8765 and silently revert a
  # prior 0.0.0.0 / LAN bind OR a persisted reverse-proxy port (e.g. a proxied :3402 flips to
  # :8765, orphaning the fronting proxy → 502). Adopt the persisted choice from the
  # env file (and treat it as explicit, so choose_host doesn't re-prompt). An env var passed on
  # THIS run still wins. Port is adopted independently of host: a re-run that sets HOST but not
  # PORT must still keep the persisted port.
  if [ "$PORT_EXPLICIT" = 0 ] && [ -f "$ENVF" ]; then
    _pp="$(_env_file_get AGENT_SESSIONS_PORT)"
    if [ -n "$_pp" ]; then PORT="$_pp"; PORT_EXPLICIT=1; fi
  fi
  [ "$HOST_EXPLICIT" = 1 ] && return 0
  [ -f "$ENVF" ] || return 0
  _ph="$(_env_file_get AGENT_SESSIONS_HOST)"
  [ -n "$_ph" ] || return 0
  HOST="$_ph"; HOST_EXPLICIT=1
  _po="$(_env_file_get AGENT_SESSIONS_ORIGIN)"
  if [ -n "$_po" ] && [ "$ORIGIN_EXPLICIT" = 0 ]; then ORIGIN="$_po"; ORIGIN_EXPLICIT=1; fi
}

adopt_persisted_channel() {
  # The app persists the release channel in the env file (Settings → System, #538). A
  # re-run without AGENT_SESSIONS_CHANNEL in the environment follows that choice, so a
  # manual `curl|sh` upgrade can't silently flip a main-channel install back to stable.
  # An env var passed on THIS run still wins (and is persisted after the env file exists).
  [ "$CHANNEL_EXPLICIT" = 1 ] && return 0
  [ -f "$ENVF" ] || return 0
  _pc="$(_env_file_get AGENT_SESSIONS_CHANNEL)"
  case "$_pc" in stable | main) CHANNEL="$_pc" ;; esac
}

choose_host() {
  # First interactive install only: let the operator pick the bind address. The default stays
  # 127.0.0.1 (the safe, reverse-proxy-fronted model). Skip entirely when the host was set
  # explicitly (env, or adopted from a prior install), when AGENT_SESSIONS_ASSUME_YES=1, or when
  # there's no tty to ask on (a piped `curl|sh`) — those keep today's localhost bind unchanged.
  [ "$HOST_EXPLICIT" = 1 ] && return 0
  [ "${AGENT_SESSIONS_ASSUME_YES:-0}" = 1 ] && return 0
  # A readable mode bit on /dev/tty is NOT enough: with no controlling terminal (a detached
  # `curl|sh`, a service, `setsid`) the node exists rw but open() fails with ENXIO, which would
  # then kill the script on the first `> /dev/tty`. Probe a real open and bail to the default.
  ( : < /dev/tty ) 2>/dev/null || return 0

  _ips="$(_host_ips)"
  {
    printf '\nWhere should %s listen for connections?\n' "$APP"
    printf '  1) 127.0.0.1   localhost only — default, recommended (put a reverse proxy / TLS in front)\n'
    printf '  2) 0.0.0.0     all interfaces — reachable from anywhere this host is\n'
  } > /dev/tty
  _i=2
  for _ip in $_ips; do
    _i=$((_i + 1))
    printf '  %d) %-13s this address only\n' "$_i" "$_ip" > /dev/tty
  done
  printf 'Choose an option [1]: ' > /dev/tty
  read _sel < /dev/tty 2>/dev/null || _sel=""
  [ -n "$_sel" ] || _sel=1

  _chosen=""
  case "$_sel" in
    1) return 0 ;;                       # localhost — the safe default, no change, no warning
    2) _chosen=0.0.0.0 ;;
    *[!0-9]*) log "unrecognized choice '$_sel' — keeping 127.0.0.1"; return 0 ;;
    *)
      _n=$((_sel - 2))                   # map 3,4,5… back to the Nth detected address
      # shellcheck disable=SC2086
      set -- $_ips
      if [ "$_n" -ge 1 ] && [ "$_n" -le "$#" ]; then
        shift "$((_n - 1))"; _chosen="$1"
      else
        log "unrecognized choice '$_sel' — keeping 127.0.0.1"; return 0
      fi
      ;;
  esac

  # Any non-localhost bind exposes a shell-equivalent surface — warn + require an explicit yes
  # (default No), mirroring the README trust model.
  {
    printf '\n  !  Binding to %s exposes %s on the network.\n' "$_chosen" "$APP"
    printf '     It launches AI agents with permission bypass — treat access as a shell on this host.\n'
    printf '     Only do this on a trusted network (LAN / VPN); put TLS + auth (a reverse proxy) in\n'
    printf '     front for anything wider, and consider enabling 2FA.\n'
    printf '  Bind to %s anyway? [y/N] ' "$_chosen"
  } > /dev/tty
  read _yn < /dev/tty 2>/dev/null || _yn=""
  case "$_yn" in
    [Yy]*) ;;
    *) log "keeping 127.0.0.1"; return 0 ;;
  esac

  HOST="$_chosen"
  if [ "$_chosen" = 0.0.0.0 ]; then
    # The browser never sends `Origin: http://0.0.0.0` — derive the origin from a real address so
    # the same-origin / CSRF checks pass. Prefer the default-route source (the operator's primary
    # reachable IP) over the first enumerated address, so a multi-homed host (docker bridges, a VPN)
    # doesn't hand back an unreachable internal address. Fall back to the first detected address.
    _addr="$(_primary_ip)"
    if [ -z "$_addr" ]; then
      # shellcheck disable=SC2086
      set -- $_ips
      [ "$#" -ge 1 ] && _addr="$1"
    fi
    if [ -n "$_addr" ]; then
      _recompute_origin "$_addr"
      note "Bound to all interfaces. Origin set to $ORIGIN (your primary address)."
      log  "If you reach it via another address/name, re-run with AGENT_SESSIONS_ORIGIN=http://<that-host>:$PORT."
    else
      note "Bound to all interfaces."
      log  "Set AGENT_SESSIONS_ORIGIN=http://<the-address-you-use>:$PORT and re-run if login fails the same-origin check."
    fi
  else
    _recompute_origin "$_chosen"
    note "Bound to $HOST. Origin set to $ORIGIN."
  fi

  # A LAN/all-interfaces bind only works if the host firewall lets the port through — offer to open
  # it (or print the manual command). Best-effort; never fails the install.
  _offer_firewall "$PORT"
}

_ensure_venv_module() {  # Debian/Ubuntu split venv into python3-venv; a vendored standalone python
                         # already ships it, so this is a no-op for the vendored interpreter.
  "$PY" -m venv --help >/dev/null 2>&1 && return
  log "python venv module missing — installing…"
  _pkg_install python3-venv >/dev/null 2>&1 || _pkg_install python3 >/dev/null 2>&1 || true
  "$PY" -m venv --help >/dev/null 2>&1 \
    || die "install the python3 venv module for your distro (e.g. python3-venv) and re-run"
}

ensure_python() {
  # Resolve $PY to a CPython >= 3.11. Order: an explicit override > a new-enough system python
  # (newest name first, so a python3.12 beside an old default python3 wins) > a distro install >
  # a vendored standalone CPython downloaded into $PREFIX/.toolchain (no sudo, no system change) —
  # the Python analogue of ensure_node's vendored Node. The vendor step ASKS first on a terminal
  # (the operator's machine, a ~30 MB download); AGENT_SESSIONS_ASSUME_YES=1 / no tty → proceed.
  _py_ok() { [ -n "$1" ] && "$1" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 11) else 1)' >/dev/null 2>&1; }

  if [ -n "${AGENT_SESSIONS_PYTHON:-}" ]; then
    _py_ok "$AGENT_SESSIONS_PYTHON" \
      || die "AGENT_SESSIONS_PYTHON=$AGENT_SESSIONS_PYTHON is not a python >= 3.11"
    PY="$AGENT_SESSIONS_PYTHON"; _ensure_venv_module; return
  fi
  for cand in python3.13 python3.12 python3.11 python3 python; do
    if have "$cand" && _py_ok "$(command -v "$cand")"; then
      PY="$(command -v "$cand")"; _ensure_venv_module; return
    fi
  done
  # No system Python >= 3.11. We deliberately DON'T try a distro `python3` install here: on the
  # stale distros that land here (e.g. Ubuntu whose python3 is 3.10) it can't supply >= 3.11 and
  # would only burn a pointless sudo prompt right before the download. Go straight to vendoring a
  # pinned standalone CPython (relocatable, no root) — ask first.
  _confirm "No system Python >= 3.11 found. Download a private one (~30 MB, no root) into $PREFIX/.toolchain? [Y/n]" \
    || die "Python >= 3.11 required. Install it (e.g. your distro's python3.12 + python3.12-venv), set AGENT_SESSIONS_PYTHON=/path/to/python3.12, or re-run and accept the download."
  os="$(uname -s)"; arch="$(uname -m)"
  case "$os" in
    Linux)  plat=unknown-linux-gnu ;;
    Darwin) plat=apple-darwin ;;
    *) die "no prebuilt Python for OS '$os' — install python3.11+ and re-run" ;;
  esac
  case "$arch" in
    x86_64|amd64) pa=x86_64 ;;
    aarch64|arm64) pa=aarch64 ;;
    *) die "no prebuilt Python for arch '$arch' — install python3.11+ and re-run" ;;
  esac
  asset="cpython-${PY_VERSION}+${PBS_TAG}-${pa}-${plat}-install_only.tar.gz"
  url="https://github.com/astral-sh/python-build-standalone/releases/download/${PBS_TAG}/${asset}"
  # Supply-chain pin: the expected SHA-256 per supported asset, from the release's SHA256SUMS.
  # A `curl | sh` install (esp. the no-tty auto-proceed) must NOT trust a mutable release URL on
  # TLS alone — we verify the tarball against this digest before unpacking and refuse on mismatch.
  # These pins are tied to PY_VERSION+PBS_TAG above; bump all four together when those change.
  case "${pa}-${plat}" in
    x86_64-unknown-linux-gnu)  want_sha=9be5c21b78dbc371e739bc7faf3b007b8e607335f780bdd2e0dd44a6e3580d76 ;;
    aarch64-unknown-linux-gnu) want_sha=f0c9ea0022b2dfdf0a4733e962ba8cc883c45d26df26116b9802b658240a25d7 ;;
    x86_64-apple-darwin)       want_sha=e6776f05a160f9d44f9c2bc8bd1e252037856808528bf910dea791bdf70a7224 ;;
    aarch64-apple-darwin)      want_sha=0c21806e8690e4b20a6c2e9dc662f46196c5ba719686e8dd60f00af6ff409a75 ;;
    *) die "no pinned checksum for ${pa}-${plat} at Python ${PY_VERSION} — install python3.11+ and re-run" ;;
  esac
  tdir="$PREFIX/.toolchain"; pdir="$tdir/cpython-${PY_VERSION}"
  if [ ! -x "$pdir/bin/python3" ]; then
    mkdir -p "$tdir"
    log "fetching a self-contained Python ${PY_VERSION} (${pa}/${plat})…"
    curl -fsSL "$url" -o "$tdir/python.tar.gz" || die "could not download standalone Python ${PY_VERSION}"
    got_sha="$(_sha256 "$tdir/python.tar.gz")"
    [ -n "$got_sha" ] || die "no sha256 tool (sha256sum/shasum) to verify the Python download — install one and re-run"
    [ "$got_sha" = "$want_sha" ] \
      || { rm -f "$tdir/python.tar.gz"; die "Python download checksum mismatch (expected $want_sha, got $got_sha) — refusing to use it"; }
    rm -rf "$tdir/python"
    tar -xzf "$tdir/python.tar.gz" -C "$tdir" || die "could not unpack standalone Python"
    rm -rf "$pdir"; mv "$tdir/python" "$pdir"   # the install_only tarball extracts to ./python
    rm -f "$tdir/python.tar.gz"
  fi
  PY="$pdir/bin/python3"
  _py_ok "$PY" || die "the vendored Python looks broken — set AGENT_SESSIONS_PYTHON to a python >= 3.11"
}

ensure_prereqs() {
  have curl || die "curl not found — install curl and re-run"
  have git || { log "git missing — installing…"; _pkg_install git || die "install git and re-run"; }
  ensure_python
  # The ws terminal attaches agents under a persistent dtach master.
  have dtach || { log "dtach missing (terminal pane) — installing…"; _pkg_install dtach >/dev/null 2>&1 \
    || log "could not auto-install dtach — install it so the terminal pane works"; }
  # The React UI is built from source (Vite) at install time; skip resolving Node when
  # the build is explicitly skipped (CI / bring-your-own-dist).
  [ "${AGENT_SESSIONS_SKIP_WEB_BUILD:-0}" = 1 ] || ensure_node
  preflight_report
}

preflight_report() {
  log "prerequisites:"
  log "  git      $(command -v git || echo MISSING)"
  log "  python   ${PY:-MISSING} ($("${PY:-python3}" -V 2>&1 | awk '{print $2}'))"
  log "  node     $(command -v node || echo '(vendored)') ($(node -v 2>/dev/null || echo "v$NODE_VERSION vendored"))"
  log "  dtach    $(command -v dtach || echo 'MISSING — terminal pane degraded')"
  log "  channel  $CHANNEL"
  # #612: `main` is already opt-in (CHANNEL defaults to stable, and only an explicit
  # AGENT_SESSIONS_CHANNEL=main selects it) — what was missing is that the choice was silent.
  # An install that tracks a moving branch takes whatever HEAD says at each auto-update, with
  # no release review between the commit and the running service, so it should say so out loud
  # rather than leave the operator to infer it from a one-word line above.
  if [ "$CHANNEL" = main ]; then
    note "NOTE: channel 'main' tracks the development branch, not tagged releases."
    log "Auto-updates will follow main HEAD — unreviewed by a release cut. Not for production."
    log "Use AGENT_SESSIONS_CHANNEL=stable (the default) for a release-tracking install."
  fi
}

resolve_ref() {
  if [ -n "$REF" ]; then printf '%s' "$REF"; return; fi
  if [ "$CHANNEL" = main ]; then printf 'main'; return; fi
  # stable = the highest semver-ish vX.Y.Z tag on the remote (empty → default branch).
  git ls-remote --tags --refs "$REPO_URL" 'v*' 2>/dev/null | sed 's#.*/##' | sort -V | tail -1
}

build_release() {
  # Clone at the ref, then build the venv + pip-install AT the final release path and
  # set $rel (global). The venv is built in place — venv console-script shebangs are
  # absolute, so a venv must never be moved after creation. Only the plain source tree
  # is relocated. `current` is flipped to $rel by the caller after a full build.
  ref="$1"
  tmp="$(mktemp -d "$PREFIX/.clone.XXXXXX")"
  if [ -n "$ref" ]; then
    git clone -q --depth 1 --branch "$ref" "$REPO_URL" "$tmp/src" 2>/dev/null \
      || { git clone -q "$REPO_URL" "$tmp/src"; git -C "$tmp/src" checkout -q "$ref"; }
  else
    git clone -q --depth 1 "$REPO_URL" "$tmp/src"
  fi
  # Bind what was cloned to what was verified, BEFORE anything is built from it.
  #
  # $REF is a tag: a mutable pointer resolved once by the verifier and again, independently,
  # by the clone above. A tag moved between those two lookups passes verification and then
  # delivers different bytes — which is the entire attack the manifest exists to stop, walking
  # in through the gap between the check and the build. Comparing the cloned commit to the
  # verified one makes the two lookups one decision.
  #
  # Empty $EXPECT_COMMIT means nobody claimed a commit — nothing to contradict, so nothing to
  # refuse. That is now a HAND-RUN install only: `update.select_stable_target()` picks the tag
  # and its commit together and refuses the spawn outright when either is missing, so a
  # self-update can no longer reach this line without a pin. (It used to be able to, which is
  # what this comment described.) A human running install.sh directly is choosing their own
  # ref and is not the threat model this comparison addresses.
  #
  # So the comparison fails closed only on a genuine disagreement between two lookups.
  full_sha="$(git -C "$tmp/src" rev-parse HEAD 2>/dev/null || true)"
  if [ -n "$EXPECT_COMMIT" ] && [ "$full_sha" != "$EXPECT_COMMIT" ]; then
    rm -rf "$tmp"
    die "refusing to build $ref: it was verified as commit $EXPECT_COMMIT but the clone resolved it to ${full_sha:-<unknown>}. A released tag that changed between verification and checkout is exactly what must not be installed. Nothing was built."
  fi
  sha="$(git -C "$tmp/src" rev-parse --short HEAD)"
  mkdir -p "$RELEASES"
  rel="$RELEASES/$(date +%Y%m%d-%H%M%S)-$sha"
  rm -rf "$rel"
  mkdir -p "$rel"
  mv "$tmp/src" "$rel/src"   # source is plain files — safe to relocate
  rm -rf "$tmp"
  "$PY" -m venv "$rel/venv"   # built at its final path (with the resolved python) → valid shebangs
  "$rel/venv/bin/pip" install --quiet --upgrade pip
  "$rel/venv/bin/pip" install --quiet "$rel/src"
  build_web "$rel"
}

build_web() {
  # Build the React SPA (Vite) into <rel>/src/web/dist. The app serves it when the env
  # points AGENT_SESSIONS_WEB_DIST here (set by write_env_if_absent, stable via `current`).
  # web/dist is git-ignored, so every release builds its own — no stale artifact.
  rel="$1"
  if [ "${AGENT_SESSIONS_SKIP_WEB_BUILD:-0}" = 1 ]; then
    log "skipping UI build (AGENT_SESSIONS_SKIP_WEB_BUILD=1)"; return 0
  fi
  [ -f "$rel/src/web/package.json" ] || { log "no web/ in this release — skipping UI build"; return 0; }
  log "building the React UI (this can take a minute)…"
  # Stamp the release version into the bundle (#661): build_release() pip-installed the package
  # before calling us, so the venv's CLI reports the exact version being installed. Vite bakes
  # it in as __APP_VERSION__ — the footer shows it, and the changed dist content busts the PWA
  # precache on EVERY release (even server-only ones). Empty/missing ⇒ vite falls back to "dev".
  app_version="$("$rel/venv/bin/agent-sessions" version 2>/dev/null || echo '')"
  ( cd "$rel/src/web" \
      && AGENT_SESSIONS_VERSION="$app_version" "$NPM" ci --no-audit --no-fund --silent \
      && AGENT_SESSIONS_VERSION="$app_version" "$NPM" run build --silent ) \
    || die "UI build failed — see the npm output above"
  [ -f "$rel/src/web/dist/index.html" ] || die "UI build produced no dist/index.html"
}

write_env_if_absent() {
  # First install only: generate a signing secret + admin credentials. The plaintext
  # password is returned on stdout (printed to the console ONCE by the caller); only the
  # hash is persisted. On upgrade the existing env is left untouched.
  rel="$1"
  [ -f "$ENVF" ] && { printf ''; return; }
  py="$rel/venv/bin/python"
  secret="$("$py" -c 'import secrets; print(secrets.token_urlsafe(48))')"
  password="$("$py" -c 'import secrets; print(secrets.token_urlsafe(18))')"
  hash="$("$py" -c 'import sys; from agent_sessions.auth import hash_password; print(hash_password(sys.argv[1]))' "$password")"
  umask 077
  cat > "$ENVF" <<EOF
AGENT_SESSIONS_USERNAME=admin
AGENT_SESSIONS_PASSWORD_HASH=$hash
AGENT_SESSIONS_SECRET_KEY=$secret
AGENT_SESSIONS_ORIGIN=$ORIGIN
AGENT_SESSIONS_HOST=$HOST
AGENT_SESSIONS_PORT=$PORT
AGENT_SESSIONS_ENV_FILE=$ENVF
AGENT_SESSIONS_FORCE_PASSWORD_CHANGE=1
# Cap glibc's per-thread malloc arenas (#630). The broker parks one blocking-read thread
# per live session, and glibc binds each to its own arena (up to 8*nproc), each of which
# grows to a high-water mark and never returns it to the OS — 7.7 GB RSS against a 145 MB
# heap was observed. Two arenas is plenty (the threads block in os.read, they don't
# allocate). Read by glibc at process start, so it must be in the environment before exec;
# systemd's EnvironmentFile sets it before ExecStart. Delete this line to revert.
MALLOC_ARENA_MAX=2
EOF
  chmod 600 "$ENVF"
  printf '%s' "$password"
}

_env_has() { grep -q "^$1=" "$ENVF" 2>/dev/null; }
_env_set_if_absent() {
  # Append KEY=VAL only if KEY is absent — preserves operator overrides + existing
  # secrets/credentials (we never rewrite the lines already in the file). 0600 is kept
  # because we only append to an already-0600 file.
  _env_has "$1" || printf '%s=%s\n' "$1" "$2" >> "$ENVF"
}
_env_set() {
  # Set KEY=VAL, replacing an existing line. Only for fixed installer-owned keys with
  # token values (never secrets / user input). Rewrites via a 0600 temp + rename so the
  # file never has looser permissions; other lines are preserved (order not guaranteed).
  [ "$(_env_file_get "$1")" = "$2" ] && _env_has "$1" && return 0
  if _env_has "$1"; then
    _tmp="$ENVF.set.$$"
    grep -v "^$1=" "$ENVF" > "$_tmp" || true
    printf '%s=%s\n' "$1" "$2" >> "$_tmp"
    chmod 600 "$_tmp"
    mv "$_tmp" "$ENVF"
  else
    printf '%s=%s\n' "$1" "$2" >> "$ENVF"
  fi
}

migrate_env() {
  # Bring an env file (fresh OR pre-existing from an older install) up to the current
  # serving contract: the shipped product is the React UI + ws-PTY terminal, the only
  # UI/terminal there is (the app no longer reads a UI/terminal selector env var).
  # Idempotent and non-destructive — existing keys win.
  [ -f "$ENVF" ] || return 0
  mkdir -p "$PREFIX/pty"   # ws-PTY dtach sockets live here
  umask 077
  _env_set_if_absent AGENT_SESSIONS_WEB_DIST "$CURRENT/src/web/dist"
  _env_set_if_absent AGENT_SESSIONS_RUNTIME_DIR "$PREFIX/pty"
  # Cap glibc malloc arenas on EXISTING installs too (#630) — appended only if absent, so an
  # operator override is preserved. Takes effect on the next service (re)start.
  _env_set_if_absent MALLOC_ARENA_MAX 2
  # Persist an explicitly-passed channel (#538) so the app (which reads the env file
  # live) and later re-runs (adopt_persisted_channel) follow it. UI changes rewrite the
  # same key; a defaulted run leaves whatever the operator/UI chose untouched.
  if [ "$CHANNEL_EXPLICIT" = 1 ]; then
    _env_set AGENT_SESSIONS_CHANNEL "$CHANNEL"
  fi
}

render_unit() {
  mkdir -p "$UNIT_DIR"
  cat > "$UNIT" <<EOF
[Unit]
Description=agent-sessions
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
# #165: only SIGTERM the broker's main PID on stop; leave dtach + the agent processes
# alone so a service restart (every deploy) does NOT kill the user's live session. The
# new broker rediscovers the still-alive masters via the existing sock files.
KillMode=process
# #346 Phase A: session children (dtach masters + agents + their builds) currently share
# this cgroup, and the systemd default OOMPolicy=stop fails the WHOLE unit when the kernel
# OOM-kills ANY of them — restarting the broker and dropping every websocket (observed
# in production 2026-06-08, twice in 10 min). \`continue\` confines the damage to the killed
# process; the broker's own MainPID dying still fails the unit via Restart=on-failure.
OOMPolicy=continue
# Same shared-cgroup problem for the task budget: the user-slice default (~2175) is easily
# exhausted by session workloads (test runners), and at the ceiling fork fails → PTY spawns
# die with EAGAIN. Generous explicit ceiling until #346 Phase B isolates sessions in scopes.
TasksMax=8192
# Memory guardrail (#630): a soft ceiling so runaway growth surfaces as reclaim pressure
# instead of silent creep to OOM. A percentage (not a fixed GiB) so it scales across hosts
# — 80% is well above legitimate multi-session use (the arena leak this backstops was fixed
# by MALLOC_ARENA_MAX=2 in the env file). Soft: throttles/reclaims, never kills (that stays
# OOMPolicy=continue). The whole unit shares one cgroup (app + session children), so keep it
# generous — tune down only if this host should cap sessions harder.
MemoryHigh=80%
# Put ~/.local/bin first so sessions spawned by the app (claude/opencode/codex/gemini/agy/kimi,
# which commonly live there) are on PATH — otherwise the claude CLI nags
# "Native installation exists but ~/.local/bin is not in your PATH". Before EnvironmentFile
# so an explicit PATH in the env file still wins. %h = the service user's home dir.
Environment=PATH=%h/.local/bin:/usr/local/bin:/usr/bin:/bin
EnvironmentFile=$ENVF
ExecStart=$CURRENT/venv/bin/agent-sessions serve --host $HOST --port $PORT
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
EOF
}

# --- Home Free stream channel (#27) -----------------------------------------------
# A machine-generated console name + access key let the user reach this box from any
# browser through a blind relay. The key is NEVER user-chosen and NEVER leaves the box
# except as the E2E pre-shared key (the relay only sees ciphertext). Off unless opted in.
homefree_gen_name() {  # random callsign like "viper-8231" (matches the relay name rule)
  set -- viper falcon cobra raven hydra onyx delta sierra tango zulu nomad specter atlas orbit lynx
  _i=$(( $(od -An -N2 -tu2 /dev/urandom | tr -d ' ') % $# ))
  eval "_w=\${$((_i + 1))}"
  _n=$(( $(od -An -N2 -tu2 /dev/urandom | tr -d ' ') % 9000 + 1000 ))
  printf '%s-%s\n' "$_w" "$_n"
}

homefree_gen_key() {  # >=128-bit, base32, lowercase, no padding — machine-generated only
  if have base32; then
    head -c 20 /dev/urandom | base32 | tr -d '=' | tr 'A-Z' 'a-z' | cut -c1-32
  else
    openssl rand -hex 20  # 160-bit hex fallback
  fi
}

render_homefree_unit() {
  mkdir -p "$UNIT_DIR"
  # Full-app streaming (#579): the agent reverse-proxies the loopback app at HOMEFREE_APP_PORT.
  # There is no recovery-shell fallback, so stream mode refuses non-loopback binds before this.
  cat > "$HOMEFREE_UNIT" <<EOF
[Unit]
Description=agent-sessions Home Free relay agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=AGENT_SESSIONS_RELAY_URL=$HOMEFREE_RELAY_URL
Environment=HOMEFREE_CONSOLE_NAME_FILE=$HOMEFREE_DIR/console_name
Environment=HOMEFREE_ACCESS_KEY_FILE=$HOMEFREE_DIR/access_key
Environment=HOMEFREE_IDENTITY_PATH=$HOMEFREE_DIR/identity
Environment=HOMEFREE_APP_PORT=$PORT
ExecStart=$CURRENT/venv/bin/python -m agent_sessions.homefree
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
}

# Option A (#595): set the box app to AUTH_MODE=none so app-mode streams without an in-app
# login prompt — the access key + loopback binding are the single gate. Idempotent; only
# called on a loopback bind. Drops the force-password-change flag (inert under AUTH_MODE=none).
homefree_enable_app_auth() {
  [ -f "$ENVF" ] || return 0
  _tmp="$ENVF.hf.$$"
  grep -vE '^(AGENT_SESSIONS_AUTH_MODE|AGENT_SESSIONS_FORCE_PASSWORD_CHANGE)=' "$ENVF" > "$_tmp" 2>/dev/null || true
  printf 'AGENT_SESSIONS_AUTH_MODE=none\n' >> "$_tmp"
  mv "$_tmp" "$ENVF"
  chmod 600 "$ENVF" 2>/dev/null || true
  if [ "${AGENT_SESSIONS_NO_SERVICE:-0}" != 1 ] && systemctl --user >/dev/null 2>&1; then
    systemctl --user restart "$APP.service" 2>/dev/null || true  # pick up AUTH_MODE=none
  fi
}

homefree_print_credentials() {
  _name="$1"; _key="$2"
  if [ -t 1 ]; then
    _ESC="$(printf '\033')"
    _R="${_ESC}[1;31m"; _B="${_ESC}[1m"; _Z="${_ESC}[0m"
  else
    _R=''; _B=''; _Z=''
  fi
  note "BattleLab remote (stream) is enabled — reach this box from any browser."
  log "Console name: ${_name}"
  log "Access key:   ${_key}"
  printf '  %sConnect at:   %s%s\n' "$_B" "$HOMEFREE_CONNECT_URL" "$_Z"
  printf '  %s(enter the console name + access key above; nothing else to set up.)%s\n' "$_B" "$_Z"
  printf '\n'
  printf '  %sFULL-APP mode: the browser streams your entire BattleLab UI.%s\n' "$_B" "$_Z"
  printf '  %s* Your box app now uses the ACCESS KEY as its ONLY gate (no password),%s\n' "$_R" "$_Z"
  printf '  %s  bound to loopback. The access key alone grants full control.%s\n' "$_R" "$_Z"
  printf '\n'
  printf '  %s* SECURITY: the access key grants FULL CONTROL of this machine.%s\n' "$_R" "$_Z"
  printf '  %sNever enter it for anyone who contacted you. BattleLab staff will%s\n' "$_R" "$_Z"
  printf '  %sNEVER ask for your access key or console name.%s\n' "$_R" "$_Z"
}

homefree_setup() {
  # Enabling/re-enabling streaming writes the access key and starts the unit, so it mutates
  # exactly what rotate and disable mutate and belongs inside the same fence. Without this a
  # reinstall could recreate the key and restart streaming while a disable was reporting it
  # off, or invalidate the snapshot a rotation had already acted on.
  #
  # Scoped deliberately to the Home Free setup step rather than the whole installer: the lock
  # guards Home Free key/service state, and holding it across an entire install (npm, pip, a
  # web build) would turn an unrelated slow step into a lockout of the security commands.
  homefree_lock_acquire "enabling Home Free"
  _homefree_setup_locked
  _rc=$?
  homefree_lock_release
  return $_rc
}

_homefree_setup_locked() {
  # App-only stream mode requires the app to be private to the box. The agent proxies to and
  # signs Origin for exact 127.0.0.1:$PORT; aliases such as localhost/::1 are rejected rather
  # than silently enabling a terminal fallback.
  [ "$HOST" = "127.0.0.1" ] || die "BattleLab stream mode requires AGENT_SESSIONS_HOST=127.0.0.1; re-run with a loopback bind or use self-host mode."
  mkdir -p "$HOMEFREE_DIR"; chmod 700 "$HOMEFREE_DIR" 2>/dev/null || true
  [ -f "$HOMEFREE_DIR/console_name" ] || homefree_gen_name > "$HOMEFREE_DIR/console_name"
  [ -f "$HOMEFREE_DIR/access_key" ] || homefree_gen_key > "$HOMEFREE_DIR/access_key"
  chmod 600 "$HOMEFREE_DIR/console_name" "$HOMEFREE_DIR/access_key" 2>/dev/null || true
  _name="$(cat "$HOMEFREE_DIR/console_name")"
  _key="$(cat "$HOMEFREE_DIR/access_key")"
  homefree_enable_app_auth
  render_homefree_unit
  if [ "${AGENT_SESSIONS_NO_SERVICE:-0}" != 1 ] && systemctl --user >/dev/null 2>&1; then
    systemctl --user daemon-reload
    systemctl --user enable "$APP-homefree.service" >/dev/null 2>&1 || true
    systemctl --user restart "$APP-homefree.service" || true
  else
    log "start the agent with:  $CURRENT/venv/bin/python -m agent_sessions.homefree"
  fi
  homefree_print_credentials "$_name" "$_key"
}

# --- Home Free credential lifecycle (#612) ---------------------------------------------
# Operator entry points for the access key AFTER setup: rotate it, switch streaming off, or
# read it back. None of them runs an install; all three are idempotent, so a re-run (or a
# config-management tool applying the same state twice) is a no-op rather than a surprise.
#
# The ordering rule that matters throughout is lockout-safety. The access key is the ONLY
# gate on a streamed box — `homefree_enable_app_auth` sets AGENT_SESSIONS_AUTH_MODE=none —
# so a half-finished rotation that has invalidated the old key without a working new one
# leaves a machine nobody can reach. Every step below therefore writes and validates the
# replacement first and swaps last.

homefree_key_valid() {  # a generated key: >= 32 chars of lowercase base32 / hex, nothing else
  _k="$1"
  [ -n "$_k" ] || return 1
  [ "${#_k}" -ge 32 ] || return 1
  case "$_k" in *[!a-z0-9]*) return 1 ;; esac
  return 0
}

homefree_lock_acquire() {
  # Serialize the WHOLE lifecycle transaction — preflight, key-file transitions, service
  # action, banner — not the individual steps. Every step is careful on its own, and that is
  # not enough: they are only correct as a unit. A disable and a rotate running together
  # interleave into states neither can produce alone, and two rotations cost the roll-back.
  #
  # mkdir(2) is the lock because it is atomic on every POSIX filesystem and needs no
  # util-linux on a minimal container. $1 names the operation for the message.
  [ -d "$HOMEFREE_DIR" ] || mkdir -p "$HOMEFREE_DIR" 2>/dev/null || true
  if mkdir "$HOMEFREE_LOCKDIR" 2>/dev/null; then
    homefree_lock_publish "$1"
    return 0
  fi

  # The directory exists. Who owns it?
  _owner="$(cat "$HOMEFREE_LOCKDIR/pid" 2>/dev/null || true)"
  case "$_owner" in
    '' | *[!0-9]*)
      # No readable owner. This is EITHER a live holder that has created the directory but
      # not yet published its PID, OR a crash — and from here those are indistinguishable.
      # Refusing is the only safe reading: treating "no owner yet" as "stale" is precisely
      # how a second command walks through a live fence, which is what a review probe did.
      # Fail closed and let the operator resolve it; a retry a second later usually does.
      die "$1 refused: the Home Free lifecycle lock at $HOMEFREE_LOCKDIR exists but names no owner. Another command may be starting up, or a previous one died before recording itself — those look identical from here, and guessing wrong would run two key operations at once. Re-run in a moment; if it persists, remove that directory by hand once you are sure nothing is running. Nothing has been changed."
      ;;
  esac
  if kill -0 "$_owner" 2>/dev/null; then
    die "$1 refused: another Home Free lifecycle command (PID $_owner) is already running. These commands change the same key and the same service, so they run one at a time. Wait for it to finish and re-run. Nothing has been changed."
  fi

  # A provably dead owner: take over. Re-created rather than reused, so ownership is
  # republished under this PID. If two takeovers race, exactly one mkdir wins and the other
  # refuses — losing the race is not a licence to proceed.
  rm -rf "$HOMEFREE_LOCKDIR" 2>/dev/null || true
  mkdir "$HOMEFREE_LOCKDIR" 2>/dev/null \
    || die "$1 refused: could not take over the Home Free lifecycle lock at $HOMEFREE_LOCKDIR (another command took it first). Nothing has been changed."
  homefree_lock_publish "$1"
}

homefree_lock_publish() {
  # Record ownership, and treat a failure to record it as a failure to lock. A lock nobody
  # can attribute is worse than no lock at all: the next arrival cannot tell it from a crash.
  echo $$ > "$HOMEFREE_LOCKDIR/pid" 2>/dev/null || {
    rm -rf "$HOMEFREE_LOCKDIR" 2>/dev/null || true
    die "$1 refused: could not record ownership of the Home Free lifecycle lock at $HOMEFREE_LOCKDIR. Nothing has been changed."
  }
  HOMEFREE_LOCK_HELD=1
  # `die` exits without unwinding, so the trap is what stops a refusal from stranding the
  # lock. INT/TERM must also TERMINATE: releasing the fence and then carrying on would leave
  # the rest of the operation running outside it, which is the one thing the lock forbids.
  trap 'homefree_lock_release' EXIT
  trap 'homefree_lock_release; exit 130' INT
  trap 'homefree_lock_release; exit 143' TERM
}

homefree_lock_release() {
  [ -n "$HOMEFREE_LOCK_HELD" ] || return 0
  HOMEFREE_LOCK_HELD=""
  # Remove only a lock this process still owns. Blindly clearing whatever occupies the path
  # would drop a fence somebody else legitimately took over after we were declared dead.
  _held="$(cat "$HOMEFREE_LOCKDIR/pid" 2>/dev/null || true)"
  [ "$_held" = "$$" ] || return 0
  rm -rf "$HOMEFREE_LOCKDIR" 2>/dev/null || true
}

homefree_select_mode() {
  # Decide ONCE per run whether this box manages the agent through systemd, then cache it.
  #
  # This has to be a single decision, not a question asked repeatedly. Probing per call means
  # the answer can change mid-operation — a user manager that goes away between the preflight
  # and the action (logout, session teardown, a DBus restart) flips a run that was committed
  # to "systemd will stop this for me" into "there is nothing to stop, report success". That
  # is the same false assurance the preflight exists to prevent, arriving through the back
  # door. Caught in review on this PR.
  [ -z "$HOMEFREE_MODE" ] || return 0
  if [ "${AGENT_SESSIONS_NO_SERVICE:-0}" != 1 ] && systemctl --user >/dev/null 2>&1; then
    HOMEFREE_MODE=systemd
  else
    HOMEFREE_MODE=none
  fi
}

homefree_service_do() {
  # Run a systemctl --user verb on the homefree unit, per the mode chosen for this run.
  # Returns non-zero when the run is systemd-managed and the action did not take.
  #
  #   * mode "none"    — there is no unit to act on, and the preflight has already proved no
  #     agent is running, so doing nothing IS the correct outcome. An install that never had
  #     a service must not start failing because a unit it never created cannot be stopped.
  #   * mode "systemd" — the run is committed. Every failure from here is a failure, the
  #     manager having vanished included. Rotate and disable are security operations whose
  #     effect depends entirely on the agent restarting or stopping; the agent holds the key
  #     in memory, so a swallowed failure means the command prints "disabled" or "rotated"
  #     while the old key is still serving traffic. Re-probing availability here and calling
  #     a failed probe a no-op is exactly the hole this shape closes.
  homefree_select_mode
  [ "$HOMEFREE_MODE" = systemd ] || return 0
  systemctl --user "$1" "$APP-homefree.service" >/dev/null 2>&1 || return 1
  return 0
}

homefree_agent_state() {
  # Is a Home Free relay agent running as this user?  Echoes: running | stopped | unknown
  #
  # Only ever consulted on the NO-SYSTEMD path. Where a user systemd exists the unit is the
  # control surface and `homefree_service_do` already fails loudly when a verb does not take.
  # Where it does not, there is no unit to ask — and the agent may well have been started by
  # hand, because the installer itself prints that command when it cannot create a service
  # ("start the agent with: .../python -m agent_sessions.homefree").
  #
  # The pattern is ANCHORED ON THE INTERPRETER, not on a bare mention of the module name. A
  # loose substring match treats any process whose command line merely contains the string as
  # a live agent — an operator grepping the installer, an editor with this file open, a
  # config-management run. Those are false "running" answers, and while failing closed on one
  # is safe, a check that cries wolf on `grep` is a check people learn to override by reflex.
  # Requiring "<path ending in python> ... -m agent_sessions.homefree" matches how the agent
  # is actually launched, by both the unit and the manual command printed above.
  #
  # `unknown` is a real third answer, not a failure to try. A box where neither probe works
  # cannot be inspected, and reporting `stopped` there would be the exact false assurance this
  # check exists to prevent.
  if [ "${AGENT_SESSIONS_HOMEFREE_AGENT_STOPPED:-0}" = 1 ]; then
    echo stopped; return 0   # operator asserts it by hand; see the `unknown` message below
  fi
  _uid="$(id -u 2>/dev/null || true)"
  if [ -n "$_uid" ] && command -v pgrep >/dev/null 2>&1; then
    # pgrep's own contract carries the distinction: 0 = matched, 1 = matched nothing,
    # >= 2 = pgrep itself failed. Only the first two are answers; anything else falls
    # through to ps, because "the tool broke" is not evidence that nothing is running.
    _rc=0
    pgrep -u "$_uid" -f "$HOMEFREE_AGENT_RE" >/dev/null 2>&1 || _rc=$?
    case "$_rc" in
      0) echo running; return 0 ;;
      1) echo stopped; return 0 ;;
    esac
  fi
  if [ -n "$_uid" ] && command -v ps >/dev/null 2>&1; then
    if _args="$(ps -u "$_uid" -o args= 2>/dev/null)"; then
      if printf '%s\n' "$_args" | grep -Eq "$HOMEFREE_AGENT_RE"; then
        echo running
      else
        echo stopped
      fi
      return 0
    fi
  fi
  echo unknown
}

homefree_require_manageable_agent() {
  # Preflight for the two security operations, run BEFORE any key state is mutated.
  # $1 = what the caller was about to do, named in the refusal.
  #
  # With user systemd present there is nothing to check: the unit is the control surface, and
  # `homefree_service_do` fails closed on a verb that does not take.
  #
  # Without it there is no unit — and a running agent has ALREADY read the access key into
  # memory. Replacing or quarantining the file underneath it changes nothing about what it is
  # currently serving, so a command that went ahead and printed success would be telling the
  # operator that a live credential is dead. That is strictly worse than an error. Refuse
  # while nothing has been touched, and say exactly what to do instead.
  homefree_select_mode
  [ "$HOMEFREE_MODE" != systemd ] || return 0
  case "$(homefree_agent_state)" in
    stopped) return 0 ;;
    running)
      die "$1 refused: this box has no user systemd to manage $APP-homefree.service, and a Home Free agent is still RUNNING. It read the access key at startup and holds it in memory, so changing the key file underneath it would revoke nothing while reporting success. Stop it first with: pkill -u $(id -u 2>/dev/null) -f '$HOMEFREE_AGENT_RE'  --- that is the same anchored pattern this check uses, so it cannot match a bystander that merely mentions the module. Then re-run. Nothing has been changed."
      ;;
  esac
  die "$1 refused: this box has no user systemd to manage $APP-homefree.service, and neither pgrep nor ps could report whether a Home Free agent is still running. A running agent holds the access key in memory, so this command cannot confirm the change would take effect, and revocation that cannot be confirmed must not be reported as revocation. Stop any running agent, then re-run with AGENT_SESSIONS_HOMEFREE_AGENT_STOPPED=1 to confirm none is running. Nothing has been changed."
}

homefree_rotate_key() {
  # The lock is taken BEFORE any state is inspected: a preflight that read the world outside
  # it would already be acting on a snapshot another command could invalidate.
  homefree_lock_acquire "rotating the access key"
  _homefree_rotate_key_locked
  _rc=$?
  homefree_lock_release
  return $_rc
}

_homefree_rotate_key_locked() {
  [ -f "$HOMEFREE_KEY_FILE" ] || {
    if [ -f "$HOMEFREE_DISABLED_KEY" ]; then
      die "Home Free is disabled — nothing to rotate. Re-enable with AGENT_SESSIONS_REMOTE=stream (which issues a fresh key)."
    fi
    die "Home Free is not set up on this box — nothing to rotate. Enable it with AGENT_SESSIONS_REMOTE=stream."
  }
  # The console name is the box's identity to the relay and to whoever already has it
  # written down; a key rotation is not a rename, so it is read and preserved, never regenerated.
  _name="$(cat "$HOMEFREE_NAME_FILE" 2>/dev/null || true)"
  [ -n "$_name" ] || die "Home Free console name is missing or empty at $HOMEFREE_NAME_FILE — refusing to rotate against a broken install."

  # Nothing has been generated or moved yet — this is the last point at which a refusal
  # costs nothing, so the "can this change actually take effect?" question is asked here.
  homefree_require_manageable_agent "rotating the access key"

  # Generate → write 0600 → VALIDATE → only then swap. `umask 077` covers the window between
  # creation and chmod; the chmod then makes the mode explicit rather than umask-dependent.
  _new="$HOMEFREE_KEY_FILE.new.$$"
  ( umask 077; homefree_gen_key > "$_new" ) || { rm -f "$_new"; die "could not generate a replacement access key"; }
  chmod 600 "$_new" 2>/dev/null || true
  _newkey="$(cat "$_new" 2>/dev/null || true)"
  homefree_key_valid "$_newkey" || { rm -f "$_new"; die "the generated access key failed validation — the existing key is untouched and still works"; }

  # Keep the superseded key. A rotation the operator regrets (credentials pasted into a
  # device that then went offline) is otherwise unrecoverable, and this file is 0600 inside
  # an already-0700 directory. It is the previous key, not a second live one: the agent
  # only ever reads $HOMEFREE_KEY_FILE.
  # Written through a 0600 temp file, VERIFIED to hold the live key, and only then installed
  # by rename(2). A best-effort `cp` that was allowed to fail meant rotation could replace the
  # live key and print "previous key kept at ..." when no such file existed — the operator is
  # then told a roll-back exists for a credential that is already gone, which is worse than
  # having no roll-back at all. The rotation aborts here rather than make a promise it cannot
  # keep, and it aborts while the live key is still untouched. Caught in review on this PR.
  _prevtmp="$HOMEFREE_PREV_KEY.tmp"   # fixed name: the lifecycle lock makes this single-writer
  ( umask 077; cat "$HOMEFREE_KEY_FILE" > "$_prevtmp" ) \
    || { rm -rf "$_prevtmp" 2>/dev/null || true; rm -f "$_new" 2>/dev/null || true; die "could not write the roll-back copy of the current access key to $_prevtmp — the live key is untouched and still works. Nothing was changed."; }
  chmod 600 "$_prevtmp" 2>/dev/null || true
  if [ "$(cat "$_prevtmp" 2>/dev/null)" != "$(cat "$HOMEFREE_KEY_FILE" 2>/dev/null)" ]; then
    rm -rf "$_prevtmp" 2>/dev/null || true; rm -f "$_new" 2>/dev/null || true
    die "the roll-back copy of the current access key did not match the live key — refusing to rotate without a working way back. The live key is untouched and still works."
  fi
  # POSIX `mv -f file DIR` SUCCEEDS by moving the file *inside* DIR. If $HOMEFREE_PREV_KEY is
  # a directory (or a symlink to one), the rotation would then swap the live key and announce
  # a roll-back at a path that is not the file it names. Reject a destination that is not a
  # plain regular file before the move, and verify what actually landed after it — the promise
  # printed at the end is only worth what this check proves. Caught in review on this PR.
  if [ -L "$HOMEFREE_PREV_KEY" ] || { [ -e "$HOMEFREE_PREV_KEY" ] && [ ! -f "$HOMEFREE_PREV_KEY" ]; }; then
    rm -rf "$_prevtmp" 2>/dev/null || true; rm -f "$_new" 2>/dev/null || true
    die "$HOMEFREE_PREV_KEY exists but is not a regular file — refusing to rotate, because the roll-back copy could not be stored where the success message says it is. Remove or move that path and re-run. The live key is untouched and still works."
  fi
  mv -f "$_prevtmp" "$HOMEFREE_PREV_KEY" \
    || { rm -rf "$_prevtmp" 2>/dev/null || true; rm -f "$_new" 2>/dev/null || true; die "could not install the roll-back copy at $HOMEFREE_PREV_KEY — the live key is untouched and still works. Nothing was changed."; }
  if [ -L "$HOMEFREE_PREV_KEY" ] || [ ! -f "$HOMEFREE_PREV_KEY" ] \
    || [ "$(cat "$HOMEFREE_PREV_KEY" 2>/dev/null)" != "$(cat "$HOMEFREE_KEY_FILE" 2>/dev/null)" ]; then
    rm -f "$_new" 2>/dev/null || true
    die "the roll-back copy at $HOMEFREE_PREV_KEY is not a readable regular file holding the current access key — refusing to rotate without a working way back. The live key is untouched and still works."
  fi
  chmod 600 "$HOMEFREE_PREV_KEY" 2>/dev/null || true
  # rename(2) within one directory — the live key is either wholly the old one or wholly the
  # new one, never absent. This single call is the moment the old key stops being live.
  mv -f "$_new" "$HOMEFREE_KEY_FILE" || { rm -f "$_new"; die "could not install the replacement access key — the existing key is untouched"; }
  chmod 600 "$HOMEFREE_KEY_FILE" 2>/dev/null || true

  # Restart only AFTER the new material is in place and validated, so the agent never comes
  # back up against a key that is missing or half-written.
  # A failed restart here is NOT cosmetic: the new key is on disk but the running agent is
  # still authenticating with the old one, so the operator would be told to use a key that
  # does not work — locked out of their own box. Die instead of printing the success banner,
  # and say exactly what state things are in.
  homefree_service_do restart || die "the new access key was written, but restarting $APP-homefree.service FAILED — the RUNNING agent is still using the OLD key, so the new one will not work until the service restarts. Check 'systemctl --user status $APP-homefree.service'. The previous key is at $HOMEFREE_PREV_KEY."
  note "Home Free access key rotated. The console name is unchanged."
  log "Devices holding the previous key must be re-entered with the new one."
  log "Previous key kept at $HOMEFREE_PREV_KEY (0600) in case you need to roll back."
  homefree_print_credentials "$_name" "$_newkey"
}

homefree_disable() {
  homefree_lock_acquire "disabling Home Free"
  _homefree_disable_locked
  _rc=$?
  homefree_lock_release
  return $_rc
}

_homefree_disable_locked() {
  # Deliberately narrow: this turns OFF the relay agent and takes the key out of the live
  # config. It does NOT touch $APP.service, the app's env, sessions, transcripts, or engine
  # data — disabling remote access must never be a way to lose local state.
  # "Off" is decided by the LIVE KEY, not by the unit file — the unit is deliberately kept
  # so the operator can re-enable, so its presence says nothing about whether streaming is on.
  homefree_require_manageable_agent "disabling Home Free"
  if [ ! -f "$HOMEFREE_KEY_FILE" ]; then
    # Still converge systemd: a previous run may have quarantined the key and then failed
    # before reaching the service, and "already disabled" should end with it actually stopped.
    homefree_service_do stop || die "$APP-homefree.service could not be stopped — Home Free is NOT disabled. Check 'systemctl --user status $APP-homefree.service'."
    homefree_service_do disable || die "$APP-homefree.service could not be disabled — it may start again at login. Check 'systemctl --user status $APP-homefree.service'."
    log "Home Free is already disabled (no active access key) — nothing to change."
    return 0
  fi
  # Stop FIRST, and abort if it fails — before the key is touched. Quarantining the file while
  # the agent is still running achieves nothing: the agent already read the key at startup and
  # holds it in memory, so the box stays remotely reachable with a credential the operator has
  # been told is revoked. Moving the key would only destroy the evidence of which key that is.
  # Revocation you cannot confirm must not be reported as revocation.
  homefree_service_do stop || die "$APP-homefree.service could not be stopped — the access key has NOT been revoked and the agent may still be serving with it. Nothing was changed. Check 'systemctl --user status $APP-homefree.service' and re-run."
  homefree_service_do disable || die "$APP-homefree.service was stopped but could not be disabled, so it may start again at login. The access key has NOT been quarantined. Check 'systemctl --user status $APP-homefree.service' and re-run."
  # Only now, with the agent provably down, is the key taken out of the live config.
  # Quarantined rather than deleted: `disable` should be reversible by an operator who meant
  # `stop`. Either way it is out of the live config — the agent reads only
  # $HOMEFREE_KEY_FILE, and that path no longer exists.
  mv -f "$HOMEFREE_KEY_FILE" "$HOMEFREE_DISABLED_KEY" || die "could not quarantine the access key at $HOMEFREE_KEY_FILE"
  chmod 600 "$HOMEFREE_DISABLED_KEY" 2>/dev/null || true
  note "Home Free streaming is disabled. The relay agent is stopped and will not start at login."
  log "The access key is quarantined at $HOMEFREE_DISABLED_KEY (0600) and is no longer live."
  log "Your local install is untouched: $APP.service, sessions, transcripts and engine data are unchanged."
  # Said plainly rather than silently fixed. Enabling stream mode set AGENT_SESSIONS_AUTH_MODE=none,
  # and flipping that back here would lock out an operator who has no password set — so this
  # command leaves app auth exactly as it found it (its stated contract) and tells the operator
  # what that means instead of deciding for them.
  if grep -q '^AGENT_SESSIONS_AUTH_MODE=none' "$ENVF" 2>/dev/null; then
    note "NOTE: this box still runs with AGENT_SESSIONS_AUTH_MODE=none (set when streaming was enabled)."
    log "It is bound to loopback, so it is not reachable off-box — but any LOCAL account can now"
    log "reach the app with no password. To restore password auth: remove that line from"
    log "$ENVF, set a password, and restart $APP.service."
  fi
}

homefree_show_credentials() {
  if [ ! -f "$HOMEFREE_KEY_FILE" ]; then
    [ -f "$HOMEFREE_DISABLED_KEY" ] \
      && die "Home Free is disabled — there is no active access key. Re-enable with AGENT_SESSIONS_REMOTE=stream (which issues a fresh key)."
    die "Home Free is not set up on this box. Enable it with AGENT_SESSIONS_REMOTE=stream."
  fi
  _name="$(cat "$HOMEFREE_NAME_FILE" 2>/dev/null || true)"
  _key="$(cat "$HOMEFREE_KEY_FILE" 2>/dev/null || true)"
  [ -n "$_name" ] && [ -n "$_key" ] \
    || die "Home Free credentials at $HOMEFREE_DIR are incomplete — re-run with AGENT_SESSIONS_REMOTE=stream to repair."
  # Read-only: prints what already exists and generates nothing. Reuses the setup banner so
  # the "this key grants full control" warnings travel with the key every time it is shown.
  homefree_print_credentials "$_name" "$_key"
}

homefree_lifecycle_dispatch() {
  # Maintenance flags short-circuit the installer entirely — they operate on an install that
  # already exists and must never build a release as a side effect.
  #
  # Only `--homefree-*` spellings are claimed here. An unrecognised one is fatal rather than
  # ignored: these are security-relevant commands, and silently installing the app because
  # `--homefree-rotatekey` was mistyped is the worst possible response. Any OTHER argument is
  # left alone, preserving the previous behaviour of a script that parsed no arguments at all.
  case "${1:-}" in
    --homefree-rotate-key)       homefree_rotate_key; exit 0 ;;
    --homefree-disable)          homefree_disable; exit 0 ;;
    --homefree-show-credentials) homefree_show_credentials; exit 0 ;;
    --homefree-*)
      die "unknown option '$1' — expected --homefree-rotate-key, --homefree-disable or --homefree-show-credentials" ;;
    *) return 0 ;;
  esac
}

homefree_prompt_remote() {  # echo "stream" or "selfhost"; only prompts on a real tty
  [ -e /dev/tty ] || { echo selfhost; return 0; }
  {
    printf '\n  Remote access to this machine:\n'
    printf '    1) Self-host (default) — you provide reachability (your network / nginx)\n'
    printf '    2) Stream via BattleLab — reach it from anywhere with a name + key (free)\n'
    printf '  Choose [1]: '
  } > /dev/tty
  read -r _ans < /dev/tty || _ans=1
  case "$_ans" in 2 | stream | s) echo stream ;; *) echo selfhost ;; esac
}

homefree_maybe_setup() {  # self-host default; stream only when explicitly chosen
  _mode="$REMOTE"
  if [ -z "$_mode" ]; then
    if [ -t 0 ] && [ "${AGENT_SESSIONS_ASSUME_YES:-0}" != 1 ] \
      && [ "${AGENT_SESSIONS_NO_SERVICE:-0}" != 1 ]; then
      _mode="$(homefree_prompt_remote)"
    else
      _mode=selfhost  # non-interactive / no-tty / no-service → never contact a relay
    fi
  fi
  case "$_mode" in
    stream) homefree_setup ;;
    *) : ;;  # self-host: nothing extra
  esac
}

prune_releases() {
  # Keep the newest $KEEP_RELEASES (plus whatever `current` points at) for rollback.
  [ -d "$RELEASES" ] || return 0
  cur="$(readlink "$CURRENT" 2>/dev/null || true)"
  # shellcheck disable=SC2012
  ls -1dt "$RELEASES"/*/ 2>/dev/null | tail -n +"$((KEEP_RELEASES + 1))" | while read -r d; do
    [ "${d%/}" = "$cur" ] && continue
    rm -rf "$d"
  done
}

_healthcheck() {
  i=0
  while [ "$i" -lt 10 ]; do
    if curl -fsS -m 2 "http://$HOST:$PORT/healthz" >/dev/null 2>&1; then return 0; fi
    i=$((i + 1))
    sleep 1
  done
  return 1
}

manage_service() {
  prev="$1"  # the release `current` pointed at before this flip (rollback target)
  if [ "${AGENT_SESSIONS_NO_SERVICE:-0}" = 1 ] || ! systemctl --user >/dev/null 2>&1; then
    note "Service not started automatically (no systemctl --user session)."
    log "Start it with:  $CURRENT/venv/bin/agent-sessions serve"
    return 0
  fi
  render_unit
  systemctl --user daemon-reload
  systemctl --user enable "$APP.service" >/dev/null 2>&1 || true
  systemctl --user restart "$APP.service"
  _healthcheck && return 0
  # Unhealthy. Roll back to the previous release if there is one (self-update safety):
  # re-point `current` (atomic) + restart so a bad update can't leave the host down.
  if [ -n "$prev" ] && [ "$prev" != "$rel" ] && [ -d "$prev" ]; then
    log "new release failed /healthz — rolling back to $(basename "$prev")"
    rb="$PREFIX/.current.rb.$$"
    ln -s "$prev" "$rb"
    mv -Tf "$rb" "$CURRENT" 2>/dev/null || { rm -f "$rb"; ln -sfn "$prev" "$CURRENT"; }
    systemctl --user restart "$APP.service"
    _healthcheck && die "update failed health check — rolled back to the previous release"
    die "update failed and the rollback release is also unhealthy"
  fi
  die "service started but /healthz never came up on $HOST:$PORT"
}

migrate_legacy_autoupdate() {
  # The systemd autoupdate timer is retired (#538): the app now schedules the daily
  # check itself, gated on the AGENT_SESSIONS_AUTOUPDATE env-file key (Settings →
  # System → Updates). Preserve a previously-enabled timer as the in-app opt-in, then
  # remove the legacy units. No-op on fresh installs and once migrated.
  [ "${AGENT_SESSIONS_NO_SERVICE:-0}" = 1 ] && return 0
  systemctl --user >/dev/null 2>&1 || return 0
  [ -f "$UNIT_DIR/$APP-update.timer" ] || [ -f "$UNIT_DIR/$APP-update.service" ] || return 0
  if systemctl --user is-enabled "$APP-update.timer" >/dev/null 2>&1; then
    _env_set_if_absent AGENT_SESSIONS_AUTOUPDATE 1
  fi
  systemctl --user disable --now "$APP-update.timer" >/dev/null 2>&1 || true
  rm -f "$UNIT_DIR/$APP-update.timer" "$UNIT_DIR/$APP-update.service"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  log "migrated the legacy autoupdate timer → in-app automatic updates (Settings → System)"
}

seed_onboarding() {
  # First-run onboarding pref (#675). A genuine fresh install seeds onboarded=false so the
  # setup wizard shows even when the engines' session history was preserved (uninstall keeps
  # ~/.claude etc., so the app's session-scan fallback would otherwise infer "already
  # onboarded"). An upgrade seeds onboarded=true so a returning user is never dragged back
  # through setup (#463). Idempotent: only writes when the pref is currently unset, via the
  # app's own prefs writer so the path (honoring AGENT_SESSIONS_PREFS) + atomic write match
  # what the running app uses.
  _py="$CURRENT/venv/bin/python"
  [ -x "$_py" ] || return 0
  _pref="$(sed -n 's/^AGENT_SESSIONS_PREFS=//p' "$ENVF" 2>/dev/null | tail -1)"
  _pref="${_pref:-${AGENT_SESSIONS_PREFS:-}}"
  _want=True; [ "${FRESH:-0}" = 1 ] && _want=False
  if [ -n "$_pref" ]; then
    AGENT_SESSIONS_PREFS="$_pref" "$_py" -c 'import sys
from agent_sessions import prefs
prefs.get_onboarded() is None and prefs.set_onboarded(sys.argv[1] == "True")' "$_want" 2>/dev/null || true
  else
    "$_py" -c 'import sys
from agent_sessions import prefs
prefs.get_onboarded() is None and prefs.set_onboarded(sys.argv[1] == "True")' "$_want" 2>/dev/null || true
  fi
}

main() {
  # Maintenance flags first: they act on an existing install and exit without building
  # anything (#612). A no-flag invocation falls straight through to the install path.
  homefree_lifecycle_dispatch "${1:-}"
  # Fresh vs upgrade (#675): key off a *completed* prior install — a valid `current`
  # symlink whose target exists — not the mere presence of `releases/`. A failed first
  # install can leave an empty `releases/` behind (the trap removes only the half-built
  # release dir), and `current` is only ever created after a build succeeds, so this
  # correctly treats a retry-after-failure as still-fresh.
  FRESH=1; [ -L "$CURRENT" ] && [ -e "$CURRENT" ] && FRESH=0
  mkdir -p "$PREFIX"
  adopt_persisted_bind    # re-run: a persisted bind in the env file wins (no silent revert to localhost)
  adopt_persisted_channel # re-run: a persisted (UI-chosen) channel wins the same way (#538)
  choose_host             # fresh interactive install: offer to bind a chosen address / all interfaces
  ensure_prereqs
  ref="$(resolve_ref)"
  log "installing $APP (${ref:-default branch}) into $PREFIX …"
  rel=""
  # Remove a half-built release on any failure before `current` is flipped — the prior
  # release keeps serving (rollback-safe). Cleared once the flip succeeds.
  trap 'rm -rf "$rel"' EXIT INT TERM
  build_release "$ref"  # sets $rel
  # Write/refresh the unit before flipping so the ExecStart path is valid — but ONLY when
  # we'll actually manage the service. Under NO_SERVICE we must not touch the host's
  # systemd unit at all (it's a per-user, not per-HOME, path — otherwise a scratch/test
  # install would clobber the real unit).
  [ "${AGENT_SESSIONS_NO_SERVICE:-0}" = 1 ] || render_unit
  password="$(write_env_if_absent "$rel")"
  # Bring the env up to the current serving contract (React UI + ws terminal). Runs for
  # BOTH a fresh install and an upgrade of an older env — idempotent + non-destructive,
  # so re-running the installer actually cuts an existing deployment over to the React UI.
  migrate_env
  # Atomic flip: create the new link beside `current`, then rename(2) it over the old
  # one — atomic on the same filesystem, so a concurrent start/health-check/restart never
  # sees a missing `current` (unlike `ln -sfn`, which unlinks then recreates). Falls back
  # to a plain swap where `mv -T` is unavailable. One-step rollback = re-point to a prior
  # release dir.
  prev_target="$(readlink "$CURRENT" 2>/dev/null || true)"  # for rollback on a bad update
  tmp_link="$PREFIX/.current.$$"
  ln -s "$rel" "$tmp_link"
  mv -Tf "$tmp_link" "$CURRENT" 2>/dev/null || { rm -f "$tmp_link"; ln -sfn "$rel" "$CURRENT"; }
  trap - EXIT INT TERM        # release is live; do not clean it up
  prune_releases
  # Discover installed agent CLIs and record their paths in the env (best-effort; also
  # re-runs on every upgrade so newly-installed engines are picked up).
  "$CURRENT/venv/bin/agent-sessions" doctor --env "$ENVF" >/dev/null 2>&1 || true
  seed_onboarding            # #675: fresh install ⇒ show the setup wizard; upgrade ⇒ leave it
  migrate_legacy_autoupdate  # retire the systemd timer → in-app setting BEFORE the service (re)starts
  manage_service "$prev_target"
  version="$("$CURRENT/venv/bin/agent-sessions" version 2>/dev/null || echo '?')"

  note "agent-sessions $version installed."
  log "URL:     $ORIGIN"
  if [ -n "$password" ]; then
    log "username: admin"
    log "password: $password"
    note "Save the password now — it is shown ONCE and only the hash is stored."
  else
    log "(existing credentials kept; upgrade in place)"
  fi

  homefree_maybe_setup  # optional stream channel (#27) — self-host default, no relay contacted
}

main "$@"
