"""Copy the management IPv4 configuration between Meraki switches."""

import json
from datetime import datetime
from pathlib import Path


BACKUP_DIR = Path("backups")


def _save_backup(serial, settings):
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = BACKUP_DIR / f"{timestamp}_{serial}_management_before.json"

    with path.open("w", encoding="utf-8") as file:
        json.dump(settings, file, indent=2, sort_keys=True)

    print(f"[BACKUP] {path}")


def _build_payload(source_settings):
    source_wan1 = source_settings.get("wan1", {})

    using_static_ip = source_wan1.get("usingStaticIp", False)

    payload = {
        "usingStaticIp": using_static_ip,
    }

    if source_wan1.get("vlan") is not None:
        payload["vlan"] = source_wan1["vlan"]

    if using_static_ip:
        required_fields = (
            "staticIp",
            "staticSubnetMask",
            "staticGatewayIp",
        )

        missing = [
            field
            for field in required_fields
            if not source_wan1.get(field)
        ]

        if missing:
            raise ValueError(
                "Source management interface is missing: "
                + ", ".join(missing)
            )

        payload["staticIp"] = source_wan1["staticIp"]
        payload["staticSubnetMask"] = source_wan1["staticSubnetMask"]
        payload["staticGatewayIp"] = source_wan1["staticGatewayIp"]
        payload["staticDns"] = source_wan1.get("staticDns", [])

    return {"wan1": payload}


def copy_management_ipv4(dashboard, source_serial, target_serial):
    """Copy management IPv4 settings from source to target."""

    print("\nMANAGEMENT IPv4 MIGRATION")
    print("-" * 70)

    source_settings = (
        dashboard.devices.getDeviceManagementInterface(source_serial)
    )

    target_settings = (
        dashboard.devices.getDeviceManagementInterface(target_serial)
    )

    _save_backup(target_serial, target_settings)

    payload = _build_payload(source_settings)

    source_wan1 = payload["wan1"]
    dns_servers = source_wan1.get("staticDns", [])

    print(
        "[MANAGEMENT] IPv4 type: "
        + ("Static" if source_wan1["usingStaticIp"] else "DHCP")
    )
    print(f"[MANAGEMENT] LAN IPv4: {source_wan1.get('staticIp', 'DHCP')}")
    print(f"[MANAGEMENT] VLAN: {source_wan1.get('vlan', 'Not configured')}")
    print(
        "[MANAGEMENT] Subnet mask: "
        f"{source_wan1.get('staticSubnetMask', 'DHCP')}"
    )
    print(
        "[MANAGEMENT] Gateway: "
        f"{source_wan1.get('staticGatewayIp', 'DHCP')}"
    )
    print(
        "[MANAGEMENT] Primary DNS: "
        f"{dns_servers[0] if len(dns_servers) >= 1 else 'Not configured'}"
    )
    print(
        "[MANAGEMENT] Secondary DNS: "
        f"{dns_servers[1] if len(dns_servers) >= 2 else 'Not configured'}"
    )

    dashboard.devices.updateDeviceManagementInterface(
        serial=target_serial,
        **payload,
    )

    print("[MANAGEMENT] Target management IPv4 settings updated.")