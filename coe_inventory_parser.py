"""
Parser for the COE Network 'Ip_Details' workbook.

Kept separate from the Nautobot Job class so it can be unit-tested with plain
openpyxl in any Python environment -- no Nautobot install required to verify
the parsing logic itself. The Job (import_coe_inventory.py) imports this
module and does the Nautobot object creation.
"""
import re
from openpyxl import load_workbook

# Sheet names are NOT relied on -- across workbook revisions the same vendor
# data has shown up under different sheet names (e.g. "PALO-Setup" became
# "PALO-SING-LAB", "Arista Assets" became "Arista-AUS"). Instead each sheet is
# classified by scanning its cells for a recognizable header row:
#   - "standard" layout: a row with col B == 'Device' and col C == 'Make'
#     (possibly repeated more than once in the same sheet, e.g. two GNS3 lab
#     blocks stacked in one sheet).
#   - "versa" layout: a row with col B == 'Sr. No.'
# Sheets matching neither are skipped with a warning.

# Site/region tokens we recognize in a sheet name, used as a location fallback
# when the device name itself doesn't imply a location and there's no block title.
SHEET_NAME_LOCATION_HINTS = [
    ("new-york", "New York"),
    ("new york", "New York"),
    ("london", "London"),
    ("sing", "Singapore"),
    ("aus", "Australia"),
    ("india", "India"),
    ("ben", "Bengaluru"),
    ("manc", "Manchester"),
]

# Make -> Nautobot Platform network_driver (netmiko/napalm style names).
# ASA is special-cased below since it shares the "Cisco" Make column.
PLATFORM_DRIVER_MAP = {
    "cisco": "cisco_ios",
    "arista": "arista_eos",
    "juniper": "juniper_junos",
    "palo-alto": "paloalto_panos",
    "fortinet": "fortinet",
    "ubuntu": "linux",
}


def _clean(val):
    if val is None:
        return None
    if isinstance(val, str):
        v = val.replace("\n", " ").strip()
        return v if v else None
    return val


def _split_ip_cidr(raw_ip, raw_cidr):
    """
    Normalize the messy Ip address / CIDR columns into a best-effort list of
    (ip, prefixlen) tuples. Source data has: single IPs with /mask baked in,
    bare IPs with a separate CIDR column, ranges ("172.16.0.1/29 - 172.16.0.7/29"),
    comma-separated multi-IP cells, typos ("10.16..0.0/16"), and free-text
    notes mixed into the cell ("10.16.16.100--> Connects further to internet...").
    """
    results = []
    raw_ip = _clean(raw_ip)
    raw_cidr = _clean(raw_cidr)
    if not raw_ip:
        return results

    # Pull every plausible IPv4/prefix token out of the ip cell; ignore prose.
    ip_pattern = re.compile(r"\d{1,3}(?:\.\d{1,3}){3}(?:/\d{1,2})?")
    tokens = ip_pattern.findall(raw_ip)

    fallback_prefix = None
    if raw_cidr:
        m = re.search(r"/(\d{1,2})", raw_cidr)
        if m:
            fallback_prefix = m.group(1)

    for tok in tokens:
        if "/" in tok:
            ip, prefix = tok.split("/", 1)
        else:
            ip, prefix = tok, fallback_prefix or "32"
        results.append((ip, prefix))

    # De-duplicate while preserving order (ranges list start+end of same /29 etc.)
    seen = set()
    deduped = []
    for ip, prefix in results:
        key = (ip, prefix)
        if key not in seen:
            seen.add(key)
            deduped.append(key)
    return deduped


def _guess_platform_driver(make, model):
    make_key = (make or "").strip().lower()
    model_key = (model or "").strip().lower()
    if make_key == "cisco" and "asa" in model_key:
        return "cisco_asa"
    return PLATFORM_DRIVER_MAP.get(make_key)


def _guess_role(name, make, model):
    n = (name or "").lower()
    m = (model or "").lower()
    mk = (make or "").lower()
    if "-ap" in n or n.endswith("ap") or m.startswith("ap"):
        return "access-point"
    if "asa" in m or "srx" in m or "fortigate" in m or mk == "palo-alto" or "firewall" in m:
        return "firewall"
    if "ssr" in m or "sdwan" in n or "sd-wan" in n or "sd-forti" in n:
        return "sd-wan-router"
    if "spine" in n:
        return "spine-switch"
    if "leaf" in n:
        return "leaf-switch"
    if "_l2" in m or m.endswith("l2") or "ex2300" in m or "ex4400" in m or "720d" in m:
        return "switch"
    return "router"


def _guess_location(sheet_name, block_label, device_name, virt_phys):
    n = (device_name or "").upper()
    if n.startswith("LONDON"):
        return "London"
    if n.startswith("GL-NEW-OF"):
        return "GL-NEW-OF"
    if block_label:
        # "Lab 112(GNS3)" / "Lab 52(GNS3)" -> "Lab-112-GNS3" / "Lab-52-GNS3"
        m = re.search(r"Lab\s*(\d+)", block_label, re.IGNORECASE)
        if m:
            return f"Lab-{m.group(1)}-GNS3"
        return block_label.strip()
    sheet_key = (sheet_name or "").lower()
    for token, place in SHEET_NAME_LOCATION_HINTS:
        if token in sheet_key:
            return place
    if "eveng" in sheet_key or "eve-ng" in sheet_key:
        return "EVE-NG-DC-Lab"
    if (virt_phys or "").strip().lower() == "cloud":
        return "Cloud"
    if (virt_phys or "").strip().lower() == "physical":
        return "Lab-Physical-Rack"
    return "Lab"


def parse_standard_sheet(ws, sheet_name):
    """
    Scans every row for a header row (col B == 'Device'), and treats all rows
    after it -- up to the next blank row or the next header row -- as data.
    Handles sheets with more than one block (e.g. Other Allocations).
    Returns a list of normalized device dicts.
    """
    rows = list(ws.iter_rows(values_only=True))
    devices = []
    block_label = None
    in_block = False

    for row in rows:
        # Row layout: col A is always blank ("Unnamed: 0"); real data starts at B.
        b, c, d, e, f, g, h, i, j = (list(row) + [None] * 9)[1:10]
        b, c, d, e, f, g, h, i, j = map(_clean, (b, c, d, e, f, g, h, i, j))

        if b == "Device" and c == "Make":
            in_block = True
            continue

        if not in_block:
            # Possible block title row, e.g. "Lab 112(GNS3)" -- only column B populated.
            if b and not any([c, d, e, f, g, h, i, j]):
                block_label = b
            continue

        # Blank row ends the current block.
        if not any([b, c, d, e, f, g, h, i, j]):
            in_block = False
            continue

        name, make, model, virt_phys, ip_cell, cidr_cell, user, pwd, enable_pwd = (
            b, c, d, e, f, g, h, i, j
        )
        if not name:
            continue

        ip_pairs = _split_ip_cidr(ip_cell, cidr_cell)
        devices.append(
            {
                "name": name,
                "make": make,
                "model": model,
                "virtual_physical_cloud": virt_phys,
                "ip_addresses": ip_pairs,
                "username": user,
                "has_password": bool(pwd),
                "has_enable_password": bool(enable_pwd),
                "platform_driver": _guess_platform_driver(make, model),
                "role": _guess_role(name, make, model),
                "location": _guess_location(sheet_name, block_label, name, virt_phys),
                "source_sheet": sheet_name,
            }
        )
    return devices


def parse_versa_sheet(ws, sheet_name="Versa"):
    """
    Versa sheet has a bespoke column layout (Sr.No / VM-Cloud / MGMT IP / Make /
    Model / Internet IP x2 / MPLS x2 / ASN / creds / breakout). We pull out a
    normalized device dict keyed on MGMT IP where present, else the first
    reachable address for cloud-hosted controllers (Director/Controller as a URL
    or bare IP).
    """
    rows = list(ws.iter_rows(values_only=True))
    devices = []
    in_block = False

    for row in rows:
        vals = list(row) + [None] * 20
        b, c, d, e, f, g = (vals[1], vals[2], vals[3], vals[4], vals[5], vals[6])
        q, r_ = vals[17] if len(vals) > 17 else None, vals[18] if len(vals) > 18 else None
        b, c, d, e, f, g = map(_clean, (b, c, d, e, f, g))
        q, r_ = _clean(q), _clean(r_)

        if b == "Sr. No.":
            in_block = True
            continue
        if not in_block:
            continue
        if not any([b, c, d, e, f, g]):
            break

        name = c
        if not name:
            continue

        mgmt_ip = d
        ip_pairs = _split_ip_cidr(mgmt_ip, None)
        if not ip_pairs and f:
            # Cloud-hosted controllers/director often only have a URL or bare public IP in col F.
            ip_match = re.search(r"\d{1,3}(?:\.\d{1,3}){3}", f)
            if ip_match:
                ip_pairs = [(ip_match.group(0), "32")]

        make = e or "Versa"
        model = g

        devices.append(
            {
                "name": name,
                "make": make,
                "model": model,
                "virtual_physical_cloud": "Cloud" if "hosted on cloud" in (e or "").lower() else "Virtual",
                "ip_addresses": ip_pairs,
                "username": q,
                "has_password": bool(r_),
                "has_enable_password": False,
                "platform_driver": "versa_flexvnf",
                "role": "sd-wan-router" if "sdwan" in name.lower() else "sd-wan-controller",
                "location": "Cloud" if "hosted on cloud" in (e or "").lower() else "Lab",
                "source_sheet": sheet_name,
            }
        )
    return devices


def _classify_sheet(ws):
    """
    Returns 'standard', 'versa', or None by scanning every row for a
    recognizable header, rather than trusting the sheet's name/tab label.
    """
    for row in ws.iter_rows(values_only=True):
        vals = list(row) + [None] * 3
        b, c = _clean(vals[1]), _clean(vals[2])
        if b == "Sr. No.":
            return "versa"
        if b == "Device" and c == "Make":
            return "standard"
    return None


def parse_workbook(path_or_fileobj):
    wb = load_workbook(path_or_fileobj, data_only=True, read_only=True)
    all_devices = []
    warnings = []

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        kind = _classify_sheet(ws)
        if kind == "standard":
            all_devices.extend(parse_standard_sheet(ws, sheet_name))
        elif kind == "versa":
            all_devices.extend(parse_versa_sheet(ws, sheet_name))
        else:
            warnings.append(
                f"Sheet '{sheet_name}' did not match a known header layout -- skipped."
            )

    # Flag duplicate device names and duplicate IPs across the whole workbook.
    seen_names = {}
    for dev in all_devices:
        seen_names.setdefault(dev["name"], []).append(dev["source_sheet"])
    for name, sheets in seen_names.items():
        if len(sheets) > 1:
            warnings.append(f"Duplicate device name '{name}' found in sheets: {sheets}")

    seen_ips = {}
    for dev in all_devices:
        for ip, _prefix in dev["ip_addresses"]:
            seen_ips.setdefault(ip, []).append(dev["name"])
    for ip, names in seen_ips.items():
        if len(names) > 1:
            warnings.append(f"Duplicate IP '{ip}' used by devices: {names}")

    return all_devices, warnings


if __name__ == "__main__":
    import sys
    import json

    path = sys.argv[1] if len(sys.argv) > 1 else "Ip_Details-COE_Network-V1_4.xlsx"
    devices, warnings = parse_workbook(path)
    print(f"Parsed {len(devices)} devices from {path}\n")
    for d in devices:
        print(json.dumps(d, indent=2))
    if warnings:
        print("\n--- WARNINGS ---")
        for w in warnings:
            print(f"- {w}")
