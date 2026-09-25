"""
Nautobot Job: Update COE Device IP Prefixes

Standalone repair job. For devices that already exist in Nautobot AND already
have a primary IP, this checks whether the source workbook's current
prefix length (e.g. after a subnet change like /16 -> /24) matches what's
actually stored on the IPAddress, and corrects it in place if not.

This is a different problem from fix_missing_ips.py, which only fills in a
primary IP where NONE exists. Here the IP already exists but its mask_length
is stale. Running the main onboarding job again does NOT fix this -- it
skips any device that already exists (see its "skipped(existing)" counter),
so the mismatch persists silently.

Why a direct .mask_length assignment isn't enough on its own:
Nautobot's IPAddress.clean() explicitly rejects a mask_length change if
`self.parent` still points to a Prefix that no longer matches the new
mask_length -- it will NOT silently reparent for you. You must:
  1. Ensure the new parent Prefix (e.g. 10.16.16.0/24) already exists in the
     namespace (same Prefix auto-creation logic as fix_missing_ips.py).
  2. Set ip_obj.mask_length to the new value.
  3. Set ip_obj.parent = None so clean() takes the "implicitly changed --
     recompute" branch instead of the "explicitly mismatched -- reject"
     branch, then call validated_save() (which recomputes the correct
     parent automatically).
"""
import ipaddress

from nautobot.apps.jobs import Job, FileVar, register_jobs
from nautobot.dcim.models import Device
from nautobot.extras.models import Status
from nautobot.ipam.models import IPAddress, Namespace, Prefix

from .coe_inventory_parser import parse_workbook


class UpdateCOEDeviceIPPrefixes(Job):
    inventory_file = FileVar(
        description="Corrected COE Network 'Ip_Details' workbook (.xlsx) with the new prefix length(s)",
        required=True,
    )

    class Meta:
        name = "Update COE Device IP Prefixes"
        description = (
            "Repair job: corrects the mask_length (e.g. /16 -> /24) on already-onboarded "
            "devices' existing primary IPs to match the current source workbook. Does NOT "
            "touch devices whose stored prefix already matches, and does not create devices "
            "or IPs that don't already exist -- run the onboarding job or fix_missing_ips.py "
            "for those instead."
        )
        has_sensitive_variables = False

    def run(self, inventory_file):
        devices, warnings = parse_workbook(inventory_file.file)
        self.logger.info(f"Parsed {len(devices)} device records from the workbook.")

        active_status = Status.objects.filter(name="Active").first()
        if not active_status:
            active_status = Status.objects.first()
            self.logger.warning(f"'Active' status not found -- using '{active_status}' instead.")

        namespace = Namespace.objects.get_or_create(name="Global")[0]
        prefix_cache = {}

        updated, already_correct, not_found, no_primary_ip, no_ip_in_sheet, errors = 0, 0, 0, 0, 0, 0

        for dev in devices:
            name = dev["name"]
            device = Device.objects.filter(name=name).first()
            if not device:
                self.logger.warning(f"'{name}': not found in Nautobot -- skipping.")
                not_found += 1
                continue

            if not dev["ip_addresses"]:
                no_ip_in_sheet += 1
                continue

            # Only checking/correcting the device's current primary IPv4 for now.
            ip_obj = device.primary_ip4
            if ip_obj is None:
                self.logger.warning(
                    f"'{name}': has no primary IP at all -- run 'Fix Missing COE Device IPs' first, "
                    f"not this job."
                )
                no_primary_ip += 1
                continue

            # Match this device's primary IP host against the workbook's expected entries
            # (a device can have more than one IP row; match by host address).
            expected = next((p for p in dev["ip_addresses"] if p[0] == ip_obj.host), None)
            if expected is None:
                self.logger.warning(
                    f"'{name}': current primary IP {ip_obj.host} doesn't match any IP listed for "
                    f"this device in the workbook -- skipping (manual review needed)."
                )
                errors += 1
                continue

            expected_ip, expected_prefix_len = expected
            expected_prefix_len = int(expected_prefix_len)

            if ip_obj.mask_length == expected_prefix_len:
                already_correct += 1
                continue

            try:
                net = ipaddress.ip_network(f"{expected_ip}/{expected_prefix_len}", strict=False)
                cache_key = str(net)
                prefix_obj = prefix_cache.get(cache_key)
                if prefix_obj is None:
                    prefix_obj = Prefix.objects.filter(
                        network=str(net.network_address),
                        prefix_length=net.prefixlen,
                        namespace=namespace,
                    ).first()
                    if not prefix_obj:
                        prefix_obj = Prefix.objects.create(
                            prefix=str(net),
                            namespace=namespace,
                            status=active_status,
                        )
                        self.logger.info(f"Created parent Prefix '{net}' in namespace Global.")
                    prefix_cache[cache_key] = prefix_obj

                old_mask = ip_obj.mask_length
                ip_obj.mask_length = expected_prefix_len
                ip_obj.parent = None  # let clean() recompute -- required, see module docstring
                ip_obj.validated_save()

                self.logger.info(
                    f"'{name}': updated {ip_obj.host} from /{old_mask} to /{expected_prefix_len}."
                )
                updated += 1

            except Exception as exc:  # noqa: BLE001
                self.logger.failure(f"'{name}': failed to update prefix -- {exc}")
                errors += 1

        self.logger.info(
            f"Done. updated={updated}, already_correct={already_correct}, "
            f"not_found_in_nautobot={not_found}, no_primary_ip={no_primary_ip}, "
            f"no_ip_in_sheet={no_ip_in_sheet}, errors={errors}."
        )


register_jobs(UpdateCOEDeviceIPPrefixes)
