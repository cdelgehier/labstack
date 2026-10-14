# Use your lab

Run commands from the project directory.

## Practice the installation

Start clean Ubuntu nodes without installing Kubernetes or containerd:

```sh
task up:no-provision
task connect -- cp
```

This command sets up SSH, passwordless sudo, hostnames, a private network and disks at the configured capacity.
It skips every script and healthcheck, including common package installation.
Use it to practice kubeadm or a manual installation such as Kubernetes The Hard Way.
Add `PROFILES=extra-worker` if you need a second worker.

The selected profiles and inventory are saved. After `task down`, use
`task up:no-provision` again to resume without running the scripts.
A plain `task up` runs the configured lists on the same machines and disks.
It can therefore turn a clean infrastructure into a provisioned cluster.
Skipping scripts does not remove software that is already installed.

To change the inventory or selected profiles, remove the existing nodes first:

```sh
task destroy
task up:no-provision PROFILES=extra-worker
```

Open guest shells with `task connect -- cp`, `task connect -- w1` and `task connect -- w2`.
The value after `--` is the exact machine name from `machines.yml`, saved when
the lab was created. Task passes that value to Labstack. Use `task status` to list
available names; `w2` is available only in a lab started with the `extra-worker` profile.
For commands or file copies, use the generated SSH configuration:

```sh
ssh -F .state/ssh_config w1 'hostname; ip -br address'
scp -F .state/ssh_config ./example.conf w1:/tmp/
```

Private keys stay on the Mac. All guest access uses the `ubuntu` account.
For a manual installation, use the private addresses below for node communication.
By default, the Mac can reach the API through `127.0.0.1:16443`.
The port comes from the control plane's mapping for guest port `6443`; include `127.0.0.1` in the API certificate's subject alternative names.
If your installation creates `/etc/kubernetes/admin.conf` and installs kubectl on the control plane, `task kubeconfig` copies that configuration to the Mac and sets the forwarded endpoint.

## Daily commands

| Command | Result |
| --- | --- |
| `task prerequisites` | Install tools, Python and project dependencies |
| `task up` | Create or resume the environment, run scripts and check health |
| `task up PROFILES=extra-worker` | Create a new lab with the optional second worker |
| `task up:no-provision` | Create or resume without running scripts |
| `task status` | Show VM state and cluster nodes |
| `task connect -- cp` | Open a shell on the control plane |
| `task connect -- w1`, `task connect -- w2` | Open a shell on a worker |
| `task kubeconfig` | Export cluster admin access to `./kubeconfig` |
| `task down` | Stop the VMs and keep disks |
| `task reset` | Recreate the saved topology; run scripts if provisioning previously completed |
| `task destroy` | Stop the VMs and remove disks, keys and kubeconfig |
| `task test:runtime` | Check the running guests and, when selected, the Kubernetes cluster |
| `task ci` | Check Python, shell scripts and unit tests without starting VMs |

`task destroy` keeps the downloaded base disk in `.cache/` and diagnostic logs in `logs/`.
Base disks are cached by checksum and kept read-only. Machines with the same image share one base.
Each VM writes to its own QCOW2 overlay in `.state/machines/<name>/disk.qcow2`.
The default paths are `.state/machines/cp/disk.qcow2`, `.state/machines/w1/disk.qcow2` and, when enabled, `.state/machines/w2/disk.qcow2`.
Recipe files use a separate `.state/shared/` directory, so synchronization never copies a VM disk.
QEMU monitor and network sockets use a private `/tmp/labstack-<user-id>-<digest>/` directory.
Its short path avoids the macOS socket length limit, even with long project paths or machine names.
`task destroy` removes that directory after the guests stop.
If an existing lab still stores disks directly under `.state/<name>/`, run
`task down` before `task up`. Startup moves its stopped resources into
`.state/machines/<name>/`, preserving disks and UEFI state. Recipe output stays
in `.state/shared/`. If both layouts contain the same resource, Labstack stops
with an error so you can inspect the files before changing them. Saved state records
the new layout after migration, so later recipe output cannot be confused with an
old VM resource.
`du` shows the space used on the Mac, not the configured virtual capacity of each disk.
If startup fails or you interrupt it, use `task status` to inspect the nodes and `task down` to stop them.
Logs include serial output, provisioning output, healthcheck attempts and runtime diagnostics.
Shutdown allows up to two minutes per guest for container cleanup and disk writes.
If a guest still does not stop, Labstack stops its QEMU process and records a warning.

`task reset` checks tools, scripts, base images, disk capacity and available ports
before removing the environment. It keeps the saved recipe in a private `.reset.json`
file until recreation succeeds. If recreation fails, fix the reported problem and
run `task reset` again to keep the same image pins, profiles and provisioning choice.
An ordinary `task up` is blocked while a reset is pending. To cancel it and remove
the unfinished lab, use `task destroy`; cached images and logs are kept.
State updates are atomic: an interrupted write leaves the previous saved state readable.

## Use a disk from an Image Factory build

For a new environment, give the standalone Ubuntu QCOW2 path:

```sh
task up IMAGE_PATH="$HOME/git/perso/image-factory/dist/ubuntu-26.04-arm64/disk/ubuntu-26.04-arm64.qcow2"
```

`IMAGE_PATH` overrides the image for every machine in a new environment.
It bypasses catalogue and manifest requests.
The disk must be a standalone QCOW2 image with the Image Factory Ubuntu account and cloud-init setup.
It is checked and copied into the cache, so later writes to the original file cannot affect the nodes.

## Optional API access from the Mac

With kubectl installed on the Mac, run:

```sh
task kubeconfig
kubectl --kubeconfig ./kubeconfig get ns
kubectl --kubeconfig ./kubeconfig get nodes -o wide
```

The task writes `kubeconfig` in the project directory with mode `0600`.
It replaces the guest API address with the published localhost port, which is
`https://127.0.0.1:16443` by default. The API certificate covers localhost,
so TLS verification stays enabled. This file gives cluster admin access;
keep it private. `task up` also exports it after successful Kubernetes provisioning.

`task kubectl -- ...` uses a checked kubectl 1.37.1 binary in the cache and the project's kubeconfig.
It does not replace your Mac's kubectl or change `~/.kube/config`.
