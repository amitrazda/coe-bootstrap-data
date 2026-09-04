"""
Nautobot Job: Fix Missing Primary IPs (COE Network)

Standalone repair job. For any Device (by name) that already exists in
Nautobot but has no primary_ip4 set, this re-parses the same source workbook,
finds that device's expected IP(s), ensures a parent Prefix exists for that
IP's network, creates the Interface/IPAddress, and assigns primary_ip4 --
WITHOUT touching devices that already have an IP, and WITHOUT re-creating
devices that don't exist (run the main onboarding job for those instead).

This exists because the original onboarding job had two related bugs:
1. IPAddress.objects.get_or_create(namespace=...) -- 'namespace' isn't a
   literal DB field on IPAddress, only a create()-time convenience kwarg, so
   get_or_create's field validation rejected it.
2. Even after fixing that with an explicit .create(), Nautobot 2.x requires
   a parent Prefix (e.g. 10.16.0.0/16) to already exist in the target
   Namespace before an IPAddress can be created inside it -- it will not
   auto-create one. Prefix has the exact same get_or_create(prefix=...)
   incompatibility as IPAddress did, for the same reason (prefix= is also a
   constructor-only convenience kwarg, not a literal field).
"""
import ipaddress

from nautobot.apps.jobs import Job, FileVar, register_jobs
from nautobot.dcim.models import Device, Interface
from nautobot.extras.models import Status
from nautobot.ipam.models import IPAddress, Namespace, Prefix

from .coe_inventory_parser import parse_workbook


class FixMissingCOEDeviceIPs(Job):
    inventory_file = FileVar(
        description="Same COE Network 'Ip_Details' workbook used for onboarding (.xlsx)",
        required=True,
    )

    class Meta:
        name = "Fix Missing COE Device IPs"
        description = (
            "Repair job: assigns primary IPs to already-onboarded COE devices that are "
            "missing one, without recreating devices or touching devices that already have an IP."
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

        fixed, already_had_ip, not_found, no_ip_in_sheet, errors = 0, 0, 0, 0, 0

        for dev in devices:
            name = dev["name"]
            device = Device.objects.filter(name=name).first()
            if not device:
                self.logger.warning(f"'{name}': not found in Nautobot -- skipping (run the onboarding job for this one).")
                not_found += 1
                continue

            if device.primary_ip4_id is not None:
                already_had_ip += 1
                continue

            if not dev["ip_addresses"]:
                self.logger.info(f"'{name}': source sheet has no IP for this device -- nothing to fix.")
                no_ip_in_sheet += 1
                continue

            try:
                mgmt_intf = Interface.objects.filter(device=device, name="Management0").first()
                if not mgmt_intf:
                    mgmt_intf = Interface.objects.create(
                        device=device,
                        name="Management0",
                        type="virtual",
                        status=active_status,
                    )
                    self.logger.info(f"'{name}': Management0 interface didn't exist -- created it.")

                for ip, prefix_len in dev["ip_addresses"]:
                    net = ipaddress.ip_network(f"{ip}/{prefix_len}", strict=False)
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

                    addr = f"{ip}/{prefix_len}"
                    ip_obj = IPAddress.objects.filter(host=ip, parent__namespace=namespace).first()
                    if not ip_obj:
                        ip_obj = IPAddress.objects.create(
                            address=addr,
                            namespace=namespace,
                            status=active_status,
                        )
                    mgmt_intf.ip_addresses.add(ip_obj)
                    if device.primary_ip4_id is None:
                        device.primary_ip4 = ip_obj
                        device.save()

                self.logger.info(f"'{name}': assigned primary IP {dev['ip_addresses'][0][0]}.")
                fixed += 1

            except Exception as exc:  # noqa: BLE001
                self.logger.failure(f"'{name}': failed to assign IP -- {exc}")
                errors += 1

        self.logger.info(
            f"Done. fixed={fixed}, already_had_ip={already_had_ip}, "
            f"not_found_in_nautobot={not_found}, no_ip_in_sheet={no_ip_in_sheet}, errors={errors}."
        )


register_jobs(FixMissingCOEDeviceIPs)
