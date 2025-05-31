#!/usr/bin/env python3

import argparse
import sys

from prometheus_client import CollectorRegistry, Gauge, generate_latest
from PyNUTClient.PyNUT import PyNUTClient

# Default NUT status flags as per nut_exporter
NUT_STATUS_FLAGS = [
    "OL",
    "OB",
    "LB",
    "HB",
    "RB",
    "CHRG",
    "DISCHRG",
    "BYPASS",
    "CAL",
    "OFF",
    "OVER",
    "TRIM",
    "BOOST",
    "FSD",
    "SD",
]

registry = CollectorRegistry()
namespace = "network_ups_tools"

# Device info labels as per nut_exporter
INFO_LABELS = [
    "battery.type",
    "battery.mfr.date",

    "device.model",
    "device.mfr",
    "device.serial",
    "device.type",

    "driver.name",
    "driver.version",
    "driver.version.data",
    "driver.version.internal",
    "driver.version.usb",

    "ups.beeper.status",
    "ups.mfr",
    "ups.model",
    "ups.productid",
    "ups.vendorid",

]


def make_gauge(name, desc, labels):
    return Gauge(
        name,
        desc,
        labels,
        namespace=namespace,
        registry=registry,
    )

def parse_nut_vars(vars_raw):
    # Convert keys to str, and values to float if possible, else str (force str for some keys)
    vars = {}
    for k, v in vars_raw.items():
        key = k.decode("utf-8") if isinstance(k, bytes) else k
        if isinstance(v, bytes):
            v = v.decode("utf-8")
        if key in INFO_LABELS:
            value = str(v)
        else:
            try:
                value = float(v)
            except (ValueError, TypeError):
                value = v
        vars[key] = value
    return vars


# Metrics
metrics = {
    "device_info": make_gauge(
        "device_info",
        "UPS device information",
        [k.replace(".", "_").replace("-", "_") for k in INFO_LABELS],
    ),
    # ups.status is handled specially below
}


def collect_nut_metrics(
    host, port, login, password, ups_name=None, variables=None, statuses=None
):
    client = PyNUTClient(host=host, port=port, login=login, password=password)
    ups_list = client.GetUPSList()
    if ups_name is None:
        if len(ups_list) == 1:
            ups_name = (
                list(ups_list.keys())[0].decode("ascii")
                if isinstance(list(ups_list.keys())[0], bytes)
                else list(ups_list.keys())[0]
            )
        else:
            raise Exception("Multiple UPS devices found. Specify --ups.")

    vars_raw = client.GetUPSVars(ups_name)
    vars = parse_nut_vars(vars_raw)

    # Device info
    device_info = {
        k.replace(".", "_").replace("-", "_"): vars.get(k, "")
        for k in INFO_LABELS
    }
    metrics["device_info"].labels(**device_info).set(1)

    # Export all numeric variables as metrics
    for k, v in vars.items():
        if k in INFO_LABELS:
            continue

        metric_name = k.replace(".", "_").replace("-", "_")
        try:
            value = float(v if isinstance(v, bytes) else v)
        except Exception:
            continue

        if metric_name not in metrics:
            metrics[metric_name] = make_gauge(metric_name, f"NUT variable {k}", ["ups"])

        metrics[metric_name].labels(ups_name).set(value)

    # Special handling for ups.status
    status_flags = set(
        (
            vars.get("ups.status", "")
        ).split()
    )
    for flag in statuses or NUT_STATUS_FLAGS:
        if "ups_status" not in metrics:
            metrics["ups_status"] = make_gauge(
                "ups_status", "NUT UPS status flag", ["ups", "flag"]
            )
        metrics["ups_status"].labels(ups_name, flag).set(
            1 if flag in status_flags else 0
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="localhost", help="NUT server host")
    parser.add_argument("--port", type=int, default=3493, help="NUT server port")
    parser.add_argument("--login", default=None, help="NUT username")
    parser.add_argument("--password", default=None, help="NUT password")
    parser.add_argument(
        "--ups",
        default=None,
        help="UPS name (required if more than one UPS is present)",
    )
    parser.add_argument(
        "--vars",
        default=None,
        help="Comma-separated list of NUT variables to export (default: all numeric)",
    )
    parser.add_argument(
        "--statuses",
        default=None,
        help="Comma-separated list of status flags to always export",
    )
    args = parser.parse_args(sys.argv[1:])

    variables = args.vars.split(",") if args.vars else None
    statuses = args.statuses.split(",") if args.statuses else None
    collect_nut_metrics(
        args.host, args.port, args.login, args.password, args.ups, variables, statuses
    )
    print(generate_latest(registry).decode(), end="")


if __name__ == "__main__":
    main()
