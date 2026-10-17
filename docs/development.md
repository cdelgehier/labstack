# Develop Labstack

Run commands from the project directory.

## Checks

GitHub Actions runs `task ci` on pull requests and pushes to `main`, using Python
3.15 on Ubuntu. QEMU, xorriso and rsync are available for disk, ISO and file transfer
tests. These checks do not start guests. Guest runtime checks use `task test:runtime`
on macOS with Apple Silicon.

The workflow saves the pytest report for seven days, including failed runs.
`task test` also writes this report to `logs/pytest.xml`.

Install the Git hooks once:

```sh
task pre-commit:install
```

The hooks check commit messages with Commitizen, the uv lock file, Python lint,
formatting and types, shell scripts, and file contents.
They also check for merge conflicts, private keys and large files.
Unit tests run when code, provisioning or configuration files change.
Ruff fixes lint issues where possible and formats Python files.
If a hook changes a file, review it and stage it again before committing.

Use `task pre-commit:run` for staged files or `task pre-commit:all` for all files.
`task pre-commit:autoupdate` updates the pinned hook versions.
Commitizen's branch check is available as a manual hook.

Ruff uses an 88-character line length and the project settings in `pyproject.toml`.
`task test` shows coverage for `labstack/` and lists lines not covered by unit tests.
Commitizen uses the project version, updates the changelog and keeps `uv.lock`
in the version commit. Before `1.0`, breaking changes increase the minor version.

`task test` covers inventory validation, machine names, image selection, topology choices, provisioning choices, restart settings, cloud-init access, network routes and image checksums.
It checks custom IPs and ports across cloud-init, QEMU, SSH and kubeconfig, and rejects network and port conflicts.
It also rejects base disks with backing files when QEMU is installed.
Image tests cover catalogue and manifest validation, stable version selection,
fixed versions, shared image resolution, offline restarts and resets, and local disk overrides.
Recovery tests cover partial state writes, reset preflight failures and retrying a
reset before new state exists, and cancelling that reset with destroy. They also
check older monitor and PID paths, moving stopped resources without losing data,
and refusing startup during a pending reset. Recipe transfers cannot overwrite
VM disks, long names use short private sockets, and YAML order does not select the
Kubernetes control plane.
Settings tests cover Task environment values and host paths. Logging tests check JSON
and terminal output, log levels and the separation between stderr and command results.
Connection tests run the actual Task command with an SSH stub. They check custom
machine names, unavailable guests, missing arguments and safe argument quoting.
Provisioning tests cover script anchors, execution order, node context, invalid paths,
readiness checkpoints, dependency cycles, inactive and optional dependencies,
stopping after a failure, join command validation and skipping scripts with `up:no-provision`, then provisioning the same disks.
Package tests cover APT and dpkg lock retries, the shared five-minute deadline,
and immediate failures for other errors. On Linux, they also hold a real POSIX
lock while APT uses an empty, isolated sources list. This check needs no network
or package installation and runs in the existing CI job.

`task test:runtime` checks Ubuntu and ARM64, SSH, sudo, cloud-init completion, disk growth, unique machine identities and SSH host keys, and communication between every pair of nodes.
After successful provisioning with the Kubernetes recipe, it also checks Kubernetes versions, node readiness, API access from the Mac, metrics, cross-node DNS and HTTP, and volume persistence after a pod is replaced.
Test workloads are removed after success or failure. Diagnostic logs are kept.

## Host settings

`settings.py` reads environment variables with the `LABSTACK_` prefix.
Machine definitions remain in `machines.yml`; host settings control the paths and
command output. The default root is the repository that contains the package.

| Variable | Default | Purpose |
| --- | --- | --- |
| `LABSTACK_ROOT` | Package parent directory | Project directory containing `machines.yml` and `provision/` |
| `LABSTACK_CACHE_DIR` | `.cache` | Downloaded base disks and the checked kubectl binary |
| `LABSTACK_LOGS_DIR` | `logs` | Serial, provisioning and runtime diagnostic logs |
| `LABSTACK_LOG_LEVEL` | `info` | `debug`, `info`, `warning` or `error` |
| `PROFILES` | Inventory value | Comma-separated machine groups for a new lab |
| `IMAGE_PATH` | Unset | Use a standalone Image Factory Ubuntu QCOW2 disk |
| `KUBECTL_ARGS` | `get nodes -o wide` | Arguments passed by `task kubectl -- ...` |

Relative cache and log paths resolve under `LABSTACK_ROOT`. State stays in
`<root>/.state` so cleanup cannot remove another directory. Cache and logs must
stay outside that directory because `task destroy` keeps them.

For more detail during startup:

```sh
LABSTACK_LOG_LEVEL=debug task up
```

Captured events include the service name `labstack`, a module name and a UTC
timestamp. Guest output stays in the diagnostic files rather than being copied
into each event.
