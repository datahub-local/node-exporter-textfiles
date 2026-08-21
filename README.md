# node-exporter-textfiles

**IMPORTANT**: This repo is based on [galexrt/container-node_exporter-textfiles](https://github.com/galexrt/container-node_exporter-textfiles)

Container Image for easily running textfile exporter scripts from the Prometheus Community to be collected by the prometheus/node_exporter.

Container Image available from:

* [GHCR.io](https://github.com/users/datahub-local/packages/container/package/node-exporter-textfiles)

## Credits

This docker image contains the [prometheus-community/node-exporter-textfile-collector-scripts](https://github.com/prometheus-community/node-exporter-textfile-collector-scripts) repository, so that any script can be easily used with / from this Docker image.

## Usage

**This Docker image needs to be run in privileged mode for most of the scripts in the `prometheus-community/node-exporter-textfile-collector-scripts` repository, e.g., for `smartmon.sh` it is needed to be able to collect the SMART values.**

The entrypoint script is putting the output into the directory `/var/lib/node_exporter`, by default filename named after which script is running.

### Variables

| Name          | Default                  | Description                                                                       |
|---------------|--------------------------|-----------------------------------------------------------------------------------|
| `SCRIPTS`     | `smartmon.py`            | Comma-separated list of textfile collector scripts to run.                        |
| `SCRIPT`      | -                        | Deprecated alias for `SCRIPTS`, used only when `SCRIPTS` is unset.                |
| `OUTPUT_PATH` | `/var/lib/node_exporter` | Directory of the output files.                                                    |
| `INTERVAL`    | `300`                    | Interval at which the whole list of scripts is run.                               |
| `DEBUG`       | -                        | If set, the entrypoint runs with `set -ex`.                                       |

Each script writes to `${OUTPUT_PATH}/<script name without extension>.prom`, so
`SCRIPTS="nutmon.py,smartmon.py,updates.py"` produces `nutmon.prom`, `smartmon.prom` and
`updates.prom`.

Any flags / args given to the container are passed to *every* script in `SCRIPTS`.

## Custom scripts

Alongside the upstream collectors, this image ships:

| Script       | Metrics prefix                | Description                                                    |
|--------------|-------------------------------|----------------------------------------------------------------|
| `smartmon.py`| `smartmon_`                   | SMART values via `smartctl`, including NVMe and USB bridges.   |
| `nutmon.py`  | `network_ups_tools_`          | UPS values from a NUT server.                                  |
| `updates.py` | `node_apt_`, `node_reboot_`   | Pending OS updates on the **host**.                            |

### `updates.py`

Reports how far behind the node's packages are:

```
node_apt_upgrades_pending                   # packages apt would install or upgrade
node_apt_security_upgrades_pending          # of those, packages from a security origin
node_reboot_required                        # /run/reboot-required exists on the host
node_apt_package_cache_timestamp_seconds    # how fresh the host's apt metadata is
```

The container's own apt database describes the *image*, not the node, so this script reads
the host by entering PID 1's mount namespace with `nsenter`. It therefore needs
`hostPID: true` and `privileged: true`; without them it logs why and emits no metrics.

The host is only ever simulated, never modified:

```console
nsenter -t 1 -m -- apt-get -s -o Debug::NoLocking=1 dist-upgrade
```

`Debug::NoLocking=1` avoids taking the host apt lock, so this cannot collide with
`unattended-upgrades`. `dist-upgrade` is used rather than plain `upgrade` because plain
`upgrade` omits any package that needs another package installed or removed, which hides
kernel ABI bumps (`linux-image-*`).

Because an apt simulate is far slower than the other collectors, results are cached in
`/tmp` and recomputed at most every 15 minutes; in between, the cached values are
re-printed. Mount `/tmp` as an `emptyDir` so a pod restart recomputes from cold.

The throttle covers *attempts*, not just successes: a failed attempt is remembered too and
retried after `--failure-ttl` rather than on the next `INTERVAL` tick. This matters because
a timeout costs the container's whole CPU quota for its full duration, so a host that
cannot answer must not be asked every cycle.

Mind the sidecar's CPU limit when sizing `--timeout`. Measured on an arm64 Orange Pi node:
the simulate takes **~18s** given a full core, but **78-108s** under a `300m` CPU limit with
the sibling collectors competing for it. Hence the 300s default - a 60s timeout is not
survivable there.

Memory is less of a concern than it first looks. The simulate's own peak RSS is ~110 MiB,
but most of that is the file-backed apt cache mmap, which is reclaimable: running all three
scripts under a `196Mi` limit held at ~87 MiB working set with no OOM kill and no restarts.

`node_apt_package_cache_timestamp_seconds` is worth alerting on: the counts are only as
fresh as the host's last `apt update`, and a node whose apt metadata has gone stale
otherwise reports `0 pending` for the wrong reason.

On a host where the update state cannot be read at all - no `nsenter`, no `apt-get`, a
failing simulate, unparseable output - the script prints nothing and exits 0, leaving an
empty `.prom`, and explains itself on stderr (`kubectl logs`). This is the expected
outcome on appliance-style nodes such as TrueNAS.

| Flag             | Default                             | Description                                     |
|------------------|-------------------------------------|-------------------------------------------------|
| `--target-pid`   | `1`                                 | PID whose mount namespace holds the host.       |
| `--apt-mode`     | `dist-upgrade`                      | `dist-upgrade`, `full-upgrade` or `upgrade`.    |
| `--cache-file`   | `/tmp/updates-collector-cache.json` | Where computed values are cached.               |
| `--cache-ttl`    | `900`                               | Seconds before recomputing; `0` disables.       |
| `--failure-ttl`  | `300`                               | Seconds before retrying a failed attempt.       |
| `--timeout`      | `300`                               | Seconds allowed per host namespace command.     |