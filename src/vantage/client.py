"""Shared Vantage client.

Reads hardware state directly from sysfs (reads are unprivileged) and performs
privileged writes via `pkexec vantage-helper`, which is gated by polkit
(auth_admin_keep -> one prompt per session). Session-level controls (microphone,
Wi-Fi, power profile) run as the user without any prompt.

The GTK4 window (window.py) uses this for all hardware access. The embedded SNI
tray (tray.py) also calls back into this for quick toggles.

State is returned as a flat dict of strings; a key is present only when the
underlying control exists on this machine, so front-ends just render the keys
they get back.
"""
import logging
import os
import platform
import re
import shutil
import subprocess
import sys
import threading

from gi.repository import GLib, Gio

from . import gpu
from . import hardware as hw

log = logging.getLogger("vantage.client")


def run_in_thread(work, on_done):
    """Run blocking ``work()`` off the GLib main loop.

    The result (or, on failure, the caught exception) is delivered to
    ``on_done(result)`` back on the main loop, so callers can touch GTK/D-Bus
    safely. Used to keep pkexec/pactl/nmcli spawns from freezing the UI.
    """
    def target():
        try:
            result = work()
        except BaseException as exc:  # report, never let the worker thread die noisily
            log.exception("background task failed")
            result = exc
        GLib.idle_add(_deliver, on_done, result)

    threading.Thread(target=target, daemon=True).start()


def _deliver(on_done, result):
    on_done(result)
    return False   # one-shot idle source

# Privileged helper executable name. Located on PATH at call time so it works
# regardless of the install prefix; the resolved absolute path must match the
# polkit action's exec.path annotation (both come from Meson's bindir).
HELPER = "vantage-helper"

# Fan-mode value <-> label. "133" is the firmware default that maps to silent.
FAN_MODES = [
    ("0", "Super Silent"),
    ("1", "Standard"),
    ("2", "Dust Cleaning"),
    ("4", "Efficient Thermal Dissipation"),
]
FAN_LABELS = {"133": "Super Silent", "0": "Super Silent", "1": "Standard",
              "2": "Dust Cleaning", "4": "Efficient Thermal Dissipation"}

# ThinkPad manual fan levels (thinkpad_acpi /proc/acpi/ibm/fan), display order.
FAN_LEVELS = hw.FAN_LEVELS

# Lenovo (VPC2004) conservation mode is a fixed firmware cap; the threshold
# isn't exposed via sysfs, so we surface the value. Newer firmware (e.g. Yoga
# Pro 7i Gen 11) holds the battery at 80%; older ideapads used ~60%.
CONSERVATION_LIMIT_PCT = 80

class VantageConfig:
    """Persistent user preferences, stored via GSettings.

    Backed by the org.vantage.Vantage schema (see the .gschema.xml under data/).
    The schema must be compiled and installed into a GSettings schema dir, or
    Gio.Settings.new() aborts — handled by `meson install`; for an uninstalled
    run point GSETTINGS_SCHEMA_DIR at a dir holding a compiled schema.
    """

    SCHEMA_ID = "org.vantage.Vantage"

    def __init__(self):
        self._settings = Gio.Settings.new(self.SCHEMA_ID)

    def get_run_in_background(self) -> bool:
        return self._settings.get_boolean("run-in-background")

    def set_run_in_background(self, value: bool):
        self._settings.set_boolean("run-in-background", bool(value))


def have(cmd):
    return shutil.which(cmd) is not None


# pkexec spawns a polkit/GLib stack that may show a password dialog, so its
# timeout is generous; plain session tools (pactl/nmcli/...) never prompt and
# should return promptly. An unbounded wait is what lets a hung firmware write
# (think_lmi BIOS attributes can block in the EC) leak a stuck root process per
# attempt until the session's thread/process limit is exhausted — at which point
# the *next* pkexec can't even create its 'gmain' thread.
HELPER_TIMEOUT = 90
TOOL_TIMEOUT = 15


def run(*args, timeout=None):
    try:
        return subprocess.run(args, capture_output=True, text=True,
                              check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        # Synthesize a failed result so callers see a clean reason instead of
        # whatever the killed child left on its stderr.
        return subprocess.CompletedProcess(
            args, returncode=124, stdout="",
            stderr="timed out after %ss (no response from hardware)" % timeout)
    except OSError:
        return None


class Vantage:
    """Backend facade. Construct once; call get_state()/set_*()."""

    # ---- privileged helper plumbing ------------------------------------------
    @staticmethod
    def _helper(*args):
        """Build the `pkexec vantage-helper ...` argv (dev falls back to source)."""
        installed = shutil.which(HELPER)
        if installed:
            return ["pkexec", installed, *args]
        # Uninstalled run: invoke the helper module from the source tree. The
        # package's parent dir (…/src) is two levels up from this file
        # (src/vantage/client.py); putting it on sys.path lets the root process
        # resolve `from vantage import helper`.
        pkg_parent = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        bootstrap = (
            "import sys; sys.path.insert(1, %r); from vantage import helper; "
            "raise SystemExit(helper.main(sys.argv[1:]))" % pkg_parent
        )
        return ["pkexec", sys.executable, "-c", bootstrap, *args]

    def _run_helper(self, *args, timeout=HELPER_TIMEOUT):
        log.debug("helper: %s", " ".join(args))
        r = run(*self._helper(*args), timeout=timeout)
        if r is None or r.returncode != 0:
            rc = "no-process" if r is None else r.returncode
            err = "" if r is None else (r.stderr or "").strip()
            log.error("helper %s failed (rc=%s): %s", " ".join(args), rc, err)
            if err:
                self.notify("Failed: %s" % err)
            return False
        return True

    def authenticate(self):
        """Prime polkit once at launch (auth_admin_keep caches the grant)."""
        # The one deliberate password prompt — don't time it out from under a
        # user who's reaching for their keyboard.
        return self._run_helper("auth", timeout=None)

    def serial(self):
        """Read the root-only system serial number via the helper, or None."""
        r = run(*self._helper("serial"), timeout=HELPER_TIMEOUT)
        if r is not None and r.returncode == 0:
            return (r.stdout or "").strip() or None
        return None

    def notify(self, msg):
        if have("notify-send"):
            run("notify-send", "Vantage", msg, timeout=TOOL_TIMEOUT)

    # ---- async wrappers (keep blocking work off the GTK main loop) ------------
    def get_state_async(self, on_done):
        """Compute get_state() off-thread; on_done(dict|Exception) on main loop."""
        run_in_thread(self.get_state, on_done)

    def call_async(self, work, on_done=None):
        """Run a blocking backend call (write, pkexec, serial …) off-thread.

        on_done(result) fires on the main loop once it completes; pass None to
        fire-and-forget. ``result`` is the call's return value, or the caught
        exception on failure.
        """
        run_in_thread(work, on_done if on_done is not None else (lambda _r: None))

    # ---- combined state (unprivileged reads) ---------------------------------
    def get_state(self):
        """Return {key: str} for every control available on this machine."""
        state = {}
        for key, attr in hw.VPC_ATTRS.items():
            val = hw.read_attr(attr)
            if val is not None:
                state[key] = val
        # ThinkPads expose a settable charge limit instead of conservation_mode.
        # Surfaced as an on/off cap (~80%); the two keys are mutually exclusive.
        ct = hw.charge_thresholds()
        if ct is not None:
            _start, end = ct
            state["charge_limit"] = "1" if end <= CONSERVATION_LIMIT_PCT else "0"
            state["charge_end_threshold"] = str(end)
        tp = hw.read_touchpad_inhibited()
        if tp is not None:
            state["touchpad_inhibited"] = tp
        kbd, kbd_max = hw.read_kbd_backlight()
        if kbd is not None:
            state["kbd_backlight"] = kbd
            state["kbd_backlight_max"] = kbd_max
        fans = hw.read_fan_rpms()
        if fans:
            state["fan_rpms"] = ",".join(str(rpm) for _lbl, rpm in fans)
        # ThinkPad manual fan level — only when thinkpad_acpi fan_control=1.
        if hw.fan_writable():
            lvl = hw.read_fan_level()
            if lvl is not None:
                state["fan_level"] = lvl

        # ThinkPad BIOS settings (think_lmi). Presence only here — the values are
        # root-only, so they're read lazily via the helper once authenticated.
        if hw.bios_attr_present("AlwaysOnUSB"):
            state["always_on_usb_bios"] = "present"
        if hw.bios_attr_present("FnKeyAsPrimary"):
            state["fnkey_primary_bios"] = "present"

        if self._mic_source():
            state["mic_on"] = "0" if self._mic_muted() else "1"
        if have("nmcli"):
            state["wifi_on"] = "1" if self._wifi_on() else "0"
        cur, choices = self._power_profile()
        if cur is not None:
            state["power_profile"] = cur
            state["power_profile_choices"] = ",".join(choices)
        if gpu.available():
            state["gpu_mode"] = gpu.current_mode()
            state["gpu_applied"] = gpu.applied_mode()
        return state

    # ---- privileged writes (via pkexec helper) -------------------------------
    def set_vpc(self, attr, value):
        return self._run_helper("set", attr, str(value))

    def set_charge_limit(self, on):
        return self._run_helper("set", "charge_limit", "1" if on else "0")

    def set_fan_level(self, level):
        return self._run_helper("set", "fan_level", str(level))

    # ---- ThinkPad BIOS settings (think_lmi, root via helper) -----------------
    def get_bios_attr(self, attr):
        """Read a BIOS attribute's current value via the helper, or None."""
        r = run(*self._helper("bios-get", attr), timeout=HELPER_TIMEOUT)
        if r is not None and r.returncode == 0:
            return (r.stdout or "").strip() or None
        return None

    def set_bios_attr(self, attr, value):
        """Write a BIOS attribute via the helper. Returns True on success."""
        return self._run_helper("bios-set", attr, value)

    @staticmethod
    def bios_pending_reboot():
        return hw.bios_pending_reboot()

    @staticmethod
    def bios_admin_locked():
        return hw.bios_admin_locked()

    def set_touchpad_enabled(self, enabled):
        return self._run_helper("set", "touchpad_inhibited", "0" if enabled else "1")

    def set_kbd_backlight(self, level):
        return self._run_helper("set", "kbd_backlight", str(int(level)))

    def set_gpu_mode(self, mode):
        """Switch hybrid-graphics mode (integrated/hybrid). Needs a reboot.

        Returns True on success, or an error string on failure (falsy but
        informative — the window can display it directly in a toast).
        """
        r = run(*self._helper("gpu-mode", mode), timeout=HELPER_TIMEOUT)
        if r is None or r.returncode != 0:
            err = ("" if r is None else (r.stderr or "").strip())
            log.error("helper gpu-mode %s failed (rc=%s): %s", mode,
                      "no-process" if r is None else r.returncode, err)
            return err or False
        return True

    def reboot(self):
        """Reboot the machine via logind (prompts through its own polkit agent)."""
        run("systemctl", "reboot")

    # ---- session-level writes (no root) --------------------------------------
    def set_mic_on(self, on):
        run("pactl", "set-source-mute", "@DEFAULT_SOURCE@", "0" if on else "1",
            timeout=TOOL_TIMEOUT)

    def set_wifi_on(self, on):
        run("nmcli", "radio", "wifi", "on" if on else "off", timeout=TOOL_TIMEOUT)

    def set_power_profile(self, name):
        # power-profiles-daemon owns platform_profile when present; otherwise
        # write the ACPI sysfs directly (root, via the helper).
        if have("powerprofilesctl"):
            run("powerprofilesctl", "set", name, timeout=TOOL_TIMEOUT)
        else:
            self._run_helper("set", "platform_profile", name)

    # ---- session-level reads -------------------------------------------------
    @staticmethod
    def _mic_source():
        r = run("pactl", "get-source-mute", "@DEFAULT_SOURCE@", timeout=TOOL_TIMEOUT)
        return r is not None and r.returncode == 0

    @staticmethod
    def _mic_muted():
        r = run("pactl", "get-source-mute", "@DEFAULT_SOURCE@", timeout=TOOL_TIMEOUT)
        return r is not None and "yes" in (r.stdout or "")

    @staticmethod
    def _wifi_on():
        r = run("nmcli", "radio", "wifi", timeout=TOOL_TIMEOUT)
        return r is not None and "enabled" in (r.stdout or "")

    @staticmethod
    def _power_profile():
        """Return (current, [choices]).

        Prefer power-profiles-daemon; fall back to reading the ACPI
        platform_profile sysfs directly so the control still appears on
        machines without the daemon (e.g. a bare ThinkPad install).
        """
        if not have("powerprofilesctl"):
            return hw.read_platform_profile()
        r = run("powerprofilesctl", "list", timeout=TOOL_TIMEOUT)
        if r is None or r.returncode != 0:
            return None, []
        choices, current = [], None
        for line in (r.stdout or "").splitlines():
            m = re.match(r"\s*(\*?)\s*([a-z-]+):\s*$", line)
            if m:
                choices.append(m.group(2))
                if m.group(1) == "*":
                    current = m.group(2)
        return current, choices

    # ---- read-only telemetry -------------------------------------------------
    @staticmethod
    def fan_rpms():
        """Return [(label, rpm), ...] for each fan; live, unprivileged."""
        return hw.read_fan_rpms()

    @staticmethod
    def battery_info():
        """Return battery health/status dict, or None; live, unprivileged."""
        return hw.read_battery()

    @staticmethod
    def platform_family():
        """Return 'ideapad', 'thinkpad', or 'other' for display purposes."""
        return hw.platform_family()

    def system_info(self):
        """Return a dict of laptop info for the About view (serial is lazy)."""
        vendor = hw.read_dmi("sys_vendor")
        if vendor and vendor.isupper():
            vendor = vendor.title()
        model = hw.read_dmi("product_version") or hw.read_dmi("product_name")
        device = " ".join(p for p in (vendor, model) if p) or "Unknown"
        return {
            "device": device,
            "platform_family": self.platform_family(),
            "machine_type": hw.read_dmi("product_name"),
            "cpu": self._cpu_model(),
            "ram": self._ram_human(),
            "os": self._os_pretty(),
            "kernel": platform.release() or "Unknown",
            "hostname": platform.node() or "Unknown",
        }

    @staticmethod
    def _cpu_model():
        try:
            with open("/proc/cpuinfo") as fh:
                for line in fh:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return platform.processor() or "Unknown"

    @staticmethod
    def _ram_human():
        try:
            with open("/proc/meminfo") as fh:
                for line in fh:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return "%.1f GiB" % (kb / 1024 / 1024)
        except (OSError, ValueError, IndexError):
            pass
        return "Unknown"

    @staticmethod
    def _os_pretty():
        try:
            with open("/etc/os-release") as fh:
                for line in fh:
                    if line.startswith("PRETTY_NAME="):
                        return line.split("=", 1)[1].strip().strip('"')
        except OSError:
            pass
        return "Unknown"
