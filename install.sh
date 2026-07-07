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
REF="${AGENT_SESSIONS_REF:-}"
# Track whether the channel was set explicitly (env) vs defaulted: the UI persists a channel
# choice in the env file (#538), and a re-run without the env var must follow that choice
# (adopt_persisted_channel) instead of silently flipping a main-channel install to stable.
CHANNEL_EXPLICIT=0; [ -n "${AGENT_SESSIONS_CHANNEL:-}" ] && CHANNEL_EXPLICIT=1
CHANNEL="${AGENT_SESSIONS_CHANNEL:-stable}"
# Track whether HOST/ORIGIN were set explicitly (env) vs defaulted: an explicit value
# suppresses the interactive bind prompt and the derived-origin recompute (choose_host).
HOST_EXPLICIT=0; [ -n "${AGENT_SESSIONS_HOST:-}" ] && HOST_EXPLICIT=1
HOST="${AGENT_SESSIONS_HOST:-127.0.0.1}"
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
    mkdir -p "$tdir"
    log "fetching a self-contained Node $NODE_VERSION ($na) for the UI build…"
    curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-$na.tar.gz" \
      -o "$tdir/node.tar.gz" || die "could not download Node $NODE_VERSION"
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
  # Re-run / upgrade / autoupdate: the systemd unit bakes `--host` from the install-time shell
  # var, but `serve --host` only *defaults* to $AGENT_SESSIONS_HOST — so a re-run with no
  # AGENT_SESSIONS_HOST in the environment would regenerate the unit with 127.0.0.1 and silently
  # revert a prior 0.0.0.0 / LAN bind. Adopt the persisted choice from the env file (and treat it
  # as explicit, so choose_host doesn't re-prompt). An env var passed on THIS run still wins.
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
  ( cd "$rel/src/web" && "$NPM" ci --no-audit --no-fund --silent && "$NPM" run build --silent ) \
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
# Put ~/.local/bin first so sessions spawned by the app (claude/opencode/codex/gemini/agy,
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
  cat > "$HOMEFREE_UNIT" <<EOF
[Unit]
Description=agent-sessions Home Free relay agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=AGENT_SESSIONS_RELAY_URL=${AGENT_SESSIONS_RELAY_URL:-wss://REPLACE-WITH-YOUR-RELAY/relay/ws}
Environment=HOMEFREE_CONSOLE_NAME_FILE=$HOMEFREE_DIR/console_name
Environment=HOMEFREE_ACCESS_KEY_FILE=$HOMEFREE_DIR/access_key
Environment=HOMEFREE_IDENTITY_PATH=$HOMEFREE_DIR/identity
ExecStart=$CURRENT/venv/bin/python -m agent_sessions.homefree
Restart=on-failure
RestartSec=3

[Install]
WantedBy=default.target
EOF
}

homefree_print_credentials() {
  _name="$1"; _key="$2"
  if [ -t 1 ]; then _R='\033[1;31m'; _B='\033[1m'; _Z='\033[0m'; else _R=''; _B=''; _Z=''; fi
  note "BattleLab remote (stream) is enabled — reach this box from any browser."
  log "Console name: ${_name}"
  log "Access key:   ${_key}"
  printf '  %sOpen the connect page for your relay and enter the name + key above.%s\n' "$_B" "$_Z"
  printf '\n'
  printf '  %s* SECURITY: the access key grants FULL CONTROL of this machine.%s\n' "$_R" "$_Z"
  printf '  %sNever enter it for anyone who contacted you. BattleLab staff will%s\n' "$_R" "$_Z"
  printf '  %sNEVER ask for your access key or console name.%s\n' "$_R" "$_Z"
}

homefree_setup() {
  mkdir -p "$HOMEFREE_DIR"; chmod 700 "$HOMEFREE_DIR" 2>/dev/null || true
  [ -f "$HOMEFREE_DIR/console_name" ] || homefree_gen_name > "$HOMEFREE_DIR/console_name"
  [ -f "$HOMEFREE_DIR/access_key" ] || homefree_gen_key > "$HOMEFREE_DIR/access_key"
  chmod 600 "$HOMEFREE_DIR/console_name" "$HOMEFREE_DIR/access_key" 2>/dev/null || true
  _name="$(cat "$HOMEFREE_DIR/console_name")"
  _key="$(cat "$HOMEFREE_DIR/access_key")"
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

main() {
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
