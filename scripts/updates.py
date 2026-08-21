#!/usr/bin/env python3
"""node_exporter textfile collector for pending OS updates.

This runs in a privileged container next to node_exporter, so it reads the
*host* package state by entering PID 1's mount namespace with nsenter(1). The
container's own apt database describes the image, not the node, which is why
the upstream apt_info.py cannot be used here (it also needs apt_pkg, which is
not installed).

The host is only ever simulated, never mutated:

    nsenter -t 1 -m -- apt-get -s -o Debug::NoLocking=1 dist-upgrade

``-s`` keeps it a simulation and ``-o Debug::NoLocking=1`` stops apt from
taking the host lock, so this cannot collide with unattended-upgrades.

``dist-upgrade`` rather than plain ``upgrade``: plain ``upgrade`` silently
omits every package that would need another package installed or removed,
which hides kernel ABI bumps (``linux-image-*``) - exactly the updates most
worth alerting on. Only one simulate is run per refresh, because on the arm64
nodes it is the slowest thing in the collector loop.

Metrics emitted::

    node_apt_upgrades_pending
    node_apt_security_upgrades_pending
    node_reboot_required
    node_apt_package_cache_timestamp_seconds

The counts are only as fresh as the host's last ``apt update``; the cache
timestamp gauge is what tells you a node has gone quietly stale and is
reporting 0 pending for the wrong reason.

Output contract: the entrypoint pipes stdout through sponge(1) into the .prom
file, so the whole exposition is built in memory and printed exactly once, or
nothing at all is printed - a truncated exposition would break the parse for
the entire file. All diagnostics go to stderr. Any failure to read the host
(no nsenter, no apt-get, a non-zero simulate, unparseable output) prints
nothing and exits 0.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from prometheus_client import CollectorRegistry, Gauge, generate_latest

NAMESPACE = "node"

DEFAULT_CACHE_FILE = "/tmp/updates-collector-cache.json"
# INTERVAL in the deployment is 60s, but an apt simulate is not free on the
# arm64 nodes. Recompute at most this often and re-print cached values
# otherwise. /tmp is a tmpfs emptyDir, so a pod restart correctly recomputes.
DEFAULT_CACHE_TTL = 15 * 60
# Retry sooner than a success refresh, but not every INTERVAL: a timeout burns
# the whole CPU quota, so a node that cannot answer must not be asked every cycle.
DEFAULT_FAILURE_TTL = 5 * 60
# Generous on purpose. The simulate takes ~18s on an arm64 node given a full
# core, but the sidecar's CPU limit is 300m, and under that quota (with the
# sibling collectors competing) it was measured at 78-108s. 60s was not enough.
DEFAULT_TIMEOUT = 300
DEFAULT_TARGET_PID = 1

# Force a predictable locale in the host namespace so the summary line below
# stays parseable, and give apt-get a *host* PATH to be found on - nsenter
# itself is a container binary and is resolved separately.
HOST_ENV = {
    "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
    "LC_ALL": "C",
    "LANG": "C",
    "DEBIAN_FRONTEND": "noninteractive",
}

# Inst libexpat1 [2.7.1-2] (2.8.2-1~deb13u1 Debian-Security:13/stable-security [arm64])
# Inst somepkg (1.2.3 Debian:13/stable, Debian-Security:13/stable-security [amd64]) []
# The bracketed group is the currently installed version and is absent for
# packages dist-upgrade has to pull in fresh. Conf lines duplicate Inst lines.
INST_RE = re.compile(r"^Inst\s+(?P<package>\S+)\s+(?:\[[^\]]*\]\s+)?\((?P<detail>[^)]*)\)")

# "12 upgraded, 0 newly installed, 0 to remove and 3 not upgraded."
SUMMARY_RE = re.compile(
    r"^\d+ upgraded, \d+ newly installed, \d+ (?:to remove|reinstalled)", re.MULTILINE
)

# Origins are matched on a "Security" substring rather than on a suite name:
# these hosts carry DietPi and Armbian repos next to Debian's, so hardcoding
# Debian-Security would undercount.
SECURITY_ORIGIN_MARKER = "security"

# Read once in the host mount namespace: which OS we actually measured, whether
# a reboot is pending, and how old apt's metadata is.
HOST_PROBE = r"""
printf 'pretty_name=%s\n' "$(. /etc/os-release 2>/dev/null; printf '%s' "$PRETTY_NAME")"
if [ -e /run/reboot-required ] || [ -e /var/run/reboot-required ]; then
    echo reboot_required=1
else
    echo reboot_required=0
fi
for stamp in /var/lib/apt/periodic/update-success-stamp /var/lib/apt/lists; do
    if [ -e "$stamp" ]; then
        mtime=$(stat -c %Y "$stamp" 2>/dev/null) || continue
        [ -n "$mtime" ] || continue
        echo "cache_timestamp=$mtime"
        break
    fi
done
"""


class HostUnreachable(Exception):
    """The host's package state could not be read. Emit nothing, exit 0."""


def log(message):
    """Diagnostics must never touch stdout; stdout is the exposition."""
    print("updates.py: %s" % message, file=sys.stderr)


def parse_apt_upgrade_output(output):
    """Count packages apt would install/upgrade, and how many come from security.

    Args:
        output: (str) stdout of `apt-get -s ... dist-upgrade`.

    Returns:
        (total, security) package counts, deduplicated by package name.
    """
    packages = {}
    for line in output.splitlines():
        match = INST_RE.match(line.strip())
        if match is None:
            continue
        package = match.group("package")
        is_security = any(
            SECURITY_ORIGIN_MARKER in origin.lower()
            for origin in _origins(match.group("detail"))
        )
        # A package listed twice counts once, and counts as security if any of
        # its candidate origins is one.
        packages[package] = packages.get(package, False) or is_security
    return len(packages), sum(1 for is_security in packages.values() if is_security)


def _origins(detail):
    """Pull the origin fields out of an Inst line's parenthesised detail.

    `2.8.2-1~deb13u1 Debian-Security:13/stable-security [arm64]` is a new
    version, a comma separated origin list, then the architecture.
    """
    detail = re.sub(r"\s*\[[^\]]*\]\s*$", "", detail.strip())
    parts = detail.split(None, 1)
    if len(parts) < 2:
        return []
    return [origin.strip() for origin in parts[1].split(",") if origin.strip()]


def parse_host_probe(output):
    """Parse the key=value output of HOST_PROBE into a dict."""
    probe = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            probe[key.strip()] = value.strip()
    return probe


def run_in_host_ns(args, target_pid, timeout):
    """Run a command in PID `target_pid`'s mount namespace.

    Returns:
        subprocess.CompletedProcess

    Raises:
        HostUnreachable: if nsenter itself could not be run or timed out.
    """
    nsenter = shutil.which("nsenter")
    if nsenter is None:
        raise HostUnreachable("nsenter not found; cannot read the host namespace")
    command = [nsenter, "-t", str(target_pid), "-m", "--"] + args
    try:
        return subprocess.run(
            command,
            # No stdin: a simulate should never prompt, but if apt ever does it
            # must hit EOF and fail rather than stall the collector loop.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=HOST_ENV,
            timeout=timeout,
            check=False,
            universal_newlines=True,
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        raise HostUnreachable(
            "%s timed out after %ds in the host namespace" % (args[0], timeout)
        )
    except OSError as err:
        raise HostUnreachable("failed to run nsenter: %s" % err)


def collect(target_pid, timeout, apt_mode):
    """Gather the values from the host. Raises HostUnreachable on any failure."""
    probe = run_in_host_ns(["sh", "-c", HOST_PROBE], target_pid, timeout)
    if probe.returncode != 0:
        raise HostUnreachable(
            "host probe failed (rc=%d): %s"
            % (probe.returncode, probe.stderr.strip() or "no stderr")
        )
    probe_values = parse_host_probe(probe.stdout)

    apt = run_in_host_ns(
        ["apt-get", "-s", "-o", "Debug::NoLocking=1", apt_mode], target_pid, timeout
    )
    if apt.returncode != 0:
        raise HostUnreachable(
            "apt-get -s %s failed on the host (rc=%d): %s"
            % (apt_mode, apt.returncode, apt.stderr.strip().replace("\n", " ") or "no stderr")
        )

    total, security = parse_apt_upgrade_output(apt.stdout)
    # Distinguish a genuine "nothing pending" from output we did not understand:
    # apt always prints the summary line, so no Inst lines and no summary means
    # this was not apt output we can trust.
    if total == 0 and SUMMARY_RE.search(apt.stdout) is None:
        raise HostUnreachable(
            "could not parse apt-get -s %s output (no Inst lines, no summary line)"
            % apt_mode
        )

    values = {
        "upgrades_pending": total,
        "security_upgrades_pending": security,
        "reboot_required": 1 if probe_values.get("reboot_required") == "1" else 0,
    }
    try:
        values["package_cache_timestamp"] = float(probe_values["cache_timestamp"])
    except (KeyError, ValueError):
        # Not fatal: the counts are still valid, we just cannot say how fresh
        # apt's metadata is.
        log("no apt metadata timestamp available on the host")

    log(
        "host %s: %d pending (%d security), reboot_required=%d"
        % (
            probe_values.get("pretty_name") or "unknown",
            values["upgrades_pending"],
            values["security_upgrades_pending"],
            values["reboot_required"],
        )
    )
    return values


def render(values):
    """Build the complete exposition for `values` as a single string."""
    registry = CollectorRegistry()

    def gauge(name, documentation):
        return Gauge(name, documentation, namespace=NAMESPACE, registry=registry)

    gauge("apt_upgrades_pending", "Apt packages pending upgrade.").set(
        values["upgrades_pending"]
    )
    gauge(
        "apt_security_upgrades_pending",
        "Apt packages pending upgrade from a security origin.",
    ).set(values["security_upgrades_pending"])
    gauge(
        "reboot_required", "Node reboot is required for software updates."
    ).set(values["reboot_required"])
    if "package_cache_timestamp" in values:
        gauge(
            "apt_package_cache_timestamp_seconds", "Apt update last run time."
        ).set(values["package_cache_timestamp"])

    return generate_latest(registry).decode()


def read_cache(path, success_ttl, failure_ttl):
    """Return the last cached attempt if it is still fresh, else None.

    The throttle applies to *attempts*, not just successes: a failed attempt is
    remembered too, so a host that cannot answer is not re-asked on every
    60s cycle. A timeout costs the whole CPU quota for its full duration, so
    retrying that often would starve the sibling collectors.

    Returns:
        {"values": dict|None, "reason": str, "age": float} or None to recompute.
    """
    try:
        with open(path) as handle:
            cache = json.load(handle)
        age = time.time() - float(cache["timestamp"])
        values = cache["values"]
    except FileNotFoundError:
        # Normal on a cold start: /tmp is a tmpfs that resets with the pod.
        log("no cache at %s yet, computing" % path)
        return None
    except (OSError, ValueError, KeyError, TypeError) as err:
        log("unusable cache at %s (%s), recomputing" % (path, err))
        return None

    ttl = success_ttl if values is not None else failure_ttl
    if age < 0 or age >= ttl:
        log("cached attempt is %ds old (ttl %ds), refreshing" % (age, ttl))
        return None
    return {"values": values, "reason": cache.get("reason") or "", "age": age}


def write_cache(path, values, reason=""):
    """Atomically replace the cache file. A failure here is never fatal.

    `values` is None to record a failed attempt, with `reason` for the log.
    """
    temp_path = None
    try:
        directory = os.path.dirname(path) or "."
        with tempfile.NamedTemporaryFile(
            "w", dir=directory, prefix=".updates-cache.", delete=False
        ) as handle:
            temp_path = handle.name
            json.dump(
                {"timestamp": time.time(), "values": values, "reason": reason},
                handle,
            )
        os.replace(temp_path, path)
    except Exception as err:  # noqa: BLE001 - a traceback here would be output
        log("could not write cache to %s: %s" % (path, err))
        if temp_path is not None:
            # Do not leave debris behind on every cycle; /tmp is a small tmpfs.
            try:
                os.unlink(temp_path)
            except OSError:
                pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--target-pid",
        type=int,
        default=DEFAULT_TARGET_PID,
        help="PID whose mount namespace holds the host filesystem",
    )
    parser.add_argument(
        "--apt-mode",
        default="dist-upgrade",
        choices=["dist-upgrade", "full-upgrade", "upgrade"],
        help="apt-get operation to simulate (default: dist-upgrade)",
    )
    parser.add_argument(
        "--cache-file", default=DEFAULT_CACHE_FILE, help="where to cache computed values"
    )
    parser.add_argument(
        "--cache-ttl",
        type=int,
        default=DEFAULT_CACHE_TTL,
        help="seconds before cached values are recomputed (0 disables caching)",
    )
    parser.add_argument(
        "--failure-ttl",
        type=int,
        default=DEFAULT_FAILURE_TTL,
        help="seconds before retrying after a failed attempt",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="seconds to allow each host namespace command",
    )
    # The entrypoint passes the same "$@" to every script in SCRIPTS, so flags
    # meant for a sibling collector must not turn into an argparse exit here.
    args, unknown = parser.parse_known_args(sys.argv[1:])
    if unknown:
        log("ignoring arguments meant for another collector: %s" % " ".join(unknown))

    caching = args.cache_ttl > 0
    cached = (
        read_cache(args.cache_file, args.cache_ttl, args.failure_ttl)
        if caching
        else None
    )

    if cached is not None:
        values = cached["values"]
        if values is None:
            # The last attempt failed recently; do not pay for another one yet.
            log(
                "last attempt %ds ago failed (%s); not retrying for %ds; "
                "emitting no metrics"
                % (cached["age"], cached["reason"] or "no reason recorded",
                   args.failure_ttl)
            )
            return 0
        log("serving cached values (%ds old)" % cached["age"])
    else:
        try:
            values = collect(args.target_pid, args.timeout, args.apt_mode)
        except HostUnreachable as err:
            # Emitting nothing yields an empty .prom, which is valid and simply
            # produces no metrics. Never emit a partial exposition.
            log("%s; emitting no metrics" % err)
            if caching:
                write_cache(args.cache_file, None, reason=str(err))
            return 0
        except Exception as err:  # noqa: BLE001 - a traceback here would be output
            reason = "unexpected failure (%s: %s)" % (type(err).__name__, err)
            log("%s; emitting no metrics" % reason)
            if caching:
                write_cache(args.cache_file, None, reason=reason)
            return 0
        if caching:
            write_cache(args.cache_file, values)

    try:
        exposition = render(values)
    except Exception as err:  # noqa: BLE001
        log("could not render metrics (%s: %s); emitting no metrics" % (type(err).__name__, err))
        return 0
    print(exposition, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
