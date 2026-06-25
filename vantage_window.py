#!/usr/bin/env python3
"""Vantage settings window (GTK4 + libadwaita).

A native settings panel for Lenovo hardware controls, in the spirit of Lenovo
Vantage / PlasmaVantage. Privileged sysfs writes go through the polkit-gated
`pkexec vantage-helper` (shared with the tray via vantage_client); session-level
controls (microphone, Wi-Fi, power profile) run directly. Controls auto-hide
when the underlying hardware isn't present, so the same binary suits any
Lenovo model.
"""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gtk, GLib, Gio  # noqa: E402

from vantage_client import (  # noqa: E402
    Vantage, FAN_MODES, FAN_LABELS, CONSERVATION_LIMIT_PCT)

APP_ID = "org.vantage.Vantage"

# power-profiles-daemon profile id -> human label.
PROFILE_LABELS = {
    "power-saver": "Power Saver",
    "low-power": "Low Power",
    "balanced": "Balanced",
    "performance": "Performance",
}


class VantageWindow(Adw.ApplicationWindow):
    def __init__(self, app, backend):
        super().__init__(application=app, title="Lenovo Vantage")
        self.backend = backend
        self.set_default_size(420, 640)

        self._guard = False        # suppress handlers during programmatic updates
        self._updaters = []        # callables that re-sync a widget from state
        self._profiles = []        # raw power-profile ids, in display order
        self.state = backend.get_state()

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        toolbar = Adw.ToolbarView()
        self.toasts.set_child(toolbar)

        header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text("Refresh")
        refresh.connect("clicked", lambda _b: self.refresh())
        header.pack_start(refresh)
        about = Gtk.Button(icon_name="dialog-information-symbolic")
        about.set_tooltip_text("About this device")
        about.connect("clicked", self._show_about)
        header.pack_end(about)
        toolbar.add_top_bar(header)

        self.page = Adw.PreferencesPage()
        toolbar.set_content(self.page)

        self._build()

        # Single polkit prompt at launch; auth_admin_keep covers later writes.
        GLib.idle_add(self._authenticate)

    # ---- auth ----------------------------------------------------------------
    def _authenticate(self):
        if not self.backend.authenticate():
            self._toast("Not authorized — changes may prompt for a password.")
        return False

    # ---- build ---------------------------------------------------------------
    def _build(self):
        st = self.state

        power = self._group("Power & Battery")
        if "conservation_mode" in st:
            self._switch(power, "conservation_mode", "Conservation Mode",
                         "Caps charge at ~%d%% to extend battery lifespan"
                         % CONSERVATION_LIMIT_PCT,
                         lambda s: s.get("conservation_mode") == "1",
                         lambda on: self.backend.set_vpc("conservation_mode", "1" if on else "0"))
        if "usb_charging" in st:
            self._switch(power, "usb_charging", "Always-On USB",
                         "Keep USB ports powered while suspended",
                         lambda s: s.get("usb_charging") == "1",
                         lambda on: self.backend.set_vpc("usb_charging", "1" if on else "0"))
        if "power_profile" in st:
            self._add_power_profile(power)
        if self.backend.battery_info():
            self._add_battery_health(power)
        self._maybe_add(power)

        thermal = self._group("Thermal")
        if "fan_mode" in st:
            self._add_fan_mode(thermal)
        if "fan_rpms" in st:
            self._add_fan_speed(thermal)
        self._maybe_add(thermal)

        inp = self._group("Input")
        if "fn_lock" in st:
            # fn_lock sysfs: "1" == locked-off; show On == multimedia-without-Fn.
            self._switch(inp, "fn_lock", "Fn Lock",
                         "Use multimedia keys without holding Fn",
                         lambda s: s.get("fn_lock") == "0",
                         lambda on: self.backend.set_vpc("fn_lock", "0" if on else "1"))
        if "touchpad_inhibited" in st:
            self._switch(inp, "touchpad_inhibited", "Touchpad",
                         "Enable the laptop touchpad",
                         lambda s: s.get("touchpad_inhibited") != "1",
                         lambda on: self.backend.set_touchpad_enabled(on))
        if "kbd_backlight" in st:
            self._add_kbd_backlight(inp)
        self._maybe_add(inp)

        privacy = self._group("Privacy")
        if "mic_on" in st:
            self._switch(privacy, "mic_on", "Microphone",
                         "Unmute the default input device",
                         lambda s: s.get("mic_on") == "1",
                         lambda on: self.backend.set_mic_on(on))
        self._maybe_add(privacy)

        network = self._group("Network")
        if "wifi_on" in st:
            self._switch(network, "wifi_on", "Wi-Fi",
                         "Enable the wireless radio",
                         lambda s: s.get("wifi_on") == "1",
                         lambda on: self.backend.set_wifi_on(on))
        self._maybe_add(network)

    # ---- widget builders -----------------------------------------------------
    def _group(self, title):
        # PreferencesGroup titles are parsed as Pango markup; escape '&' etc.
        return Adw.PreferencesGroup(title=GLib.markup_escape_text(title))

    def _maybe_add(self, group):
        """Add the group to the page only if it ended up with rows."""
        # PreferencesGroup has no public child count; track via a marker attr.
        if getattr(group, "_has_rows", False):
            self.page.add(group)

    def _switch(self, group, key, title, subtitle, active_fn, on_toggle):
        group._has_rows = True
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.set_active(active_fn(self.state))
        row.connect("notify::active",
                    lambda r, _p: self._handle(lambda: on_toggle(r.get_active())))
        group.add(row)
        self._updaters.append(
            lambda s: self._sync(row, "active", active_fn(s)))

    def _combo(self, group, key, title, subtitle, labels, index_fn, on_select):
        group._has_rows = True
        row = Adw.ComboRow(title=title, subtitle=subtitle,
                           model=Gtk.StringList.new(labels))
        idx = index_fn(self.state)
        if idx is not None:
            row.set_selected(idx)
        row.connect("notify::selected",
                    lambda r, _p: self._handle(lambda: on_select(r.get_selected())))
        group.add(row)

        def upd(s):
            i = index_fn(s)
            if i is not None:
                self._sync(row, "selected", i)
        self._updaters.append(upd)

    def _add_power_profile(self, group):
        choices = [c for c in self.state["power_profile_choices"].split(",") if c]
        self._profiles = choices
        labels = [PROFILE_LABELS.get(c, c.title()) for c in choices]
        self._combo(group, "power_profile", "Power Profile",
                    "System performance vs. battery",
                    labels,
                    lambda s: self._profiles.index(s["power_profile"])
                    if s.get("power_profile") in self._profiles else None,
                    lambda i: self.backend.set_power_profile(self._profiles[i]))

    def _add_fan_mode(self, group):
        group._has_rows = True
        labels = [lbl for _v, lbl in FAN_MODES]
        row = Adw.ComboRow(title="Fan Mode", subtitle="Cooling profile",
                           model=Gtk.StringList.new(labels))

        def current_idx(s):
            v = s.get("fan_mode")
            return labels.index(FAN_LABELS[v]) if v in FAN_LABELS else None

        idx = current_idx(self.state)
        if idx is not None:
            row.set_selected(idx)

        def on_selected(r, _p):
            if self._guard:
                return
            i = r.get_selected()
            val, _lbl = FAN_MODES[i]
            if val == "2":
                # Dust Cleaning is transient: EC runs the cycle then resets fan_mode
                # in sysfs back to the previous value, so a normal refresh would
                # immediately revert the combo. Run the command, show a toast, and
                # restore the previous selection ourselves without a sysfs round-trip.
                self.backend.set_vpc("fan_mode", "2")
                self._toast("Dust cleaning started — fans will return to normal shortly")
                prev = current_idx(self.state)
                if prev is not None:
                    self._guard = True
                    row.set_selected(prev)
                    self._guard = False
            else:
                # The EC takes hundreds of ms to settle fan_mode sysfs after a
                # write and may pass through unmapped intermediate values (e.g.
                # "3"). An immediate refresh would read stale/garbage and revert
                # the combo. Skip it — the UI already shows the right selection.
                self.backend.set_vpc("fan_mode", val)

        row.connect("notify::selected", on_selected)
        group.add(row)

        def upd(s):
            i = current_idx(s)
            if i is not None:
                self._sync(row, "selected", i)
        self._updaters.append(upd)

    def _add_kbd_backlight(self, group):
        try:
            mx = int(self.state.get("kbd_backlight_max", "2"))
        except ValueError:
            mx = 2
        if mx <= 2:
            labels = ["Off", "Low", "High"][:mx + 1]
        else:
            labels = ["Off"] + ["Level %d" % i for i in range(1, mx + 1)]
        self._combo(group, "kbd_backlight", "Keyboard Backlight",
                    "Key illumination level",
                    labels,
                    lambda s: max(0, min(mx, int(s.get("kbd_backlight", "0")))),
                    lambda i: self.backend.set_kbd_backlight(i))

    def _add_battery_health(self, group):
        group._has_rows = True
        self._batt_row = Adw.ActionRow(title="Battery Health",
                                       subtitle=self._batt_text(self.backend.battery_info()))
        self._batt_row.add_css_class("property")
        group.add(self._batt_row)

    @staticmethod
    def _batt_text(info):
        if not info:
            return "Unavailable"
        parts = []
        if "health_pct" in info:
            parts.append("%.1f%% of design capacity" % info["health_pct"])
        if "cycle_count" in info:
            parts.append("%d cycles" % info["cycle_count"])
        if info.get("capacity"):
            charge = "%s%% charged" % info["capacity"]
            status = (info.get("status") or "").strip().lower()
            if status and status not in ("unknown", "full"):
                charge += " (%s)" % status
            parts.append(charge)
        return " · ".join(parts) or "Unavailable"

    def _add_fan_speed(self, group):
        group._has_rows = True
        self._fan_row = Adw.ActionRow(title="Fan Speed",
                                      subtitle=self._fan_text(self.backend.fan_rpms()))
        self._fan_row.add_css_class("property")
        group.add(self._fan_row)
        # Live tachometer poll (every 2s) independent of the change-driven refresh.
        GLib.timeout_add_seconds(2, self._poll_fans)

    @staticmethod
    def _fan_text(fans):
        if not fans:
            return "Unavailable"
        parts = ["%s %d RPM" % (lbl or "Fan %d" % i, rpm)
                 for i, (lbl, rpm) in enumerate(fans, 1)]
        text = " · ".join(parts)
        if all(rpm == 0 for _l, rpm in fans):
            text += "  (idle)"
        return text

    def _poll_fans(self):
        row = getattr(self, "_fan_row", None)
        if row is None:
            return False  # row gone; stop polling
        row.set_subtitle(self._fan_text(self.backend.fan_rpms()))
        return True  # keep the timer alive

    # ---- about ---------------------------------------------------------------
    def _show_about(self, *_):
        info = self.backend.system_info()
        dialog = Adw.Dialog()
        dialog.set_title("About This Device")
        dialog.set_content_width(460)

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="System")

        def row(title, value):
            r = Adw.ActionRow(title=title, subtitle=value or "Unknown")
            r.set_subtitle_selectable(True)
            group.add(r)
            return r

        row("Device", info["device"])
        if info.get("machine_type"):
            row("Machine Type", info["machine_type"])
        row("Processor", info["cpu"])
        row("Memory", info["ram"])
        row("Operating System", info["os"])
        row("Kernel", info["kernel"])
        row("Hostname", info["hostname"])
        serial_row = row("Serial Number", "Loading…")

        page.add(group)
        tv.set_content(page)
        dialog.set_child(tv)
        dialog.present(self)

        # Serial is root-only; fetch it after the dialog is up so it never blocks
        # opening (the launch-time auth is cached, so this won't re-prompt).
        def fill_serial():
            serial_row.set_subtitle(self.backend.serial() or "Unavailable")
            return False
        GLib.idle_add(fill_serial)

    # ---- state plumbing ------------------------------------------------------
    def _sync(self, row, prop, value):
        """Set a widget property without re-triggering its handler."""
        if row.get_property(prop) != value:
            row.set_property(prop, value)

    def _handle(self, action):
        if self._guard:
            return
        action()
        GLib.idle_add(self.refresh)

    def refresh(self, *_):
        self.state = self.backend.get_state()
        self._guard = True
        for upd in self._updaters:
            try:
                upd(self.state)
            except Exception:
                pass
        if getattr(self, "_fan_row", None) is not None:
            self._fan_row.set_subtitle(self._fan_text(self.backend.fan_rpms()))
        if getattr(self, "_batt_row", None) is not None:
            self._batt_row.set_subtitle(self._batt_text(self.backend.battery_info()))
        self._guard = False
        return False

    def _toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=msg))


class VantageApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.backend = Vantage()
        self.win = None

    def do_activate(self):
        if self.win is None:
            self.win = VantageWindow(self, self.backend)
        self.win.present()


def main():
    app = VantageApp()
    return app.run(None)


if __name__ == "__main__":
    raise SystemExit(main())
