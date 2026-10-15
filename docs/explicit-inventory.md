# Configure machines without YAML anchors

You can write every setting directly under its machine. YAML anchors (`&name`,
`*name` and `<<`) and reusable `x-*` blocks are optional. This example repeats
the shared settings so you can read and change each VM on its own.

It creates the same lab as the included inventory: one control plane (`cp`),
one worker (`w1`) and an optional second worker (`w2`). Lists use one item per
line, including the arguments of each healthcheck command.

## Complete inventory

Copy the complete example into `machines.yml` at the project root.
If a lab already exists, run `task destroy` before changing its inventory.
That command removes the existing VM disks.

```yaml
boxes_catalogue: https://cdelgehier.github.io/image-factory/

machines:
  cp:
    box: ubuntu-26.04-arm64
    ssh_user: ubuntu
    cpus: 2
    memory_mib: 2048
    disk_gib: 20
    networks:
      nat: {}
      lab:
        ipv4_address: 192.168.76.10
    ports:
      - "52222:22"
      - "16443:6443"
    scripts:
      - provision/base/packages.sh
      - provision/kubernetes/prepare.sh
      - provision/kubernetes/control-plane.sh
      - provision/kubernetes/cilium.sh
      - provision/kubernetes/addons.sh
    healthcheck:
      # The API must be ready for workers to join before Cilium is installed.
      test:
        - CMD
        - sudo
        - kubectl
        - --kubeconfig=/etc/kubernetes/admin.conf
        - get
        - --raw=/readyz
      after: provision/kubernetes/control-plane.sh
      interval: 5s
      timeout: 10s
      retries: 24
    depends_on:
      w1:
        condition: completed
        before: provision/kubernetes/cilium.sh
      w2:
        condition: completed
        before: provision/kubernetes/cilium.sh
        required: false

  w1:
    box: ubuntu-26.04-arm64
    ssh_user: ubuntu
    cpus: 1
    memory_mib: 1536
    disk_gib: 20
    networks:
      nat: {}
      lab:
        ipv4_address: 192.168.76.11
    ports:
      - "52232:22"
    scripts:
      - provision/base/packages.sh
      - provision/kubernetes/prepare.sh
      - provision/kubernetes/worker.sh
    depends_on:
      cp:
        condition: healthy
        before: provision/kubernetes/worker.sh
    healthcheck:
      test:
        - CMD
        - sudo
        - systemctl
        - is-active
        - kubelet
      interval: 5s
      timeout: 10s
      retries: 12

  w2:
    box: ubuntu-26.04-arm64
    ssh_user: ubuntu
    cpus: 1
    memory_mib: 1536
    disk_gib: 20
    profiles:
      - extra-worker
    networks:
      nat: {}
      lab:
        ipv4_address: 192.168.76.12
    ports:
      - "52242:22"
    scripts:
      - provision/base/packages.sh
      - provision/kubernetes/prepare.sh
      - provision/kubernetes/worker.sh
    depends_on:
      cp:
        condition: healthy
        before: provision/kubernetes/worker.sh
    healthcheck:
      test:
        - CMD
        - sudo
        - systemctl
        - is-active
        - kubelet
      interval: 5s
      timeout: 10s
      retries: 12

networks:
  nat:
    driver: nat
  lab:
    driver: socket
    subnet: 192.168.76.0/24
```

## Read the settings

Each key under `machines` is a VM name. `box` selects its Ubuntu image from the
catalogue, and `ssh_user` selects its guest account. CPU, memory and disk settings
apply only to that VM.
Changing `w1.memory_mib`, for example, does not change either of the other VMs.

The `networks` block at the bottom defines the shared networks. Each machine
attaches to them by name. `nat: {}` means that the attachment needs no additional
settings: the guest receives an address through DHCP. The `lab` attachment gives
each VM its own private address for communication between guests.

Each `ports` item maps a port on the Mac to a port in that VM.
For example, `"52222:22"` exposes the control plane's SSH port on localhost port
52222. `"16443:6443"` exposes its Kubernetes API on localhost port 16443.

The `scripts` list defines what is installed. Scripts run in the listed order
within each VM. `depends_on` lets a script wait for another machine:

1. The control plane initializes Kubernetes and its API healthcheck passes.
2. The workers can then run their join script. Their healthcheck verifies that
   the kubelet service is active.
3. The control plane waits for each active worker to finish before installing
   Cilium and the remaining addons.

The worker healthcheck checks the service rather than Kubernetes node readiness,
because nodes need Cilium before they can become Ready. The control plane's
dependency on `w2` is optional because that worker only starts with its profile.

See the [dependency diagram](configuration.md#dependency-flow) to follow these
checkpoints and the optional worker's effect on Cilium installation.

## Start the lab

From the project directory, start the control plane and one worker:

```sh
task up
task connect -- cp
```

For a new lab with both workers:

```sh
task up PROFILES=extra-worker
```

To practice installing Kubernetes yourself, use `task up:no-provision` for a new
lab. It skips scripts and healthchecks while preparing the VMs and their network.

See the [configuration reference](configuration.md) for all supported fields,
image version selection and dependency conditions.
