import argparse
import datetime as dt
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

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

truststore.inject_into_ssl()

PORT_FIELD_MAP = {
    "name": "name",
    "tags": "tags",
    "enabled": "enabled",
    "profile": "profile",
    "linkNegotiation": "linkNegotiation",
    "poeEnabled": "poeEnabled",
    "vlan": "vlan",
    "allowedVlans": "allowedVlans",
    "portScheduleId": "portScheduleId",
    "type": "type",
    "accessPolicyType": "accessPolicyType",
    "accessPolicyNumber": "accessPolicyNumber",
    "voiceVlan": "voiceVlan",
    "adaptivePolicyGroupId": "adaptivePolicyGroupId",
    "rstpEnabled": "rstpEnabled",
    "stpGuard": "stpGuard",
    "stpPortFastTrunk": "stpPortFastTrunk",
    "peerSgtCapable": "peerSgtCapable",
    "stormControlEnabled": "stormControlEnabled",
    "isolationEnabled": "isolationEnabled",
    "daiTrusted": "daiTrusted",
    "udld": "udld",
    "macAllowList": "macAllowList",
    "macWhitelistLimit": "macWhitelistLimit",
    "stickyMacAllowList": "stickyMacAllowList",
    "stickyMacAllowListLimit": "stickyMacAllowListLimit",
}

READ_ONLY_OR_UNSUPPORTED = [
    "mirror",
    "schedule",
    "adaptivePolicyGroup",
    "linkNegotiationCapabilities",
    "module",
    "portId",
]

CLEARABLE_FIELDS = {
    "name",
    "tags",
    "voiceVlan",
    "allowedVlans",
    "accessPolicyNumber",
    "portScheduleId",
    "adaptivePolicyGroupId",
    "macAllowList",
    "macWhitelistLimit",
    "stickyMacAllowList",
    "stickyMacAllowListLimit",
}


TRUNK_ONLY_FIELDS = {
    "allowedVlans",
    "peerSgtCapable",
}

ACCESS_ONLY_FIELDS = {
    "voiceVlan",
    "accessPolicyType",
    "accessPolicyNumber",
    "macAllowList",
    "macWhitelistLimit",
    "stickyMacAllowList",
    "stickyMacAllowListLimit",
}

ACCESS_POLICY_DEPENDENT = {
    "accessPolicyNumber": "Custom access policy",
    "macAllowList": "MAC allow list",
    "macWhitelistLimit": "MAC allow list",
    "stickyMacAllowList": "Sticky MAC allow list",
    "stickyMacAllowListLimit": "Sticky MAC allow list",
}

RETRYABLE_MARKERS = ("429", "500", "502", "503", "504", "rate limit", "timed out", "timeout")
MAX_RETRIES = 4
BASE_BACKOFF_SECONDS = 1.0

BACKUP_DIR = Path("backups")
REPORT_DIR = Path("reports")

PortMap = Dict[str, Dict[str, Any]]
DiffMap = Dict[str, Dict[str, Dict[str, Any]]]


'''
Creates a timestamp string.
Used for naming backup and report files so they do not overwrite each other.
'''
def now_stamp() -> str:
    return dt.datetime.now().strftime("%Y%m%d_%H%M%S")


'''
Creates the local folders used by the script:
    backups/ and reports/
'''
def ensure_dirs() -> None:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)


'''
Sorts port IDs in numeric order when possible.
Falls back to plain string sorting if any port ID is not a simple number
(for example stacked or module ports like "1_1"), so the script never crashes
on an unusual port layout.
'''
def sort_port_ids(ids) -> List[str]:
    ids = list(ids)
    try:
        return sorted(ids, key=lambda pid: int(pid))
    except (ValueError, TypeError):
        return sorted(ids, key=str)


'''
Loads the Meraki API key from the .env file and creates the Meraki Dashboard API connection.
It checks:
    whether required packages are installed
    whether MERAKI_API_KEY exists
    then returns a ready-to-use dashboard object
wait_on_rate_limit and maximum_retries let the Meraki SDK ride out rate limits
and transient errors on its own; our _with_retry adds a second safety net.
This is what lets the script talk to the Meraki cloud.
'''
def load_dashboard():
    if load_dotenv is None or meraki is None:
        raise SystemExit("Missing dependencies. Run: pip install -r requirements.txt")
    load_dotenv()
    api_key = os.getenv("MERAKI_API_KEY")
    if not api_key:
        raise SystemExit("MERAKI_API_KEY missing. Create a .env file with MERAKI_API_KEY=your_key")
    return meraki.DashboardAPI(
        api_key,
        suppress_logging=True,
        print_console=False,
        wait_on_rate_limit=True,
        maximum_retries=MAX_RETRIES,
    )


'''
Decides whether an API error is worth retrying.
Returns True for transient problems (rate limits, timeouts, 5xx server errors)
and False for real errors like a bad value or a missing switch.
'''
def _is_retryable(exc: Exception) -> bool:
    text = str(exc).lower()
    return any(marker in text for marker in RETRYABLE_MARKERS)


'''
Runs any Meraki API call and retries it with exponential backoff if it hits a
transient error. A single rate-limit blip on one port no longer aborts the
whole migration. Non-transient errors are raised immediately.
'''
def _with_retry(func, *args, **kwargs):
    attempt = 0
    while True:
        try:
            return func(*args, **kwargs)
        except Exception as exc:
            attempt += 1
            if attempt > MAX_RETRIES or not _is_retryable(exc):
                raise
            delay = BASE_BACKOFF_SECONDS * (2 ** (attempt - 1))
            print(f"[RETRY] transient error ({exc}); retry {attempt}/{MAX_RETRIES} in {delay:.1f}s")
            time.sleep(delay)


'''
Standardizes values before comparing them.
Sorts lists and dictionary keys so that two configs that mean the same thing
(for example tags in a different order) are treated as equal.
'''
def canonical(value: Any) -> Any:
    if isinstance(value, list):
        return sorted(value, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, dict):
        return {key: canonical(value[key]) for key in sorted(value)}
    return value


'''
Saves data to a JSON file.
Used for:
    backups
    diff reports
    change plans
    failure logs
It writes the file in a clean, readable format.
'''
def save_json(path: Path, data: Any) -> None:
    with open(path, "w", encoding="utf-8") as file_handle:
        json.dump(data, file_handle, indent=2, sort_keys=True)


'''
Gets all switch port configurations from Meraki for one switch.
Wrapped in _with_retry so a transient read error is retried automatically.
'''
def get_ports(dashboard, serial: str) -> PortMap:
    ports = _with_retry(dashboard.switch.getDeviceSwitchPorts, serial)
    return {str(port["portId"]): port for port in ports}


'''
Gets basic device information from Meraki Dashboard for a switch serial number.
Used to read the model for model validation.
'''
def get_device_info(dashboard, serial: str) -> Dict[str, Any]:
    try:
        return _with_retry(dashboard.devices.getDevice, serial)
    except Exception as exc:
        raise SystemExit(f"Unable to read device metadata for {serial}: {exc}")


'''
Takes one raw Meraki port config and keeps only the fields we care about migrating.
It removes extra fields that Meraki returns but we do not want to push back.
'''
def normalize_port(port: Dict[str, Any], include_nulls: bool = True) -> Dict[str, Any]:
    normalized = {}
    for get_field, out_field in PORT_FIELD_MAP.items():
        if get_field in port:
            value = canonical(port.get(get_field))
            if value is not None or include_nulls:
                normalized[out_field] = value
    return normalized


'''
Runs normalize_port() on every port in the switch.
'''
def normalize_ports(ports: PortMap, include_nulls: bool = True) -> PortMap:
    return {port_id: normalize_port(data, include_nulls) for port_id, data in ports.items()}


'''
Creates a backup of a switch's current raw port configuration.
Used before applying changes to the target switch.
'''
def backup_switch(serial: str, raw_ports: PortMap, label: str) -> Path:
    path = BACKUP_DIR / f"{now_stamp()}_{serial}_{label}.json"
    save_json(path, raw_ports)
    print(f"[BACKUP] {path}")
    return path


'''
Checks that the source and target serial numbers are not the same.
This prevents accidentally copying a switch config onto itself.
'''
def validate_serials(source: str, target: str) -> None:
    if source == target:
        raise SystemExit("Source and target serials are the same. Aborting.")


'''
Checks that the source and target switches have matching port IDs.
This protects against accidentally migrating old switch (MS120-48LP) to something that does not have the same port layout.
If ports do not match, the script stops before making changes.
'''
def validate_ports(source_ports: PortMap, target_ports: PortMap) -> None:
    source_ids = set(source_ports)
    target_ids = set(target_ports)
    if source_ids != target_ids:
        missing_on_target = sort_port_ids(source_ids - target_ids)
        extra_on_target = sort_port_ids(target_ids - source_ids)
        raise SystemExit(
            "Port ID mismatch.\n"
            f"Missing on target: {missing_on_target}\n"
            f"Extra on target: {extra_on_target}"
        )


'''
Checks that both switches have the expected number of ports and matching port IDs.
'''
def validate_expected_port_count(
    source_ports: PortMap,
    target_ports: PortMap,
    expected_count: int,
) -> None:
    expected_ids = {str(port_id) for port_id in range(1, expected_count + 1)}
    for label, ports in (("source", source_ports), ("target", target_ports)):
        actual_ids = set(ports)
        if len(ports) != expected_count or actual_ids != expected_ids:
            missing = sort_port_ids(expected_ids - actual_ids)
            extra = sort_port_ids(actual_ids - expected_ids)
            raise SystemExit(
                f"Expected {label} switch to have ports 1-{expected_count}.\n"
                f"Actual count: {len(ports)}\n"
                f"Missing: {missing}\n"
                f"Extra: {extra}"
            )


'''
Checks that the source and target switches are the expected models.
This helps prevent running the migration against the wrong switch model.
'''
def validate_models(
    dashboard,
    source_serial: str,
    target_serial: str,
    expected_source_model: str,
    expected_target_model: str,
    skip_model_check: bool,
) -> None:
    if skip_model_check:
        print("[INFO] Model validation skipped by user.")
        return
    source_info = get_device_info(dashboard, source_serial)
    target_info = get_device_info(dashboard, target_serial)
    source_model = str(source_info.get("model", ""))
    target_model = str(target_info.get("model", ""))
    print(f"[MODEL] Source {source_serial}: {source_model}")
    print(f"[MODEL] Target {target_serial}: {target_model}")
    if expected_source_model not in source_model:
        raise SystemExit(
            f"Source model validation failed. Expected '{expected_source_model}' in model value, got '{source_model}'."
        )
    if expected_target_model not in target_model:
        raise SystemExit(
            f"Target model validation failed. Expected '{expected_target_model}' in model value, got '{target_model}'."
        )


'''
Decides if a single field should be dropped from a port's payload because it
does not apply to that port's type.
Returns a short human-readable reason string if the field is incompatible,
or None if the field is fine to push.
Rules come from the Meraki updateDeviceSwitchPort schema:
    trunk-only fields on an access port -> drop
    access-only fields on a trunk port -> drop
    access-policy fields when accessPolicyType does not match -> drop
'''
def _incompatible_reason(field: str, port_type: Any, access_policy_type: Any) -> Any:
    if port_type == "access" and field in TRUNK_ONLY_FIELDS:
        return "trunk-only field on an access port"
    if port_type == "trunk" and field in ACCESS_ONLY_FIELDS:
        return "access-only field on a trunk port"
    required_apt = ACCESS_POLICY_DEPENDENT.get(field)
    if required_apt is not None and access_policy_type != required_apt:
        return f"requires accessPolicyType '{required_apt}' (port has '{access_policy_type}')"
    return None


'''
Filters one port's payload so only fields valid for that port type are kept.
Returns:
    conditioned -> the payload that is safe to push
    dropped     -> field -> reason for every field that was removed
This is what stops the target MS130 from rejecting type-mismatched fields.
'''
def condition_payload(
    payload: Dict[str, Any],
    port_type: Any,
    access_policy_type: Any,
) -> Tuple[Dict[str, Any], Dict[str, str]]:
    conditioned: Dict[str, Any] = {}
    dropped: Dict[str, str] = {}
    for field, value in payload.items():
        reason = _incompatible_reason(field, port_type, access_policy_type)
        if reason is None:
            conditioned[field] = value
        else:
            dropped[field] = reason
    return conditioned, dropped


'''
Compares the normalized source switch config against the normalized target switch config.
It checks every migrated field on every port.
This is used for human-readable reporting only, so it shows ALL differences
(including fields that the change plan will intentionally skip).
'''
def generate_diff(source_ports: PortMap, target_ports: PortMap) -> DiffMap:
    diffs: DiffMap = {}
    for port_id in sort_port_ids(source_ports):
        src = source_ports[port_id]
        dst = target_ports.get(port_id, {})
        keys = sorted(set(src) | set(dst))
        port_diff = {}
        for field in keys:
            src_val = src.get(field)
            dst_val = dst.get(field)
            if canonical(src_val) != canonical(dst_val):
                port_diff[field] = {"source": src_val, "target": dst_val}
        if port_diff:
            diffs[port_id] = port_diff
    return diffs


'''
Counts the total number of field-level differences found in the diff report.
'''
def count_field_differences(diffs: DiffMap) -> int:
    return sum(len(changes) for changes in diffs.values())


'''
Prints the differences in a readable format.
'''
def print_diff_report(diffs: DiffMap) -> None:
    if not diffs:
        print("\nPASS: No differences found")
        return
    for port_id, changes in diffs.items():
        print("\n" + "=" * 70)
        print(f"PORT {port_id}")
        print("=" * 70)
        for field, values in changes.items():
            print(f"{field}")
            print(f"  SOURCE: {values['source']}")
            print(f"  TARGET: {values['target']}")
    print("\nSummary")
    print("-" * 70)
    print(f"Ports requiring updates: {len(diffs)}")
    print(f"Field differences: {count_field_differences(diffs)}")


'''
Builds the exact payloads that will be pushed to the target switch.
It only includes fields that are different, and then runs each payload through
condition_payload() so type-incompatible fields (for example allowedVlans on an
access port) are removed before they are ever sent to Meraki.
    clear_nulls   -> allow selected null source values to clear the target
    dropped_out   -> optional dict; if given, it is filled with the fields that
                     were dropped per port for reporting (verify passes None so
                     it stays silent).
This is the single source of truth for "what the tool will actually write",
and run_verify reuses it so a skipped field can never look like a failure.
'''
def build_change_plan(
    source_ports: PortMap,
    target_ports: PortMap,
    clear_nulls: bool,
    dropped_out: Dict[str, Dict[str, str]] = None,
) -> PortMap:
    changes: PortMap = {}
    for port_id in sort_port_ids(source_ports):
        payload = {}
        for field, src_val in source_ports[port_id].items():
            dst_val = target_ports.get(port_id, {}).get(field)
            if canonical(src_val) == canonical(dst_val):
                continue
            if src_val is None and not (clear_nulls and field in CLEARABLE_FIELDS):
                continue
            payload[field] = src_val
        # Drop fields that do not apply to this port's type.
        port_type = source_ports[port_id].get("type")
        access_policy_type = source_ports[port_id].get("accessPolicyType")
        payload, dropped = condition_payload(payload, port_type, access_policy_type)
        if dropped and dropped_out is not None:
            dropped_out[port_id] = dropped
        if payload:
            changes[port_id] = payload
    return changes


'''
Pushes the change plan to the target switch (MS130-48X) using the Meraki API.
It updates each port one by one, retrying transient errors automatically.
Returns:
    applied  -> list of port IDs that were successfully changed
    failures -> list of (port_id, error_message) for ports that failed
If a port update fails:
    by default, the script stops after that port
    with --continue-on-error, it keeps going and records failures
The applied list is what lets the caller roll back only the ports it touched.
'''
def apply_changes(
    dashboard,
    serial: str,
    changes: PortMap,
    continue_on_error: bool,
) -> Tuple[List[str], List[Tuple[str, str]]]:
    applied: List[str] = []
    failures: List[Tuple[str, str]] = []
    for port_id, payload in changes.items():
        try:
            _with_retry(
                dashboard.switch.updateDeviceSwitchPort,
                serial=serial,
                portId=port_id,
                **payload,
            )
            applied.append(port_id)
            print(f"[UPDATED] Port {port_id}")
        except Exception as exc:
            message = str(exc)
            failures.append((port_id, message))
            print(f"[FAILED] Port {port_id}: {message}")
            if not continue_on_error:
                break
    return applied, failures


'''
Best-effort rollback after a failed apply.
It restores only the ports we actually changed back to their pre-migration
(writable) state, using the target's own backup that was read during preflight.
Ports we never touched are left alone. Any ports that cannot be restored are
written to a rollback failure report.
'''
def rollback_ports(
    dashboard,
    serial: str,
    applied_port_ids: List[str],
    pre_change_norm: PortMap,
    clear_nulls: bool,
) -> None:
    if not applied_port_ids:
        print("[ROLLBACK] Nothing to roll back (no ports were changed).")
        return
    print(f"[ROLLBACK] Restoring {len(applied_port_ids)} changed port(s) on {serial}...")
    restore_plan: PortMap = {}
    for port_id in applied_port_ids:
        original = pre_change_norm.get(port_id, {})
        payload = {}
        for field, val in original.items():
            if val is None and not (clear_nulls and field in CLEARABLE_FIELDS):
                continue
            payload[field] = val
        # Condition the restore payload too, so rollback cannot fail on
        # type-mismatched fields either.
        payload, _ = condition_payload(
            payload,
            original.get("type"),
            original.get("accessPolicyType"),
        )
        if payload:
            restore_plan[port_id] = payload
    _, restore_failures = apply_changes(dashboard, serial, restore_plan, continue_on_error=True)
    if restore_failures:
        path = REPORT_DIR / f"{now_stamp()}_{serial}_rollback_failures.json"
        save_json(path, restore_failures)
        print(f"[ROLLBACK] WARNING: some ports could not be restored. See {path}")
    else:
        print("[ROLLBACK] Completed. Changed ports restored to pre-migration state.")


'''
Gets switch ports from Meraki and normalizes them in one step. Returns : normalized_ports, raw_ports
    normalized ports are used for diff/compare/apply
    raw ports are used for backups
'''
def fetch_and_normalize(dashboard, serial: str) -> Tuple[PortMap, PortMap]:
    raw = get_ports(dashboard, serial)
    return normalize_ports(raw), raw


'''
Checks the source switch config for fields that may appear in the GET response but are not migrated by this script.
'''
def warn_unsupported(raw_source: PortMap) -> None:
    seen = sorted({
        field
        for port in raw_source.values()
        for field in READ_ONLY_OR_UNSUPPORTED
        if field in port
    })
    if seen:
        print("\n[INFO] These fields may appear in GET output but are not migrated by this script:")
        print(", ".join(seen))
        print(
            "Port mirroring is listed here because the switch port update schema may not accept the GET mirror object directly."
        )


'''
Prints a short summary of any fields that were dropped from the change plan
because they do not apply to the port's type. This keeps the operator informed
about exactly what the tool chose not to push and why.
'''
def report_conditioned(dropped_by_port: Dict[str, Dict[str, str]]) -> None:
    if not dropped_by_port:
        return
    total = sum(len(fields) for fields in dropped_by_port.values())
    print("\n[INFO] Field-compatibility conditioning dropped some fields from the plan:")
    for port_id in sort_port_ids(dropped_by_port):
        for field, reason in dropped_by_port[port_id].items():
            print(f"  Port {port_id}: {field} -> {reason}")
    print(f"[INFO] {total} field(s) across {len(dropped_by_port)} port(s) were skipped as not applicable.")


'''
Asks the user to confirm before applying changes.
If --yes is used, this confirmation step is skipped.
'''
def confirm_apply(changes: PortMap, target: str, yes: bool) -> None:
    if yes:
        print("[CONFIRMATION] --yes supplied. Skipping manual APPLY prompt.")
        return
    answer = input(
        f"Apply changes to {len(changes)} ports on target {target}? Type APPLY to continue: "
    )
    if answer != "APPLY":
        raise SystemExit("Aborted. No changes applied.")


'''
Runs all safety checks before comparing or applying changes.
It checks:
    source and target serials are different
    models are correct
    both switches have matching ports
    both switches have the expected port count
    unsupported fields are reported
This is the main safety gate before migration.
'''
def run_preflight(
    dashboard,
    source: str,
    target: str,
    expected_port_count: int,
    expected_source_model: str,
    expected_target_model: str,
    skip_model_check: bool,
) -> Tuple[PortMap, PortMap, PortMap, PortMap]:
    validate_serials(source, target)
    validate_models(
        dashboard=dashboard,
        source_serial=source,
        target_serial=target,
        expected_source_model=expected_source_model,
        expected_target_model=expected_target_model,
        skip_model_check=skip_model_check,
    )
    source_norm, source_raw = fetch_and_normalize(dashboard, source)
    target_norm, target_raw = fetch_and_normalize(dashboard, target)
    validate_ports(source_norm, target_norm)
    validate_expected_port_count(source_norm, target_norm, expected_port_count)
    warn_unsupported(source_raw)
    print(f"[PREFLIGHT] Source and target have matching ports 1-{expected_port_count}.")
    return source_norm, source_raw, target_norm, target_raw


'''
Runs the dry-run workflow.
This mode does not change anything in Meraki.
Steps:
    1. Preflight (validate, read source and target)
    2. Generate differences
    3. Print report
    4. Save report
'''
def run_diff_or_dry(
    dashboard,
    source: str,
    target: str,
    save_report: bool,
    expected_port_count: int,
    expected_source_model: str,
    expected_target_model: str,
    skip_model_check: bool,
) -> DiffMap:
    source_norm, _, target_norm, _ = run_preflight(
        dashboard=dashboard,
        source=source,
        target=target,
        expected_port_count=expected_port_count,
        expected_source_model=expected_source_model,
        expected_target_model=expected_target_model,
        skip_model_check=skip_model_check,
    )
    diffs = generate_diff(source_norm, target_norm)
    print_diff_report(diffs)
    # Show what conditioning would drop, so a dry run previews the real plan.
    dropped: Dict[str, Dict[str, str]] = {}
    build_change_plan(source_norm, target_norm, clear_nulls=False, dropped_out=dropped)
    report_conditioned(dropped)
    if save_report:
        path = REPORT_DIR / f"{now_stamp()}_{source}_to_{target}_diff.json"
        save_json(path, diffs)
        print(f"[REPORT] {path}")
    return diffs


'''
Re-checks the target switch after migration.
Verification passes when the change plan against the CURRENT target is empty,
meaning nothing the tool is responsible for pushing remains. Because it reuses
build_change_plan (same rules, same conditioning), intentionally-skipped fields
and type-incompatible fields can never cause a false failure. Any leftover
differences that are non-writable are reported as INFO, not FAIL.
    known_source_norm -> lets the caller reuse the source config already read
                         during preflight, avoiding a redundant GET.
'''
def run_verify(
    dashboard,
    source: str,
    target: str,
    clear_nulls: bool,
    expected_port_count: int,
    skip_port_count: bool = False,
    known_source_norm: PortMap = None,
) -> bool:
    if known_source_norm is not None:
        source_norm = known_source_norm
    else:
        source_norm, _ = fetch_and_normalize(dashboard, source)
    target_norm, _ = fetch_and_normalize(dashboard, target)
    validate_ports(source_norm, target_norm)
    if not skip_port_count:
        validate_expected_port_count(source_norm, target_norm, expected_port_count)

    residual_plan = build_change_plan(source_norm, target_norm, clear_nulls=clear_nulls)
    informational_diff = generate_diff(source_norm, target_norm)

    if residual_plan:
        print("FAIL: Writable differences still exist after apply")
        print_diff_report(generate_diff(source_norm, target_norm))
        path = REPORT_DIR / f"{now_stamp()}_{source}_to_{target}_verify_fail.json"
        save_json(path, residual_plan)
        print(f"[REPORT] {path}")
        return False

    if informational_diff:
        print(
            "PASS: target matches source for all writable fields.\n"
            "[INFO] Some non-writable / intentionally-skipped differences remain "
            "(e.g. null source values without --clear-nulls, or type-incompatible fields). "
            "These are expected."
        )
    else:
        print("PASS: target matches source for migrated fields")
    return True


'''
Shared apply-and-verify core used by both apply and migrate modes.
    1. Build the change plan (with conditioning)
    2. Report any conditioned/dropped fields
    3. Save the plan and ask the user to confirm
    4. Push changes, retrying transient errors
    5. On failure: optionally roll back changed ports, or warn about partial state
    6. On success: verify the target matches the source for writable fields
Keeping this in one place stops apply and migrate from drifting apart.
'''
def _apply_core(
    dashboard,
    source: str,
    target: str,
    source_norm: PortMap,
    target_norm: PortMap,
    clear_nulls: bool,
    continue_on_error: bool,
    yes: bool,
    rollback_on_failure: bool,
    expected_port_count: int,
) -> None:
    dropped: Dict[str, Dict[str, str]] = {}
    changes = build_change_plan(source_norm, target_norm, clear_nulls=clear_nulls, dropped_out=dropped)
    report_conditioned(dropped)
    if not changes:
        print("No writable changes required. Target already matches source.")
        return
    plan_path = REPORT_DIR / f"{now_stamp()}_{source}_to_{target}_change_plan.json"
    save_json(plan_path, changes)
    print(f"[PLAN] {plan_path}")
    print(f"Ports to update: {len(changes)}")

    confirm_apply(changes, target, yes)
    applied, failures = apply_changes(dashboard, target, changes, continue_on_error)

    if failures:
        failure_path = REPORT_DIR / f"{now_stamp()}_{target}_failures.json"
        save_json(failure_path, failures)
        print(f"[FAILURES] {failure_path}")
        if rollback_on_failure:
            rollback_ports(dashboard, target, applied, target_norm, clear_nulls)
        else:
            print(
                f"[WARNING] Target may be in a PARTIAL state: {len(applied)} port(s) "
                f"changed, {len(failures)} failed. Re-run with --rollback-on-failure "
                "to auto-restore, or restore from the target backup."
            )
        raise SystemExit("One or more port updates failed. Review the failure report.")

    print("\nVerification after apply")
    passed = run_verify(
        dashboard, source, target,
        clear_nulls=clear_nulls,
        expected_port_count=expected_port_count,
        known_source_norm=source_norm,
    )
    if not passed:
        raise SystemExit("Verification failed after apply. Review the verification report.")


'''
Runs the standalone apply workflow.
    1. Preflight checks
    2. Backup source and target configs
    3. Dry-run diff
    4. Apply + verify (via the shared _apply_core)
'''
def run_apply(
    dashboard,
    source: str,
    target: str,
    clear_nulls: bool,
    continue_on_error: bool,
    yes: bool,
    rollback_on_failure: bool,
    expected_port_count: int,
    expected_source_model: str,
    expected_target_model: str,
    skip_model_check: bool,
) -> None:
    source_norm, source_raw, target_norm, target_raw = run_preflight(
        dashboard=dashboard,
        source=source,
        target=target,
        expected_port_count=expected_port_count,
        expected_source_model=expected_source_model,
        expected_target_model=expected_target_model,
        skip_model_check=skip_model_check,
    )
    backup_switch(source, source_raw, "source_before")
    backup_switch(target, target_raw, "target_before")
    diffs = generate_diff(source_norm, target_norm)
    print_diff_report(diffs)
    _apply_core(
        dashboard, source, target, source_norm, target_norm,
        clear_nulls, continue_on_error, yes, rollback_on_failure, expected_port_count,
    )


'''
Runs the full automated migration workflow.
It performs:
    1. Preflight checks
    2. Backup source and target configs
    3. Dry-run diff
    4. Build change plan, apply, and verify (via the shared _apply_core)
This is the main one-command production migration mode.
'''
def run_migrate(
    dashboard,
    source: str,
    target: str,
    clear_nulls: bool,
    continue_on_error: bool,
    yes: bool,
    rollback_on_failure: bool,
    expected_port_count: int,
    expected_source_model: str,
    expected_target_model: str,
    skip_model_check: bool,
) -> None:
    print("\nSTEP 1: Preflight and dry run")
    print("-" * 70)
    source_norm, source_raw, target_norm, target_raw = run_preflight(
        dashboard=dashboard,
        source=source,
        target=target,
        expected_port_count=expected_port_count,
        expected_source_model=expected_source_model,
        expected_target_model=expected_target_model,
        skip_model_check=skip_model_check,
    )
    backup_switch(source, source_raw, "source_before")
    backup_switch(target, target_raw, "target_before")
    diffs = generate_diff(source_norm, target_norm)
    print_diff_report(diffs)
    if not diffs:
        print("No migration needed. Target already matches source for migrated fields.")
        return
    diff_path = REPORT_DIR / f"{now_stamp()}_{source}_to_{target}_diff.json"
    save_json(diff_path, diffs)
    print(f"[REPORT] {diff_path}")

    print("\nSTEP 2: Build change plan and apply")
    print("-" * 70)
    _apply_core(
        dashboard, source, target, source_norm, target_norm,
        clear_nulls, continue_on_error, yes, rollback_on_failure, expected_port_count,
    )
    print("\nMIGRATION COMPLETE")
    print("-" * 70)
    print("PASS: target matches source for migrated fields")


'''
Main entry point of the script. It handles command-line arguments. It decides which workflow to run and when.
It also loads the Meraki dashboard connection and makes sure the folders exist.
'''
def main() -> None:
    parser = argparse.ArgumentParser(
        description="Migrate Meraki switch port config from a source switch to a target switch."
    )
    parser.add_argument(
        "mode",
        choices=["preflight", "diff", "dry-run", "apply", "verify", "migrate"],
    )
    parser.add_argument("--source", required=True, help="Source switch serial, old MS120-48LP")
    parser.add_argument("--target", required=True, help="Target switch serial, new MS130-48X")
    parser.add_argument("--yes", action="store_true", help="Skip interactive APPLY prompt")
    parser.add_argument("--clear-nulls", action="store_true", help="Allow selected null source values to clear target values")
    parser.add_argument("--continue-on-error", action="store_true", help="Continue applying remaining ports if one update fails")
    parser.add_argument("--rollback-on-failure", action="store_true", help="Restore successfully-changed ports if the migration fails partway")
    parser.add_argument("--expected-port-count", type=int, default=48, help="Expected port count. Default: 48")
    parser.add_argument("--expected-source-model", default="MS120-48LP", help="Expected source model substring. Default: MS120-48LP")
    parser.add_argument("--expected-target-model", default="MS130-48X", help="Expected target model substring. Default: MS130-48X")
    parser.add_argument("--skip-model-check", action="store_true", help="Skip source/target model validation")
    args = parser.parse_args()
    ensure_dirs()
    dashboard = load_dashboard()
    if args.mode == "preflight":
        run_preflight(
            dashboard=dashboard,
            source=args.source,
            target=args.target,
            expected_port_count=args.expected_port_count,
            expected_source_model=args.expected_source_model,
            expected_target_model=args.expected_target_model,
            skip_model_check=args.skip_model_check,
        )
    elif args.mode in {"diff", "dry-run"}:
        run_diff_or_dry(
            dashboard=dashboard,
            source=args.source,
            target=args.target,
            save_report=True,
            expected_port_count=args.expected_port_count,
            expected_source_model=args.expected_source_model,
            expected_target_model=args.expected_target_model,
            skip_model_check=args.skip_model_check,
        )
    elif args.mode == "apply":
        run_apply(
            dashboard=dashboard,
            source=args.source,
            target=args.target,
            clear_nulls=args.clear_nulls,
            continue_on_error=args.continue_on_error,
            yes=args.yes,
            rollback_on_failure=args.rollback_on_failure,
            expected_port_count=args.expected_port_count,
            expected_source_model=args.expected_source_model,
            expected_target_model=args.expected_target_model,
            skip_model_check=args.skip_model_check,
        )
    elif args.mode == "verify":
        passed = run_verify(
            dashboard=dashboard,
            source=args.source,
            target=args.target,
            clear_nulls=args.clear_nulls,
            expected_port_count=args.expected_port_count,
        )
        if not passed:
            raise SystemExit("Verification failed.")
    elif args.mode == "migrate":
        run_migrate(
            dashboard=dashboard,
            source=args.source,
            target=args.target,
            clear_nulls=args.clear_nulls,
            continue_on_error=args.continue_on_error,
            yes=args.yes,
            rollback_on_failure=args.rollback_on_failure,
            expected_port_count=args.expected_port_count,
            expected_source_model=args.expected_source_model,
            expected_target_model=args.expected_target_model,
            skip_model_check=args.skip_model_check,
        )


if __name__ == "__main__":
    main()