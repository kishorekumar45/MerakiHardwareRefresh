#!/usr/bin/env python3
"""
Post-swap Meraki switch port configuration verifier.

Purpose:
    Independently compare a new switch's live port configuration against a
    pre-swap JSON snapshot. This script does not change the switch.

Checks by default:
    - Port type
    - Data/native VLAN
    - Allowed VLANs
    - Voice VLAN
    - Tags
    - Access policy type and number

Exit codes:
    0 = PASS, no checked differences
    1 = FAIL, checked differences found
    2 = ERROR, verifier could not complete
"""

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

try:
    import truststore
except ImportError:
    raise SystemExit(
        "Missing dependency: truststore. "
        "Run: python -m pip install truststore"
    )

truststore.inject_into_ssl()

try:
    import meraki
    from dotenv import load_dotenv
except ImportError:
    meraki = None
    load_dotenv = None

CHECKED_FIELDS = (
    "type",
    "vlan",
    "allowedVlans",
    "voiceVlan",
    "tags",
    "accessPolicyType",
    "accessPolicyNumber",
)

FIELD_LABELS = {
    "type": "Port type",
    "vlan": "Data/native VLAN",
    "allowedVlans": "Allowed VLANs",
    "voiceVlan": "Voice VLAN",
    "tags": "Tags",
    "accessPolicyType": "Access policy",
    "accessPolicyNumber": "Access policy number",
}

REPORT_DIR = Path("reports")


class Style:
    """ANSI terminal styles. Disabled automatically with --no-color."""

    enabled = True
    reset = "\033[0m"
    bold = "\033[1m"
    dim = "\033[2m"
    red = "\033[31m"
    green = "\033[32m"
    yellow = "\033[33m"
    cyan = "\033[36m"

    @classmethod
    def text(cls, value: str, *styles: str) -> str:
        if not cls.enabled:
            return value
        return "".join(styles) + value + cls.reset


def timestamp() -> str:
    """Returns a timestamp suitable for report filenames."""
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def port_sort_key(port_id: str) -> Tuple[int, Any]:
    """Sorts numeric port IDs first and safely handles unusual IDs."""
    try:
        return 0, int(port_id)
    except (TypeError, ValueError):
        return 1, str(port_id)


def canonical(value: Any) -> Any:
    """Normalizes values so ordering differences do not create false alarms."""
    if isinstance(value, list):
        return sorted((canonical(item) for item in value), key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, dict):
        return {key: canonical(value[key]) for key in sorted(value)}
    if isinstance(value, str):
        return value.strip()
    return value


def normalize_tags(value: Any) -> List[str]:
    """Normalizes Meraki tags whether represented as a list or a string."""
    if value is None:
        return []
    if isinstance(value, list):
        return sorted(str(item).strip() for item in value if str(item).strip())
    if isinstance(value, str):
        # Most API responses use a list, but this also handles comma/space text.
        cleaned = value.replace(",", " ").split()
        return sorted(item.strip() for item in cleaned if item.strip())
    return [str(value)]


def normalize_field(field: str, value: Any) -> Any:
    """Applies field-specific normalization before comparison."""
    if field == "tags":
        return normalize_tags(value)
    return canonical(value)


def extract_port_map(data: Any) -> Dict[str, Dict[str, Any]]:
    """
    Accepts the common snapshot shapes used by this project:
      1. {"1": {"portId": "1", ...}, ...}
      2. [{"portId": "1", ...}, ...]
      3. {"ports": [...]}
      4. {"ports": {"1": {...}}}
    """
    if isinstance(data, dict) and "ports" in data:
        data = data["ports"]

    if isinstance(data, list):
        result = {}
        for port in data:
            if not isinstance(port, dict) or "portId" not in port:
                raise ValueError("Snapshot port list contains an entry without portId.")
            result[str(port["portId"])] = port
        return result

    if isinstance(data, dict):
        result = {}
        for key, port in data.items():
            if not isinstance(port, dict):
                raise ValueError(f"Snapshot entry for port {key!r} is not an object.")
            port_id = str(port.get("portId", key))
            result[port_id] = port
        return result

    raise ValueError("Unsupported snapshot format. Expected a port map, port list, or {'ports': ...} wrapper.")


def load_snapshot(path: Path) -> Dict[str, Dict[str, Any]]:
    """Loads and validates the pre-swap JSON snapshot."""
    if not path.exists():
        raise FileNotFoundError(f"Snapshot not found: {path}")
    with path.open("r", encoding="utf-8") as handle:
        return extract_port_map(json.load(handle))


def load_dashboard():
    """Loads MERAKI_API_KEY from the same .env file used by main.py."""
    if meraki is None or load_dotenv is None:
        raise RuntimeError("Missing dependencies. Run: pip install meraki python-dotenv")
    load_dotenv()
    api_key = os.getenv("MERAKI_API_KEY")
    if not api_key:
        raise RuntimeError("MERAKI_API_KEY is missing from .env or the environment.")
    return meraki.DashboardAPI(
        api_key,
        suppress_logging=True,
        print_console=False,
        wait_on_rate_limit=True,
        maximum_retries=4,
    )


def fetch_live_ports(dashboard, serial: str) -> Dict[str, Dict[str, Any]]:
    """Reads the new switch's current live port configuration. No writes occur."""
    ports = dashboard.switch.getDeviceSwitchPorts(serial)
    return {str(port["portId"]): port for port in ports}


def compare_ports(
    snapshot: Dict[str, Dict[str, Any]],
    live: Dict[str, Dict[str, Any]],
    fields: Iterable[str],
) -> Tuple[Dict[str, Dict[str, Dict[str, Any]]], List[str], List[str]]:
    """Returns field mismatches, missing live ports, and unexpected live ports."""
    mismatches: Dict[str, Dict[str, Dict[str, Any]]] = {}
    snapshot_ids = set(snapshot)
    live_ids = set(live)
    missing_live = sorted(snapshot_ids - live_ids, key=port_sort_key)
    extra_live = sorted(live_ids - snapshot_ids, key=port_sort_key)

    for port_id in sorted(snapshot_ids & live_ids, key=port_sort_key):
        expected_port = snapshot[port_id]
        actual_port = live[port_id]
        port_diff = {}
        for field in fields:
            expected = normalize_field(field, expected_port.get(field))
            actual = normalize_field(field, actual_port.get(field))
            if expected != actual:
                port_diff[field] = {
                    "expected": expected_port.get(field),
                    "actual": actual_port.get(field),
                }
        if port_diff:
            mismatches[port_id] = port_diff

    return mismatches, missing_live, extra_live


def display_value(value: Any) -> str:
    """Formats values compactly for terminal output."""
    if value is None:
        return "<not set>"
    if isinstance(value, (list, dict)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def print_header(serial: str, snapshot_path: Path, port_count: int) -> None:
    """Prints a compact, operator-friendly report heading."""
    line = "=" * 76
    print(Style.text(line, Style.cyan))
    print(Style.text("  POST-SWAP PORT CONFIGURATION VERIFIER", Style.bold, Style.cyan))
    print(Style.text(line, Style.cyan))
    print(f"  New switch : {Style.text(serial, Style.bold)}")
    print(f"  Snapshot   : {snapshot_path}")
    print(f"  Ports read : {port_count}")
    print(f"  Checked    : {', '.join(FIELD_LABELS[field] for field in CHECKED_FIELDS)}")
    print(Style.text(line, Style.cyan))


def print_results(
    mismatches: Dict[str, Dict[str, Dict[str, Any]]],
    missing_live: List[str],
    extra_live: List[str],
    snapshot_count: int,
    live_count: int,
) -> None:
    """Prints the quick terminal report, grouped by failed port."""
    mismatch_fields = sum(len(changes) for changes in mismatches.values())
    print()
    print(Style.text("SUMMARY", Style.bold))
    print("-" * 76)
    print(f"  Snapshot ports       : {snapshot_count}")
    print(f"  Live ports           : {live_count}")
    print(f"  Ports with mismatches: {len(mismatches)}")
    print(f"  Field mismatches     : {mismatch_fields}")
    print(f"  Missing live ports   : {len(missing_live)}")
    print(f"  Unexpected live ports: {len(extra_live)}")

    if not mismatches and not missing_live and not extra_live:
        print()
        print(Style.text("  PASS  All checked port settings match the pre-swap snapshot.", Style.bold, Style.green))
        print(Style.text("  No VLAN, tag, or access-policy discrepancies were found.", Style.green))
        return

    print()
    print(Style.text("  FAIL  Post-swap configuration differences were found.", Style.bold, Style.red))

    if missing_live:
        print()
        print(Style.text("MISSING ON NEW SWITCH", Style.bold, Style.red))
        print("  " + ", ".join(missing_live))

    if extra_live:
        print()
        print(Style.text("UNEXPECTED PORTS ON NEW SWITCH", Style.bold, Style.yellow))
        print("  " + ", ".join(extra_live))

    if mismatches:
        print()
        print(Style.text("PORTS REQUIRING ATTENTION", Style.bold, Style.red))
        print("-" * 76)
        for port_id in sorted(mismatches, key=port_sort_key):
            print(Style.text(f"  Port {port_id}", Style.bold, Style.yellow))
            for field, values in mismatches[port_id].items():
                label = FIELD_LABELS.get(field, field)
                print(f"    {label}")
                print(f"      Expected : {Style.text(display_value(values['expected']), Style.green)}")
                print(f"      Live     : {Style.text(display_value(values['actual']), Style.red)}")
            print()

    print(Style.text("ACTION: Review flagged ports in Meraki Dashboard before closing the change.", Style.bold, Style.yellow))


def save_report(
    path: Path,
    serial: str,
    snapshot_path: Path,
    mismatches: Dict[str, Dict[str, Dict[str, Any]]],
    missing_live: List[str],
    extra_live: List[str],
    snapshot_count: int,
    live_count: int,
) -> None:
    """Writes a machine-readable JSON report for the change record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    report = {
        "generatedAt": dt.datetime.now().astimezone().isoformat(),
        "targetSerial": serial,
        "snapshot": str(snapshot_path),
        "result": "PASS" if not mismatches and not missing_live and not extra_live else "FAIL",
        "checkedFields": list(CHECKED_FIELDS),
        "snapshotPortCount": snapshot_count,
        "livePortCount": live_count,
        "portsWithMismatches": len(mismatches),
        "fieldMismatchCount": sum(len(changes) for changes in mismatches.values()),
        "missingLivePorts": missing_live,
        "unexpectedLivePorts": extra_live,
        "mismatches": mismatches,
    }
    with path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, sort_keys=True)


def parse_args() -> argparse.Namespace:
    """Defines the standalone verifier command-line interface."""
    parser = argparse.ArgumentParser(
        description="Verify a new Meraki switch against a pre-swap port snapshot. Read-only."
    )
    parser.add_argument("--snapshot", required=True, type=Path, help="Pre-swap JSON snapshot or main.py source backup")
    parser.add_argument("--target", required=True, help="Serial number of the new switch")
    parser.add_argument("--report", type=Path, help="Optional JSON report path; a timestamped path is used by default")
    parser.add_argument("--no-report", action="store_true", help="Do not save a JSON report")
    parser.add_argument("--no-color", action="store_true", help="Disable ANSI terminal colors")
    return parser.parse_args()


def main() -> int:
    """Runs the read-only post-swap verification workflow."""
    args = parse_args()
    Style.enabled = not args.no_color and sys.stdout.isatty()

    try:
        snapshot = load_snapshot(args.snapshot)
        dashboard = load_dashboard()
        live = fetch_live_ports(dashboard, args.target)
        mismatches, missing_live, extra_live = compare_ports(snapshot, live, CHECKED_FIELDS)

        print_header(args.target, args.snapshot, len(live))
        print_results(mismatches, missing_live, extra_live, len(snapshot), len(live))

        if not args.no_report:
            report_path = args.report or (
                REPORT_DIR / f"{timestamp()}_{args.target}_post_swap_verify.json"
            )
            save_report(
                report_path,
                args.target,
                args.snapshot,
                mismatches,
                missing_live,
                extra_live,
                len(snapshot),
                len(live),
            )
            print()
            print(f"  JSON report: {report_path}")

        return 0 if not mismatches and not missing_live and not extra_live else 1
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(Style.text(f"ERROR: {exc}", Style.bold, Style.red), file=sys.stderr)
        return 2
    except Exception as exc:
        print(Style.text(f"ERROR: Unable to complete verification: {exc}", Style.bold, Style.red), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
