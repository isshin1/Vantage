#!/usr/bin/env python3
"""Vantage privileged helper.

Run as root via pkexec — never invoke directly. It performs the small set of
whitelisted sysfs writes the UI needs and nothing else, so the unprivileged
front-ends can't write arbitrary paths. polkit (see the .policy file) gates
execution and, with auth_admin_keep, prompts only once per session.

Usage:
  vantage-helper auth                      # no-op; only used to prime polkit at launch
  vantage-helper set <key> <value>         # write a whitelisted VPC/platform control
  vantage-helper gpu-mode <mode>           # switch GPU mode (MUX)
  vantage-helper bios-get <attr>           # read a whitelisted think_lmi BIOS attr
  vantage-helper bios-set <attr> <value>   # write a whitelisted think_lmi BIOS attr
"""
import sys

from . import gpu
from . import hardware as hw

FALSEY = {"0", "false", "off", "no", ""}

# Accepted values per VPC attribute. Keys are already whitelisted (so no path
# traversal is possible), but pinning the values too keeps the privileged write
# from poking unexpected bytes into the platform driver.
VPC_ALLOWED = {
    "conservation_mode": {"0", "1"},
    "usb_charging":       {"0", "1"},
    "fn_lock":            {"0", "1"},
    "fan_mode":           {"0", "1", "2", "4"},
}

# think_lmi BIOS attributes the helper may read/write. Pinning the attribute
# names keeps the privileged process from touching arbitrary firmware settings;
# the value is then validated against the attribute's own possible_values.
BIOS_ATTRS = {"AlwaysOnUSB", "FnKeyAsPrimary"}


def _set(key, value):
    if key in hw.VPC_ATTRS:
        allowed = VPC_ALLOWED.get(key)
        if allowed is not None and value not in allowed:
            sys.stderr.write(
                "vantage-helper: rejected value %r for %s\n" % (value, key))
            return False
        return hw.write_attr(hw.VPC_ATTRS[key], value)
    if key == "charge_limit":
        # ThinkPad battery charge cap. "1" caps at ~80% to extend lifespan;
        # "0" restores an effectively-unlimited window.
        if value not in {"0", "1"}:
            sys.stderr.write(
                "vantage-helper: rejected value %r for charge_limit\n" % value)
            return False
        if value == "1":
            return hw.write_charge_thresholds(75, 80)
        return hw.write_charge_thresholds(95, 100)
    if key == "platform_profile":
        # write_platform_profile validates the value against the kernel's
        # platform_profile_choices, so no separate whitelist is needed.
        return hw.write_platform_profile(value)
    if key == "fan_level":
        # write_fan_level validates against hw.FAN_LEVELS and the fan_control
        # gate, so the value is already constrained.
        return hw.write_fan_level(value)
    if key == "touchpad_inhibited":
        return hw.write_touchpad_inhibited(value.lower() not in FALSEY)
    if key == "kbd_backlight":
        try:
            return hw.write_kbd_backlight(int(value))
        except ValueError:
            return False
    sys.stderr.write("vantage-helper: unknown key %r\n" % key)
    return None


def main(argv):
    if not argv:
        sys.stderr.write(
            "usage: vantage-helper auth | set <key> <value> | gpu-mode <mode> | "
        "bios-get <attr> | bios-set <attr> <value>\n")
        return 2
    if argv[0] == "auth":
        return 0
    if argv[0] == "serial":
        val = hw.read_dmi_serial()
        if val is None:
            return 1
        sys.stdout.write(val + "\n")
        return 0
    if argv[0] == "set" and len(argv) == 3:
        ok = _set(argv[1], argv[2])
        if ok is None:
            return 2
        return 0 if ok else 1
    if argv[0] == "gpu-mode" and len(argv) == 2:
        if argv[1] not in gpu.MODES:
            sys.stderr.write("vantage-helper: rejected gpu mode %r\n" % argv[1])
            return 2
        return 0 if gpu.set_mode(argv[1]) else 1
    if argv[0] == "bios-get" and len(argv) == 2:
        if argv[1] not in BIOS_ATTRS:
            sys.stderr.write("vantage-helper: bios attr not allowed: %r\n" % argv[1])
            return 2
        val = hw.read_bios_attr(argv[1])
        if val is None:
            return 1
        sys.stdout.write(val + "\n")
        return 0
    if argv[0] == "bios-set" and len(argv) == 3:
        if argv[1] not in BIOS_ATTRS:
            sys.stderr.write("vantage-helper: bios attr not allowed: %r\n" % argv[1])
            return 2
        return 0 if hw.write_bios_attr(argv[1], argv[2]) else 1
    sys.stderr.write(
        "usage: vantage-helper auth | set <key> <value> | gpu-mode <mode> | "
        "bios-get <attr> | bios-set <attr> <value>\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
