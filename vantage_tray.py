#!/usr/bin/env python3
"""Vantage tray indicator.

A StatusNotifierItem tray app (works on wlroots/Sway/Hyprland and KDE/GNOME via
a host that supports the tray protocol) exposing quick toggles, plus an
"Open Vantage…" item that launches the full GTK4 settings window
(vantage_window.py). All backend access goes through the shared client
(vantage_client.Vantage), so the tray and window behave identically.

The tray uses AyatanaAppIndicator3 (GTK3) because GTK4 has no tray API; the
settings window is GTK4/libadwaita and runs as a separate process.
"""
import os
import shutil
import subprocess
import sys

import gi
gi.require_version("Gtk", "3.0")

# Prefer the maintained Ayatana fork; fall back to legacy Canonical libappindicator.
try:
    gi.require_version("AyatanaAppIndicator3", "0.1")
    from gi.repository import AyatanaAppIndicator3 as AppIndicator
except (ValueError, ImportError):
    gi.require_version("AppIndicator3", "0.1")
    from gi.repository import AppIndicator3 as AppIndicator

from gi.repository import Gtk, GLib  # noqa: E402

from vantage_client import (  # noqa: E402
    Vantage, FAN_MODES, FAN_LABELS, CONSERVATION_LIMIT_PCT)

APP_ID = "vantage-tray"
# Symbolic (monochrome) icon so the tray host recolors it to match the panel's
# other symbolic icons, rather than showing the colourful app logo.
ICON = "vantage-symbolic"

PROFILE_LABELS = {
    "power-saver": "Power Saver", "low-power": "Low Power",
    "balanced": "Balanced", "performance": "Performance",
}
KBD_LABELS = ["Off", "Low", "High"]


class Tray:
    def __init__(self):
        self.backend = Vantage()
        self.ind = AppIndicator.Indicator.new(
            APP_ID, ICON, AppIndicator.IndicatorCategory.HARDWARE)
        self.ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self.ind.set_title("Lenovo Vantage")
        self._guard = False
        GLib.idle_add(self.backend.authenticate)
        self.rebuild()

    # ---- window launcher -----------------------------------------------------
    @staticmethod
    def _open_window(*_):
        if shutil.which("vantage"):
            cmd = ["vantage"]
        else:
            script = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                  "vantage_window.py")
            cmd = [sys.executable, script]
        try:
            subprocess.Popen(cmd)
        except OSError:
            pass

    # ---- menu construction ---------------------------------------------------
    def rebuild(self, *_):
        self._guard = True
        st = self.backend.get_state()
        menu = Gtk.Menu()

        def toggle(label, active, handler, on="🟢 ON ", off="⚪ OFF"):
            # Tray hosts render only entry text (they ignore dbusmenu check-state),
            # so on/off is drawn as a switch-style glyph inside the label.
            item = Gtk.MenuItem(label="%-18s %s" % (label, on if active else off))
            item.connect("activate", lambda _w: handler(not active))
            menu.append(item)

        if "conservation_mode" in st:
            toggle("Conservation (~%d%%)" % CONSERVATION_LIMIT_PCT,
                   st["conservation_mode"] == "1",
                   lambda new: self._set_vpc_bool("conservation_mode", new))
        if "usb_charging" in st:
            toggle("Always-On USB", st["usb_charging"] == "1",
                   lambda new: self._set_vpc_bool("usb_charging", new))
        if "power_profile" in st:
            self._add_profile_menu(menu, st)
        if "fan_mode" in st:
            self._add_fan_menu(menu, st["fan_mode"])
        if "fn_lock" in st:
            toggle("FN Lock", st["fn_lock"] == "0", self._set_fn_lock)
        if "touchpad_inhibited" in st:
            toggle("Touchpad", st["touchpad_inhibited"] != "1",
                   self._set_touchpad_enabled)
        if "kbd_backlight" in st:
            self._add_kbd_menu(menu, st)
        if "mic_on" in st:
            toggle("Microphone", st["mic_on"] == "1", self._set_mic)
        if "wifi_on" in st:
            toggle("Wi-Fi", st["wifi_on"] == "1", self._set_wifi)

        menu.append(Gtk.SeparatorMenuItem())
        win_item = Gtk.MenuItem(label="Open Vantage…")
        win_item.connect("activate", self._open_window)
        menu.append(win_item)
        refresh = Gtk.MenuItem(label="Refresh")
        refresh.connect("activate", self.rebuild)
        menu.append(refresh)
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", lambda _w: Gtk.main_quit())
        menu.append(quit_item)

        menu.show_all()
        self.ind.set_menu(menu)
        self._guard = False

    def _add_fan_menu(self, menu, value):
        current = FAN_LABELS.get(value, value)
        fans = self.backend.fan_rpms()
        rpm = ""
        if fans:
            rpm = " (%s)" % " · ".join("%d RPM" % r for _l, r in fans)
        item = Gtk.MenuItem(label="Fan Mode: %s%s" % (current, rpm))
        sub = Gtk.Menu()
        for val, label in FAN_MODES:
            prefix = "▶ " if current == label else "   "
            r = Gtk.MenuItem(label=prefix + label)
            r.connect("activate", lambda _w, v=val: self._set_fan(v))
            sub.append(r)
        item.set_submenu(sub)
        menu.append(item)

    def _add_profile_menu(self, menu, st):
        profiles = [p for p in st["power_profile_choices"].split(",") if p]
        current = st.get("power_profile")
        item = Gtk.MenuItem(
            label="Power Profile: %s" % PROFILE_LABELS.get(current, current or "?"))
        sub = Gtk.Menu()
        for p in profiles:
            prefix = "▶ " if p == current else "   "
            r = Gtk.MenuItem(label=prefix + PROFILE_LABELS.get(p, p.title()))
            r.connect("activate", lambda _w, name=p: self._set_profile(name))
            sub.append(r)
        item.set_submenu(sub)
        menu.append(item)

    def _add_kbd_menu(self, menu, st):
        try:
            mx = int(st.get("kbd_backlight_max", "2"))
            cur = int(st.get("kbd_backlight", "0"))
        except ValueError:
            mx, cur = 2, 0
        labels = KBD_LABELS[:mx + 1] if mx <= 2 \
            else ["Off"] + ["Level %d" % i for i in range(1, mx + 1)]
        item = Gtk.MenuItem(label="Keyboard Backlight: %s"
                            % (labels[cur] if cur < len(labels) else cur))
        sub = Gtk.Menu()
        for i, label in enumerate(labels):
            prefix = "▶ " if i == cur else "   "
            r = Gtk.MenuItem(label=prefix + label)
            r.connect("activate", lambda _w, lvl=i: self._set_kbd(lvl))
            sub.append(r)
        item.set_submenu(sub)
        menu.append(item)

    # ---- handlers ------------------------------------------------------------
    def _after(self):
        GLib.idle_add(self.rebuild)

    def _set_vpc_bool(self, attr, new):
        if self._guard:
            return
        self.backend.set_vpc(attr, "1" if new else "0")
        self._after()

    def _set_fn_lock(self, new):
        if self._guard:
            return
        self.backend.set_vpc("fn_lock", "0" if new else "1")
        self._after()

    def _set_touchpad_enabled(self, new):
        if self._guard:
            return
        self.backend.set_touchpad_enabled(new)
        self._after()

    def _set_fan(self, val):
        if self._guard:
            return
        self.backend.set_vpc("fan_mode", val)
        if val != "2":
            # EC sysfs readback settles after ~300ms+ and passes through unmapped
            # intermediate values; skip immediate rebuild to avoid showing stale state.
            return
        self._after()

    def _set_profile(self, name):
        if self._guard:
            return
        self.backend.set_power_profile(name)
        self._after()

    def _set_kbd(self, level):
        if self._guard:
            return
        self.backend.set_kbd_backlight(level)
        self._after()

    def _set_mic(self, new):
        if self._guard:
            return
        self.backend.set_mic_on(new)
        self._after()

    def _set_wifi(self, new):
        if self._guard:
            return
        self.backend.set_wifi_on(new)
        self._after()


def main():
    Tray()
    Gtk.main()


if __name__ == "__main__":
    main()
