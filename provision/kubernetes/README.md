# CKS practice lab

This recipe builds a Kubernetes practice environment with Cilium on Ubuntu
ARM64 VMs. It is an example lab provisioned with Labstack.

The saved inventory is [machines.yml.cks](../../machines.yml.cks). It defines
one control plane (`cp`), one worker (`w1`) and an optional second worker (`w2`).
The recipe currently installs Kubernetes 1.37.1.

## Start the lab

Install the host prerequisites as described in the
[Labstack README](../../README.md#quick-start). If a lab already exists, run
`task destroy` before switching; this removes its VM disks and access keys.

From the project directory:

```sh
cp machines.yml.cks machines.yml
task up
task connect -- cp
```

For a new environment with the second worker, use
`task up PROFILES=extra-worker` instead of `task up`.
Open worker shells with `task connect -- w1` or `task connect -- w2` when enabled.

See the [inventory guide](../../docs/recipes.md) to save changes to this recipe.
The [expanded inventory example](../../docs/explicit-inventory.md) shows the
machines and dependencies without YAML anchors.

## Provisioning

Common packages come from `provision/base/packages.sh`. The scripts in this
directory prepare the nodes, initialize the control plane, join workers and
install Cilium and the remaining addons.

The control plane's API healthcheck allows workers to join. Cilium installation
waits for the active workers to finish their provisioning. The optional worker
is skipped when its profile is inactive.

## Access Kubernetes from the Mac

After provisioning, Labstack exports the kubeconfig and prepares its pinned
kubectl client. To export the kubeconfig again:

```sh
task kubeconfig
task kubectl -- get nodes -o wide
```

With your own kubectl:

```sh
kubectl --kubeconfig ./kubeconfig get ns
```

The file stays in the project directory and uses the API port published on
localhost, with TLS verification enabled. Your `~/.kube/config` stays unchanged.

## Practice a manual installation

For a new environment, skip the recipe's scripts and healthchecks:

```sh
task up:no-provision
task connect -- cp
```

Labstack prepares SSH access, networking and disks. Install Kubernetes yourself
inside the VMs. Add `PROFILES=extra-worker` when creating the environment if
you need the second worker.

A later `task up` runs the configured scripts on the same machines. Skipping
provisioning keeps any software already installed.

See the [usage guide](../../docs/usage.md#practice-the-installation) for guest
access, private networking and API certificate requirements.
