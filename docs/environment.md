# Set guest environment variables

Add variables to each machine in `machines.yml`. Use `environment` for visible
settings and `env_file` for values kept in separate files:

```yaml
machines:
  cp:
    box: ubuntu-26.04-arm64
    ssh_user: ubuntu
    # Keep this machine's resources, networks, ports and scripts.
    env_file:
      - env/common.env
      - env/control-plane.env
    environment:
      LAB_NAME: cka
      LOG_LEVEL: debug
      FEATURE_ENABLED: "true"
```

Create `env/common.env` beside the inventory:

```dotenv
LAB_NAME=practice
LOG_LEVEL=info
HTTP_PROXY=http://proxy.example.com:3128
NO_PROXY=localhost,127.0.0.1,192.168.76.0/24
```

Create `env/control-plane.env`, for example with `LOG_LEVEL=warning`.
After `task up`, the guest has `LAB_NAME=cka` and `LOG_LEVEL=debug` because
the YAML values have priority. Remove the proxy variables if you do not use a proxy.

Open a guest shell and read a value:

```sh
task connect -- cp
printenv LAB_NAME
```

This also works with `task up:no-provision`. Environment setup belongs to
cloud-init and does not require a provisioning script.

## Files, values and priority

`env_file` accepts one path or a list. Paths are relative to the directory
containing `machines.yml`; absolute paths, parent paths and links outside that
directory are rejected. Each file must exist and contain at most 1 MiB of UTF-8
text. Files for inactive profiles are read only when those machines are selected.
Labstack does not load a host `.env` automatically or copy the Mac's environment.

Values are applied in this order:

1. Read `env_file` entries from first to last. Later values replace earlier ones.
2. Apply `environment` from the machine's YAML. It overrides every file value,
   including when its value is an empty string.

Use a mapping for `environment`, with string values. Quote numbers and booleans
such as `"8080"` and `"true"`. An empty string is valid; null values are not.
Names use letters, digits and underscores, starting with a letter or underscore.
The [built-in script context](configuration.md#choose-the-provisioning-scripts),
including `NODE_NAME` and `SSH_USER`, cannot be overridden.

Dotenv files accept `NAME=value`, empty values such as `NAME=`, blank lines,
comments, quoted values and an optional `export` prefix. Values must fit on one
line and cannot contain NUL or carriage returns. Use quotes around spaces or `#`:

```dotenv
LABEL="My CKA lab"
EMPTY=
LITERAL='${HOST_VALUE} and $(hostname) stay as text'
```

Labstack does not expand `${VARIABLE}`, run shell commands or apply Compose's
interpolation rules. Double-quoted dotenv values support escape sequences such as
`\t`; escapes that produce line breaks are rejected. A missing file or invalid
entry stops creation before image downloads or VM creation. Errors name the file
and line without printing its values.

## Scripts, healthchecks and shells

Provisioning scripts receive the variables inside their root shell, after `sudo`.
Read them normally, for example with `"$LAB_NAME"`. Healthchecks receive the same
saved values under the configured SSH account:

```yaml
healthcheck:
  test: [CMD-SHELL, 'test "$LOG_LEVEL" = debug']
```

`CMD-SHELL` expands shell expressions. With `CMD`, arguments stay literal; use
`[CMD, printenv, LAB_NAME]` to read a variable without a shell expression.
A `sudo` command inside a healthcheck or an interactive session can filter its
environment. Use a root login shell with `sudo -i` when needed.

Guest login shells load the variables through `/etc/profile.d/labstack.sh`.
For a non-login SSH command, source the generated shell file explicitly:

```sh
ssh -F .state/ssh_config cp '. /etc/labstack/environment.sh; printenv LAB_NAME'
```

## Use variables in a systemd service

Services do not inherit variables from your SSH session. Add this to a service
unit or its drop-in, then reload and restart that service:

```ini
[Service]
EnvironmentFile=/etc/labstack/environment
```

To check the file with a temporary service:

```sh
sudo systemd-run --wait --pipe \
  --property=EnvironmentFile=/etc/labstack/environment \
  /usr/bin/printenv LAB_NAME
```

The systemd file contains `NAME=value` entries. The separate
`/etc/labstack/environment.sh` file contains shell exports because the two formats
use different quoting rules. Do not source the systemd file as a shell script.
Setting proxy variables here does not configure every system service; each service
that needs them must load the file.

## Keep values private and reproducible

Guest environment files are owned by root, readable by the SSH account's group,
and have mode `0640`. The host state and cloud-init seed also contain the resolved
values inside the private `.state/` directory. Treat them as private data, not an
encrypted secret store. The state file has mode `0600`; `task destroy` removes
the state and guest disks. Logs are retained, so avoid printing credentials or
using shell tracing in scripts and healthchecks.

Values are resolved when the lab is created and saved in `.state/state.json`.
Later starts and `task reset` reuse that snapshot even if a source file changes
or disappears. To apply new values, run `task destroy`, then `task up`.
Changes to the YAML environment or file list also require recreating the lab.

The repository ignores `.env`, `.env.*` and `*.env` files. Use `*.env.example`
for tracked examples and keep real credentials out of `machines.yml`.
