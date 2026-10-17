#!/usr/bin/env bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

# Keep this helper in each standalone recipe: guests receive one script at a time.
apt_run() (
  # A subshell keeps temporary-file and signal traps local to this command.
  local command="$1"
  shift
  local output deadline remaining=300 delay status
  local -a pipeline_status
  output=$(mktemp)
  trap 'rm -f "$output"' EXIT
  trap 'exit 130' INT
  trap 'exit 143' TERM
  deadline=$((SECONDS + remaining))

  while true; do
    # Native dpkg waits and our retries use the same budget. Lists and archives
    # do not use dpkg's timeout, and apt-mark can fail directly inside dpkg.
    if LC_ALL=C "$command" -o DPkg::Lock::Timeout="$remaining" -o Acquire::Retries=3 "$@" \
      2>&1 | tee "$output" >&2; then
      return 0
    else
      pipeline_status=("${PIPESTATUS[@]}")
      status=${pipeline_status[0]}
      # A log capture failure must also stop provisioning.
      if ((status == 0)); then
        return "${pipeline_status[1]}"
      fi
    fi
    # Permission errors and broken packages must fail, even with exit code 100.
    if ! grep -Eq \
      'Could not get lock .*(It is held by process [0-9]+|Resource temporarily unavailable)|dpkg: error: dpkg (frontend|database) lock was locked by (another process|.+ process with pid [0-9]+)' \
      "$output"; then
      return "$status"
    fi
    remaining=$((deadline - SECONDS))
    if ((remaining <= 0)); then
      break
    fi
    delay=5
    if ((remaining < delay)); then
      delay=$remaining
    fi
    printf 'Waiting for APT lock (%s): retrying in %ss (%ss remaining).\n' \
      "$command" "$delay" "$remaining" >&2
    sleep "$delay"
    remaining=$((deadline - SECONDS))
    if ((remaining <= 0)); then
      break
    fi
  done
  printf 'Timed out waiting for APT lock (%s) after 300s.\n' "$command" >&2
  return "$status"
)

# Keep common guest tools separate from container runtimes and cluster packages.
# Keep native dpkg lock messages visible; -qq would hide them while APT waits.
apt_run apt-get update -q
apt_run apt-get install -y -q \
  bash-completion ca-certificates curl dnsutils gpg jq rsync vim
