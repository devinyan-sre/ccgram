#!/usr/bin/env bash
# Health-gated deploy for a local-fork install, with automatic rollback.
#
# The plain deploy path (`uv tool install --force --reinstall . && systemctl
# restart`) reports success as soon as the *install* succeeds — it says nothing
# about whether the service actually came back up. A bad commit therefore left
# a dead bot behind a "deployed OK" message.
#
# This wraps that path: record the current commit, deploy, then require the
# service to reach a healthy state within a timeout. If it does not, reinstall
# the previous commit and restart.
#
# Rollback builds the previous commit in a temporary git worktree rather than
# checking it out in place, so the user's working tree is never touched.
#
# Usage:
#   scripts/deploy.sh [--no-rollback] [--timeout SECONDS]
set -euo pipefail

SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SERVICE="${CCGRAM_SERVICE:-ccgram}"
TIMEOUT=60
AUTO_ROLLBACK=1

while [[ $# -gt 0 ]]; do
	case "$1" in
	--no-rollback) AUTO_ROLLBACK=0 ;;
	--timeout)
		TIMEOUT="$2"
		shift
		;;
	-h | --help)
		sed -n '2,18p' "$0"
		exit 0
		;;
	*)
		echo "unknown argument: $1" >&2
		exit 2
		;;
	esac
	shift
done

log() { echo "[deploy] $*"; }
fail() {
	echo "[deploy] ERROR: $*" >&2
	exit 1
}

# --- health ---------------------------------------------------------------

metrics_port() {
	# Read from the deployed env file; empty/0 means the endpoint is disabled.
	local env_file="${HOME}/.ccgram/.env"
	[[ -f "${env_file}" ]] || return 0
	grep -E '^CCGRAM_METRICS_PORT=' "${env_file}" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '[:space:]'
}

healthy() {
	# 1. systemd must consider the unit running. Under Type=notify this already
	#    means the bot sent READY=1, i.e. bootstrap completed.
	[[ "$(systemctl --user is-active "${SERVICE}" 2>/dev/null)" == "active" ]] || return 1

	# 2. If the metrics listener is enabled, require /healthz too — it is backed
	#    by the same gate the systemd watchdog uses, so this catches a process
	#    that is up but whose core loops are not making progress.
	local port
	port="$(metrics_port)"
	if [[ -n "${port}" && "${port}" != "0" ]]; then
		curl -fsS -o /dev/null --max-time 5 "http://127.0.0.1:${port}/healthz" || return 1
	fi
	return 0
}

wait_for_health() {
	local deadline=$((SECONDS + TIMEOUT))
	while ((SECONDS < deadline)); do
		if healthy; then return 0; fi
		sleep 2
	done
	return 1
}

restart_count() {
	systemctl --user show "${SERVICE}" -p NRestarts --value 2>/dev/null || echo 0
}

install_from() {
	log "installing from $1"
	uv tool install --force --reinstall "$1" >/dev/null
}

diagnostics() {
	echo "--- systemctl status ---" >&2
	systemctl --user status "${SERVICE}" --no-pager 2>&1 | tail -20 >&2
	echo "--- recent log ---" >&2
	tail -30 "${HOME}/.ccgram/ccgram.log" 2>/dev/null >&2 || true
}

# --- deploy ---------------------------------------------------------------

command -v uv >/dev/null || fail "uv not found in PATH"
git -C "${SRC_DIR}" rev-parse --git-dir >/dev/null 2>&1 || fail "${SRC_DIR} is not a git repo"

PREV_REF="$(git -C "${SRC_DIR}" rev-parse HEAD)"
NEW_REF="$(git -C "${SRC_DIR}" rev-parse --short HEAD)"
BASELINE_RESTARTS="$(restart_count)"

log "deploying ${NEW_REF} (rollback target: ${PREV_REF:0:8})"
install_from "${SRC_DIR}"
systemctl --user restart "${SERVICE}"

if wait_for_health; then
	# A unit that is 'active' only because systemd already restarted it after a
	# crash is not a successful deploy — compare the restart counter too.
	if [[ "$(restart_count)" != "${BASELINE_RESTARTS}" ]]; then
		log "WARNING: NRestarts changed (${BASELINE_RESTARTS} -> $(restart_count)); the service crashed at least once"
	fi
	log "healthy — deploy of ${NEW_REF} complete"
	exit 0
fi

log "service did not become healthy within ${TIMEOUT}s"
diagnostics

if [[ "${AUTO_ROLLBACK}" != "1" ]]; then
	fail "deploy failed; --no-rollback set, leaving ${NEW_REF} installed"
fi

# --- rollback -------------------------------------------------------------

ROLLBACK_TREE="$(mktemp -d)"
cleanup() {
	git -C "${SRC_DIR}" worktree remove --force "${ROLLBACK_TREE}" 2>/dev/null || true
	rm -rf "${ROLLBACK_TREE}" 2>/dev/null || true
}
trap cleanup EXIT

log "rolling back to ${PREV_REF:0:8}"
# --detach: a rollback build must never claim a branch name.
rm -rf "${ROLLBACK_TREE}"
git -C "${SRC_DIR}" worktree add --detach "${ROLLBACK_TREE}" "${PREV_REF}" >/dev/null 2>&1 ||
	fail "could not create rollback worktree at ${PREV_REF}"

install_from "${ROLLBACK_TREE}"
systemctl --user restart "${SERVICE}"

if wait_for_health; then
	log "rolled back to ${PREV_REF:0:8} and healthy"
	exit 1 # deploy still failed, even though recovery worked
fi

diagnostics
fail "rollback to ${PREV_REF:0:8} also unhealthy — manual intervention required"
