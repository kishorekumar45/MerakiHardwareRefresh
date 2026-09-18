# Meraki Switch Refresh Tool

[![Python Check](https://github.com/kishorekumar45/meraki-switch-refresh/actions/workflows/ci.yml/badge.svgcom/kishorekumar45/meraki-switch-refresh/actions/workflows/ci.yml)
4
![Python](https://img.shields.io/badge/Python-3.13atform](https://img.shields.io/bm-Windows-lightgrey
5
![Cisco Meraki](https://img.shields.io/badge/Cisco-Meraki-67ttps://img.shields.io/badge/Source-MS120--48LP-bluemg.shields.io/badge/Target-MS130--48X-green
6
![API](https://img.shields.io/badge/Meraki-Dashboard_API
7
![Status](https://img.shields.io/badge/Status-Production_n

This is the production script for migrating switch port configuration from an existing Meraki MS120-48LP switch to a replacement Meraki MS130-48X switch.

This script **does** connect to Meraki Dashboard.
This script **does** use the Meraki API.
This script **does** require a `.env` file with a valid Meraki API key.

Before using this production script, run the offline test harness first:
    powershell: python test/test_main.py full-demo
The offline test harness lets you safely validate the workflow locally before running against real Meraki switches.

---

## What This Script Does

The production script uses real Meraki Dashboard API calls to:

- Read port configuration from the source MS120-48LP switch
- Read port configuration from the target MS130-48X switch
- Normalize both switch configs to only the fields selected for migration
- Compare source and target port configs
- Create a change plan
- Back up source and target port configs
- Apply the source port config to the target switch
- Re-read the target switch
- Verify that the target matches the source for migrated fields

The script assumes a 48-port refresh workflow by default:
    Source: MS120-48LP
    Target: MS130-48X
    Expected ports: 1-48

---

## Files and Folders

text
main.py
.env
requirements.txt
backups/
reports/
test/


### File Purpose

main.py
    Runs the production Meraki switch refresh workflow.

.env
    Stores the Meraki API key.

requirements.txt
    Lists required Python packages.

backups/
    Stores source and target switch port backups before migration.

reports
    Stores diff reports, change plans, verification failure reports, and failure logs.

test/
    Contains the offline test harness. Run this before production.

---

## Setup

Install dependencies:
    powershell: pip install -r requirements.txt

Create a `.env` file in the project root:
    MERAKI_API_KEY=your_meraki_api_key_here
    
    Confirm the `.env` file is named exactly:

---

## Recommended Command

Use the automated migration command after you have tested the offline harness, reviewed the dry-run results, and are ready to run against production.

Run this from the project root:
    powershell: python main.py migrate --source OLD_SERIAL --target NEW_SERIAL --expected-port-count 52 --rollback-on-failure --yes


Example:
    powershell: python main.py migrate --source Q2GX-B3FM-HBTM --target Q3LV-JRJY-4EW3 --expected-port-count 52 --rollback-on-failure --yes
Use `--yes` only after you are confident the dry-run output and change plan are correct.

Expected successful final output:
    MIGRATION COMPLETE
    ----------------------------------------------------------------------
    PASS: target matches source for migrated fields

### What this does

This Command:
    1. Validates the source and target switch models.
    2. Validates that both switches expose ports 1 through 52.
    3. Backs up the source and target port configurations.
    4. Generates and saves a dry-run difference report.
    5. Builds and saves the change plan.
    6. Applies the source port configuration to the target switch.
    7. Attempts to restore successfully changed target ports if the migration fails partway.
    8. Re-reads the target switch and verifies all writable migrated fields.

    Important: The script reads from the source switch but sends configuration updates only to the target switch. The --yes option skips the manual APPLY confirmation, so verify both serial numbers carefully before running the command.

## Optional: Run Each Command Separately

If you want to go deeper and control each stage manually, run the commands one by one.

---

### 1. Syntax check - Checks that `main.py` has valid Python syntax.

    powershell: python -m py_compile main.py

Expected result:
    No output


### 2. Preflight check - Runs safety checks before generating a diff or applying changes.

    powershell: python main.py preflight --source OLD_SERIAL --target NEW_SERIAL

    What this does
    It checks:
    - Source and target serials are different
    - Source model contains MS120-48LP
    - Target model contains MS130-48X
    - Source and target port IDs match
    - Both switches have ports 1-48
    - Unsupported/read-only fields are reported

    Expected output:
        [MODEL] Source OLD_SERIAL: MS120-48LP
        [MODEL] Target NEW_SERIAL: MS130-48X
        [PREFLIGHT] Source and target have matching ports 1-48.

---

### 3. Dry run

    powershell: python main.py dry-run --source OLD_SERIAL --target NEW_SERIAL
    
    What this does:
    Compares the source switch config against the target switch config.
    This does **not** apply any changes.
    
    Expected output:
        Ports requiring updates: ...
        Field differences: ...
        [REPORT] reports/...

    This creates a diff report in: reports/
---

### 4. Apply migration

    powershell: python main.py apply --source OLD_SERIAL --target NEW_SERIAL

    Expected output:
        [BACKUP] backups/...
        [PLAN] reports/...
        Apply changes to ... ports on target NEW_SERIAL? Type APPLY to continue:
        [UPDATED] Port 1
        [UPDATED] Port 2

    Verification after apply
        PASS: target matches source for migrated fields
---

### 5. Verify migration

    powershell: python main.py verify --source OLD_SERIAL --target NEW_SERIAL

    What this does:
        Compares the source switch config against the updated target switch config.

    Expected output:
        PASS: target matches source for migrated fields

        If verification fails, a verification failure report is saved in: reports/
---

## Recommended Manual Production Sequence

If you do not want to use the one-command `migrate` workflow, run this sequence manually:
    powershell: 
        python -m py_compile main.py
        python main.py preflight --source OLD_SERIAL --target NEW_SERIAL
        python main.py dry-run --source OLD_SERIAL --target NEW_SERIAL
        python main.py apply --source OLD_SERIAL --target NEW_SERIAL
        python main.py verify --source OLD_SERIAL --target NEW_SERIAL

    Expected final result:
    PASS: target matches source for migrated fields


---
## Other commands
### Continue if one port update fails
    powershell: --continue-on-error

    Continues updating remaining ports if one port update fails. By default, the script stops after a failure.

---

### Skip model validation
    powershell: --skip-model-check

    Skips checking that the source is `MS120-48LP` and the target is `MS130-48X`. Use this only if you are certain the serial numbers are correct.

---

### Change expected port count

    powershell: --expected-port-count 48

    Default is `48`.
    Use this only if adapting the script for a different switch refresh path.
---


## Important Notes

- Run the offline test harness first before production.
- The production script makes real Meraki Dashboard API calls.
- The target switch can be changed when using `apply` or `migrate`.
- Always review the dry-run output before applying changes.
- Keep the generated backup and report files for change records.
- Port mirroring is reported as unsupported/read-only and is not migrated by this script.

---

## Safe Production Flow

    Best practice sequence:
    powershell:
        python test/test_main.py full-demo
        python main.py preflight --source OLD_SERIAL --target NEW_SERIAL
        python main.py dry-run --source OLD_SERIAL --target NEW_SERIAL
        python main.py migrate --source OLD_SERIAL --target NEW_SERIAL

    If the final command succeeds, expected final output is:
        MIGRATION COMPLETE
        ----------------------------------------------------------------------
        PASS: target matches source for migrated fields

