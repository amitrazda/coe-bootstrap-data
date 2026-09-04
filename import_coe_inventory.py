"""
Nautobot Job: Import COE Network Inventory

Reads the "Ip_Details-COE_Network" workbook (uploaded at run time) and creates
Locations, Manufacturers, Platforms, DeviceTypes, Roles, Devices, and primary
management IP addresses in Nautobot.

INSTALL:
  1. Drop this file AND coe_inventory_parser.py into the same directory in
     your Jobs source (JOBS_ROOT, or a Git Repository configured as a
     "jobs" data source).
  2. In Nautobot: Jobs -> refresh/sync the job source, then enable this Job
     under Jobs > Job list (Jobs are disabled by default).
  3. Run it with "Dry run" checked first. Read the job log fully before
     re-running with dry run unchecked.

SECURITY NOTE:
  This Job intentionally does NOT write the plaintext usernames/passwords
  from the spreadsheet into Nautobot. It only reports (in the job log) which
  devices have credentials in the source file, so you can load them into
  Nautobot's Secrets app (or Vault/environment-variable backed secrets)
  yourself, keyed by device name.
"""
from django.contrib.contenttypes.models import ContentType
import ipaddress
from nautobot.apps.jobs import Job, FileVar, BooleanVar, ObjectVar, register_jobs
from nautobot.dcim.models import (
    Device,
    DeviceType,
    Interface,
    Location,
    LocationType,
    Manufacturer,
    Platform,
)
from nautobot.extras.models import Role, Status
from nautobot.ipam.models import IPAddress, Namespace, Prefix

from .coe_inventory_parser import parse_workbook

# Roles this Job creates on demand if they don't already exist.
KNOWN_ROLES = [
    "router",
    "switch",
    "firewall",
    "access-point",
    "spine-switch",
    "leaf-switch",
    "sd-wan-router",
    "sd-wan-controller",
]


class ImportCOENetworkInventory(Job):
    inventory_file = FileVar(
        description="COE Network 'Ip_Details' workbook (.xlsx)",
        required=True,
    )
    default_location = ObjectVar(
        model=Location,
        required=False,
        description=(
            "Fallback Location for devices where the sheet doesn't imply one. "
            "If left blank, a Location is created per inferred name "
            "(e.g. 'Lab-112-GNS3', 'London', 'Cloud')."
        ),
    )
    dry_run = BooleanVar(
        default=True,
        description="If checked, only logs what WOULD be created/changed -- no database writes.",
    )

    class Meta:
        name = "Import COE Network Inventory"
        description = "Parse the COE Ip_Details workbook and onboard devices into Nautobot."
        has_sensitive_variables = False

    def run(self, inventory_file, default_location=None, dry_run=True):
        devices, warnings = parse_workbook(inventory_file.file)

        self.logger.info(f"Parsed {len(devices)} device records from the workbook.")
        for w in warnings:
            self.logger.warning(w)

        if dry_run:
            self.logger.info("DRY RUN -- no changes will be written to Nautobot.")

        active_status = self._get_status("Active")
        location_type = self._get_or_create_location_type("Lab Segment")

        location_cache = {}
        manufacturer_cache = {}
        platform_cache = {}
        devicetype_cache = {}
        role_cache = {}
        prefix_cache = {}

        created, skipped, errors = 0, 0, 0

        for dev in devices:
            try:
                loc_name = default_location.name if default_location else dev["location"]
                location = self._get_or_create_location(
                    loc_name, location_type, location_cache, active_status, dry_run
                )

                manufacturer = self._get_or_create_manufacturer(
                    dev["make"], manufacturer_cache, dry_run
                )

                platform = self._get_or_create_platform(
                    dev["make"], dev["platform_driver"], manufacturer, platform_cache, dry_run
                )

                device_type = self._get_or_create_device_type(
                    manufacturer, dev["model"], devicetype_cache, dry_run
                )

                role = self._get_or_create_role(dev["role"], role_cache, dry_run)

                existing = Device.objects.filter(name=dev["name"]).first()
                if existing:
                    self.logger.warning(
                        f"Device '{dev['name']}' already exists (id={existing.id}) -- skipping create. "
                        f"Re-run with a rename or extend this job to update in place."
                    )
                    skipped += 1
                    continue

                if dev["has_password"] or dev["has_enable_password"]:
                    self.logger.info(
                        f"'{dev['name']}': credentials present in source sheet "
                        f"(username={dev['username']!r}) -- add these to Nautobot Secrets manually, "
                        f"NOT stored by this job."
                    )

                if dry_run:
                    self.logger.info(
                        f"Would create Device '{dev['name']}' "
                        f"(role={dev['role']}, location={loc_name}, "
                        f"type={manufacturer.name if manufacturer else '?'} {dev['model']}, "
                        f"platform={dev['platform_driver']}, "
                        f"ips={dev['ip_addresses']})"
                    )
                    created += 1
                    continue

                device = Device.objects.create(
                    name=dev["name"],
                    device_type=device_type,
                    role=role,
                    location=location,
                    status=active_status,
                    platform=platform,
                )

                mgmt_intf = Interface.objects.create(
                    device=device,
                    name="Management0",
                    type="virtual",
                    status=active_status,
                )

                namespace = Namespace.objects.get_or_create(name="Global")[0]
                for ip, prefix in dev["ip_addresses"]:
                    net = ipaddress.ip_network(f"{ip}/{prefix}", strict=False)
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

                    addr = f"{ip}/{prefix}"
                    ip_obj = IPAddress.objects.filter(host=ip, parent__namespace=namespace).first()
                    ip_created = False
                    if not ip_obj:
                        ip_obj = IPAddress.objects.create(
                            address=addr,
                            namespace=namespace,
                            status=active_status,
                        )
                        ip_created = True
                    if not ip_created:
                        self.logger.warning(
                            f"IP {ip} already exists in Nautobot -- reusing/assigning to "
                            f"'{dev['name']}' Management0 (check for real-world duplicate IP use)."
                        )
                    mgmt_intf.ip_addresses.add(ip_obj)
                    if device.primary_ip4_id is None:
                        device.primary_ip4 = ip_obj
                        device.save()

                self.logger.info(f"Created Device '{dev['name']}' at {loc_name}.")
                created += 1

            except Exception as exc:  # noqa: BLE001 -- surface any row failure and keep going
                self.logger.failure(f"Failed on device '{dev.get('name')}': {exc}")
                errors += 1

        self.logger.info(
            f"Done. created/would-create={created}, skipped(existing)={skipped}, errors={errors}."
        )

    # -- helpers -----------------------------------------------------------

    def _get_status(self, name):
        status = Status.objects.filter(name=name).first()
        if not status:
            self.logger.warning(f"Status '{name}' not found -- using first available Status.")
            status = Status.objects.first()
        return status

    def _get_or_create_location_type(self, name):
        lt, _ = LocationType.objects.get_or_create(name=name)
        return lt

    def _get_or_create_location(self, name, location_type, cache, active_status, dry_run):
        if name in cache:
            return cache[name]
        if dry_run:
            cache[name] = None
            return None
        loc, made = Location.objects.get_or_create(
            name=name,
            defaults={"location_type": location_type, "status": active_status},
        )
        if made:
            self.logger.info(f"Created Location '{name}'.")
        cache[name] = loc
        return loc

    def _get_or_create_manufacturer(self, make, cache, dry_run):
        make = make or "Unknown"
        if make in cache:
            return cache[make]
        if dry_run:
            cache[make] = None
            return None
        mfr, made = Manufacturer.objects.get_or_create(name=make)
        if made:
            self.logger.info(f"Created Manufacturer '{make}'.")
        cache[make] = mfr
        return mfr

    def _get_or_create_platform(self, make, driver, manufacturer, cache, dry_run):
        key = f"{make}:{driver}"
        if key in cache:
            return cache[key]
        if dry_run or not driver:
            cache[key] = None
            return None
        platform, made = Platform.objects.get_or_create(
            network_driver=driver,
            defaults={"name": driver, "manufacturer": manufacturer},
        )
        if made:
            self.logger.info(f"Created Platform '{driver}'.")
        cache[key] = platform
        return platform

    def _get_or_create_device_type(self, manufacturer, model, cache, dry_run):
        model = model or "Unknown"
        key = f"{manufacturer}:{model}" if manufacturer else model
        if key in cache:
            return cache[key]
        if dry_run or not manufacturer:
            cache[key] = None
            return None
        dt, made = DeviceType.objects.get_or_create(
            manufacturer=manufacturer,
            model=model,
        )
        if made:
            self.logger.info(f"Created DeviceType '{manufacturer.name} {model}'.")
        cache[key] = dt
        return dt

    def _get_or_create_role(self, role_name, cache, dry_run):
        if role_name in cache:
            return cache[role_name]
        if dry_run:
            cache[role_name] = None
            return None
        role, made = Role.objects.get_or_create(name=role_name)
        role.content_types.add(ContentType.objects.get_for_model(Device))
        if made:
            self.logger.info(f"Created Role '{role_name}'.")
        cache[role_name] = role
        return role


register_jobs(ImportCOENetworkInventory)
