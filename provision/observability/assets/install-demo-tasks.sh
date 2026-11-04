#!/usr/bin/env bash
set -euo pipefail
: "${PROVISION_ASSETS_DIR:?Labstack must transfer the recipe assets}"
: "${SSH_USER:?Labstack must identify the guest login account}"

readonly task_version=3.46.4
if ! command -v task >/dev/null || [[ $(task --version) != "Task version: v${task_version}" ]]; then
  case $(dpkg --print-architecture) in
    arm64) architecture=arm64; checksum=9079bbdfd439f2168ee380d9167fd77f13f9fab7afb516205ac3a578f16bcb59 ;;
    amd64) architecture=amd64; checksum=4b0098862292de03d568c851b207bce5fdb8a59cb533d6ed4132386151926c46 ;;
    *) printf 'Unsupported Task architecture\n' >&2; exit 1 ;;
  esac
  temporary_directory=$(mktemp -d)
  trap 'rm -rf "$temporary_directory"' EXIT
  archive="$temporary_directory/task.tar.gz"
  curl -fsSL --retry 3 "https://github.com/go-task/task/releases/download/v${task_version}/task_linux_${architecture}.tar.gz" -o "$archive"
  printf '%s  %s\n' "$checksum" "$archive" | sha256sum --check --status
  tar -xzf "$archive" -C "$temporary_directory" task
  install -m 0755 "$temporary_directory/task" /usr/local/bin/task
fi

install -d -m 0755 /opt/labstack-demo
install -m 0644 "$PROVISION_ASSETS_DIR/Taskfile.yml" /opt/labstack-demo/Taskfile.yml
install -m 0755 "$PROVISION_ASSETS_DIR/demo.sh" /usr/local/sbin/labstack-demo
presenter_home=$(getent passwd "$SSH_USER" | cut -d: -f6)
[[ -n $presenter_home && -d $presenter_home ]]
# Preserve an existing personal Taskfile; the installed copy is always available with -d.
for filename in Taskfile.yml Taskfile.yaml taskfile.yml taskfile.yaml; do
  if [[ -e "$presenter_home/$filename" || -L "$presenter_home/$filename" ]]; then
    exit 0
  fi
done
ln -s /opt/labstack-demo/Taskfile.yml "$presenter_home/Taskfile.yml"
