#!/usr/bin/env python3
"""Vantage settings window (GTK4 + libadwaita).

A native settings panel for Lenovo hardware controls, in the spirit of Lenovo
Vantage / PlasmaVantage. Privileged sysfs writes go through the polkit-gated
`pkexec vantage-helper` (shared with the tray via client.py); session-level
controls (microphone, Wi-Fi, power profile) run directly. Controls auto-hide
when the underlying hardware isn't present, so the same binary suits any
Lenovo model.

This module defines only the window; the GApplication and CLI entry point live
in main.py.
"""
import logging
from gettext import gettext as _

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, Gtk, GLib  # noqa: E402

from . import autostart  # noqa: E402
from .client import (  # noqa: E402
    FAN_MODES, FAN_LABELS, FAN_LEVELS, CONSERVATION_LIMIT_PCT)

# ThinkPad manual fan-level labels, aligned 1:1 with client.FAN_LEVELS
# (["auto", "0".."7", "full-speed"]).
FAN_LEVEL_LABELS = ([_("Auto")]
                    + [_("Level %d") % i for i in range(8)]
                    + [_("Full Speed")])

log = logging.getLogger("vantage.window")

# power-profiles-daemon profile id -> human label.
PROFILE_LABELS = {
    "power-saver": _("Power Saver"),
    "low-power": _("Low Power"),
    "balanced": _("Balanced"),
    "performance": _("Performance"),
}


class VantageWindow(Adw.ApplicationWindow):
    def __init__(self, app, backend, config):
        super().__init__(application=app, title=_("Lenovo Vantage"))
        self.backend = backend
        self._config = config
        self._sni    = None        # VantageSNI instance when active
        self.set_default_size(420, 640)

        self._guard = False        # suppress handlers during programmatic updates
        self._updaters = []        # callables that re-sync a widget from state
        self._profiles = []        # raw power-profile ids, in display order
        self._fan_timer_id = 0     # live fan poll source id (0 = stopped)
        self._fan_hold_id = 0      # watchdog re-arm source id (0 = stopped)
        self._fan_hold_level = None  # manual fan level to keep alive, or None
        self._bios_inits = []      # deferred BIOS value reads (run after auth)
        self.state = backend.get_state()

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        toolbar = Adw.ToolbarView()
        self.toasts.set_child(toolbar)

        header = Adw.HeaderBar()
        refresh = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh.set_tooltip_text(_("Refresh"))
        refresh.connect("clicked", lambda _b: self.refresh())
        header.pack_start(refresh)

        # Window-scoped actions backing the primary menu (Adw.ApplicationWindow
        # is not a GActionMap, so use an inserted action group).
        actions = Gio.SimpleActionGroup()
        for name, handler in (("preferences", self._show_settings),
                              ("device-info", self._show_device_info),
                              ("about", self._show_about)):
            act = Gio.SimpleAction.new(name, None)
            act.connect("activate", handler)
            actions.add_action(act)
        self.insert_action_group("win", actions)

        menu = Gio.Menu()
        menu.append(_("Preferences"), "win.preferences")
        menu.append(_("Device Information"), "win.device-info")
        about_section = Gio.Menu()
        about_section.append(_("About Vantage"), "win.about")
        menu.append_section(None, about_section)

        menu_btn = Gtk.MenuButton(icon_name="open-menu-symbolic",
                                  menu_model=menu)
        menu_btn.set_tooltip_text(_("Main Menu"))
        header.pack_end(menu_btn)
        toolbar.add_top_bar(header)

        self.page = Adw.PreferencesPage()
        toolbar.set_content(self.page)

        self._build()

        # Single polkit prompt at launch; auth_admin_keep covers later writes.
        GLib.idle_add(self._authenticate)

        if self._config.get_run_in_background():
            GLib.idle_add(self._start_sni)

    # ---- auth ----------------------------------------------------------------
    def _authenticate(self):
        # pkexec may block on a polkit prompt — run it off the main loop so the
        # window stays responsive while the password dialog is up.
        self.backend.call_async(self.backend.authenticate, self._on_authenticated)
        return False

    def _on_authenticated(self, ok):
        if ok is not True:
            self._toast(_("Not authorized — changes may prompt for a password."))
        # BIOS attribute values are root-only; read them now that polkit is
        # primed, so the lazy reads don't trigger a second password prompt.
        for init in self._bios_inits:
            init()

    # ---- build ---------------------------------------------------------------
    def _build(self):
        st = self.state

        power = self._group(_("Power & Battery"))
        if "conservation_mode" in st:
            self._switch(power, "conservation_mode", _("Conservation Mode"),
                         _("Caps charge at ~%d%% to extend battery lifespan")
                         % CONSERVATION_LIMIT_PCT,
                         lambda s: s.get("conservation_mode") == "1",
                         lambda on: self.backend.set_vpc("conservation_mode", "1" if on else "0"))
        elif "charge_limit" in st:
            # ThinkPad equivalent of conservation mode (settable charge cap).
            self._switch(power, "charge_limit", _("Battery Charge Limit"),
                         _("Caps charge at ~%d%% to extend battery lifespan")
                         % CONSERVATION_LIMIT_PCT,
                         lambda s: s.get("charge_limit") == "1",
                         lambda on: self.backend.set_charge_limit(on))
        if "usb_charging" in st:
            self._switch(power, "usb_charging", _("Always-On USB"),
                         _("Keep USB ports powered while suspended"),
                         lambda s: s.get("usb_charging") == "1",
                         lambda on: self.backend.set_vpc("usb_charging", "1" if on else "0"))
        elif "always_on_usb_bios" in st:
            self._add_bios_toggle(
                power, "AlwaysOnUSB", _("Always-On USB"),
                _("Keep USB ports powered while off · applies after reboot"))
        if "power_profile" in st:
            self._add_power_profile(power)
        if self.backend.battery_info():
            self._add_battery_health(power)
        self._maybe_add(power)

        thermal = self._group(_("Thermal"))
        if "fan_mode" in st:
            self._add_fan_mode(thermal)
        if "fan_level" in st:
            self._add_fan_level(thermal)
        if "fan_rpms" in st:
            self._add_fan_speed(thermal)
        self._maybe_add(thermal)

        inp = self._group(_("Input"))
        if "fn_lock" in st:
            # fn_lock sysfs: "1" == locked-off; show On == multimedia-without-Fn.
            self._switch(inp, "fn_lock", _("Fn Lock"),
                         _("Use multimedia keys without holding Fn"),
                         lambda s: s.get("fn_lock") == "0",
                         lambda on: self.backend.set_vpc("fn_lock", "0" if on else "1"))
        elif "fnkey_primary_bios" in st:
            # FnKeyAsPrimary: Enable = F1-F12 primary, Disable = media primary.
            # Present it as the latter (what users mean by "media keys"), so the
            # switch ON maps to the attribute's "Disable".
            self._add_bios_toggle(
                inp, "FnKeyAsPrimary", _("Media Keys"),
                _("Use the top row for media without holding Fn · "
                  "applies after reboot"),
                on_value="Disable", off_value="Enable")
        if "touchpad_inhibited" in st:
            self._switch(inp, "touchpad_inhibited", _("Touchpad"),
                         _("Enable the laptop touchpad"),
                         lambda s: s.get("touchpad_inhibited") != "1",
                         lambda on: self.backend.set_touchpad_enabled(on))
        if "kbd_backlight" in st:
            self._add_kbd_backlight(inp)
        self._maybe_add(inp)

        privacy = self._group(_("Privacy"))
        if "mic_on" in st:
            self._switch(privacy, "mic_on", _("Microphone"),
                         _("Unmute the default input device"),
                         lambda s: s.get("mic_on") == "1",
                         lambda on: self.backend.set_mic_on(on))
        self._maybe_add(privacy)

        network = self._group(_("Network"))
        if "wifi_on" in st:
            self._switch(network, "wifi_on", _("Wi-Fi"),
                         _("Enable the wireless radio"),
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

    def _add_bios_toggle(self, group, attr, title, subtitle,
                         on_value="Enable", off_value="Disable"):
        """An on/off BIOS setting via think_lmi.

        The switch maps to ``on_value``/``off_value`` (override when the natural
        on-state is the attribute's "Disable", e.g. FnKeyAsPrimary where media
        keys are primary when the attribute is Disabled).

        The current value is root-only, so it's read lazily after polkit is
        primed (registered in self._bios_inits, run from _on_authenticated).
        Writes go through the helper and may only apply after a reboot. The row
        stays insensitive until the value is known, or if a BIOS Admin password
        gates writes.
        """
        group._has_rows = True
        row = Adw.SwitchRow(title=title, subtitle=subtitle)
        row.set_sensitive(False)

        def on_toggle(r, _p):
            if self._guard:
                return
            want = r.get_active()
            value = on_value if want else off_value

            def done(ok):
                if ok is True:
                    if self.backend.bios_pending_reboot():
                        self._toast(
                            _("“%s” will take effect after a reboot") % title)
                else:
                    self._guard = True
                    r.set_active(not want)   # revert; write was rejected
                    self._guard = False
                    self._toast(_("Could not change “%s”") % title)

            self.backend.call_async(
                lambda: self.backend.set_bios_attr(attr, value), done)

        row.connect("notify::active", on_toggle)
        group.add(row)

        def init():
            if self.backend.bios_admin_locked():
                row.set_subtitle(_("Managed by BIOS (admin password set)"))
                return

            def got(val):
                if isinstance(val, str):
                    self._guard = True
                    row.set_active(val.strip().lower() == on_value.lower())
                    self._guard = False
                    row.set_sensitive(True)
                else:
                    row.set_subtitle(_("Unavailable"))

            self.backend.call_async(
                lambda: self.backend.get_bios_attr(attr), got)

        self._bios_inits.append(init)

    def _add_power_profile(self, group):
        choices = [c for c in self.state["power_profile_choices"].split(",") if c]
        self._profiles = choices
        labels = [PROFILE_LABELS.get(c, c.title()) for c in choices]
        self._combo(group, "power_profile", _("Power Profile"),
                    _("System performance vs. battery"),
                    labels,
                    lambda s: self._profiles.index(s["power_profile"])
                    if s.get("power_profile") in self._profiles else None,
                    lambda i: self.backend.set_power_profile(self._profiles[i]))

    def _add_fan_mode(self, group):
        group._has_rows = True
        labels = [lbl for _v, lbl in FAN_MODES]
        row = Adw.ComboRow(title=_("Fan Mode"), subtitle=_("Cooling profile"),
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
                self.backend.call_async(lambda: self.backend.set_vpc("fan_mode", "2"))
                self._toast(_("Dust cleaning started — fans will return to normal shortly"))
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
                self.backend.call_async(lambda: self.backend.set_vpc("fan_mode", val))

        row.connect("notify::selected", on_selected)
        group.add(row)

        def upd(s):
            i = current_idx(s)
            if i is not None:
                self._sync(row, "selected", i)
        self._updaters.append(upd)

    def _add_fan_level(self, group):
        # Manual ThinkPad fan control. Faithful to thinkfan's safety model: a
        # manual level arms the EC watchdog (auto-reverts if we stop writing),
        # we re-arm it while the window is visible, and hand control back to
        # 'auto' the moment the window is hidden/closed — so the fan can never
        # stay pinned when Vantage isn't watching.
        group._has_rows = True
        row = Adw.ComboRow(
            title=_("Fan Control"),
            subtitle=_("Manual fan level — reverts to Auto when Vantage closes"),
            model=Gtk.StringList.new(FAN_LEVEL_LABELS))

        def current_idx(s):
            v = s.get("fan_level")
            return FAN_LEVELS.index(v) if v in FAN_LEVELS else 0

        row.set_selected(current_idx(self.state))

        def on_selected(r, _p):
            if self._guard:
                return
            level = FAN_LEVELS[r.get_selected()]
            self._fan_hold_level = None if level == "auto" else level
            self.backend.call_async(lambda: self.backend.set_fan_level(level))
            if level == "auto":
                self._stop_fan_hold()
            else:
                self._start_fan_hold()

        row.connect("notify::selected", on_selected)
        group.add(row)
        self._updaters.append(
            lambda s: self._sync(row, "selected", current_idx(s)))
        # Re-arm while visible; release (hand back to auto) when hidden/closed.
        self.connect("map", lambda *_a: self._start_fan_hold())
        self.connect("unmap", lambda *_a: self._release_fan_hold())

    def _start_fan_hold(self):
        if not self._fan_hold_level or self._fan_hold_id or not self.get_mapped():
            return
        self._rearm_fan_hold()   # apply immediately on (re)show
        # Re-arm well inside the EC's watchdog window (FAN_WATCHDOG_SECS=120).
        self._fan_hold_id = GLib.timeout_add_seconds(90, self._rearm_fan_hold)

    def _rearm_fan_hold(self):
        level = self._fan_hold_level
        if not level:
            self._fan_hold_id = 0
            return False
        self.backend.call_async(lambda: self.backend.set_fan_level(level))
        return True

    def _stop_fan_hold(self):
        if self._fan_hold_id:
            GLib.source_remove(self._fan_hold_id)
            self._fan_hold_id = 0

    def _release_fan_hold(self):
        self._stop_fan_hold()
        if self._fan_hold_level:
            # Window gone: return the fan to the EC straight away.
            self.backend.call_async(lambda: self.backend.set_fan_level("auto"))

    def _add_kbd_backlight(self, group):
        try:
            mx = int(self.state.get("kbd_backlight_max", "2"))
        except ValueError:
            mx = 2
        if mx <= 2:
            labels = [_("Off"), _("Low"), _("High")][:mx + 1]
        else:
            labels = [_("Off")] + [_("Level %d") % i for i in range(1, mx + 1)]
        self._combo(group, "kbd_backlight", _("Keyboard Backlight"),
                    _("Key illumination level"),
                    labels,
                    lambda s: max(0, min(mx, int(s.get("kbd_backlight", "0")))),
                    lambda i: self.backend.set_kbd_backlight(i))

    def _add_battery_health(self, group):
        group._has_rows = True
        self._batt_row = Adw.ActionRow(title=_("Battery Health"),
                                       subtitle=self._batt_text(self.backend.battery_info()))
        self._batt_row.add_css_class("property")
        group.add(self._batt_row)

    @staticmethod
    def _batt_text(info):
        if not info:
            return _("Unavailable")
        parts = []
        if "health_pct" in info:
            parts.append(_("%.1f%% of design capacity") % info["health_pct"])
        if "cycle_count" in info:
            parts.append(_("%d cycles") % info["cycle_count"])
        if info.get("capacity"):
            charge = _("%s%% charged") % info["capacity"]
            status = (info.get("status") or "").strip().lower()
            if status and status not in ("unknown", "full"):
                charge += " (%s)" % status
            parts.append(charge)
        return " · ".join(parts) or _("Unavailable")

    def _add_fan_speed(self, group):
        group._has_rows = True
        self._fan_row = Adw.ActionRow(title=_("Fan Speed"),
                                      subtitle=self._fan_text(self.backend.fan_rpms()))
        self._fan_row.add_css_class("property")
        group.add(self._fan_row)
        # Live tachometer poll (every 2s), but only while the window is on screen —
        # no point waking the CPU every 2s when hidden in the tray. map/unmap also
        # ensures the timer is torn down with the window (no leaked source).
        self.connect("map", lambda *_a: self._start_fan_poll())
        self.connect("unmap", lambda *_a: self._stop_fan_poll())

    def _start_fan_poll(self):
        if getattr(self, "_fan_row", None) is None or self._fan_timer_id:
            return
        self._poll_fans()   # refresh immediately on show
        self._fan_timer_id = GLib.timeout_add_seconds(2, self._poll_fans)

    def _stop_fan_poll(self):
        if self._fan_timer_id:
            GLib.source_remove(self._fan_timer_id)
            self._fan_timer_id = 0

    @staticmethod
    def _fan_text(fans):
        if not fans:
            return _("Unavailable")
        parts = ["%s %d RPM" % (lbl or _("Fan %d") % i, rpm)
                 for i, (lbl, rpm) in enumerate(fans, 1)]
        text = " · ".join(parts)
        if all(rpm == 0 for _l, rpm in fans):
            text += "  " + _("(idle)")
        return text

    def _poll_fans(self):
        row = getattr(self, "_fan_row", None)
        if row is None:
            self._fan_timer_id = 0
            return False  # row gone; stop polling
        row.set_subtitle(self._fan_text(self.backend.fan_rpms()))
        return True  # keep the timer alive

    # ---- tray / background ---------------------------------------------------

    def _show_settings(self, *_args):
        # A plain Adw.Dialog that sizes to its content — Adw.PreferencesDialog
        # wraps its page in a scrolled view at a large default height, which
        # leaves empty space and a scrollbar for just two rows.
        dialog = Adw.Dialog(title=_("Preferences"), content_width=420)
        dialog.set_follows_content_size(True)

        grp = Adw.PreferencesGroup()

        bg_row = Adw.SwitchRow(
            title=_("Run in Background"),
            subtitle=_("Keep Vantage in the system tray when closed"),
        )
        # Set state before connecting so the programmatic update is a no-op.
        bg_row.set_active(
            self._config.get_run_in_background() or self._sni is not None)
        bg_row.connect("notify::active", self._on_bg_toggled)
        grp.add(bg_row)

        auto_row = Adw.SwitchRow(title=_("Start on Login"))
        if autostart.desktop_supports_autostart():
            auto_row.set_subtitle(_("Launch Vantage in the tray when you log in"))
        else:
            desktop = autostart.current_desktop() or _("your compositor")
            # Bare WMs (Hyprland, sway, …) don't read ~/.config/autostart.
            auto_row.set_subtitle(
                _("%s may ignore autostart entries — add "
                  "“exec-once = vantage --tray” to its config") % desktop)
        auto_row.set_active(autostart.is_enabled())
        auto_row.connect("notify::active", self._on_autostart_toggled)
        grp.add(auto_row)

        grp.set_margin_top(12)
        grp.set_margin_bottom(12)
        grp.set_margin_start(12)
        grp.set_margin_end(12)

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        tv.set_content(grp)
        dialog.set_child(tv)
        dialog.present(self)

    def _on_bg_toggled(self, row, _pspec):
        if self._guard:
            return
        enabled = row.get_active()
        self._config.set_run_in_background(enabled)
        if enabled:
            self._start_sni()
        else:
            self._stop_sni()
            self.present()   # un-hide window if it was hidden

    def _on_autostart_toggled(self, row, _pspec):
        if self._guard:
            return
        try:
            autostart.set_enabled(row.get_active())
        except OSError:
            log.exception("failed to update autostart entry")
            self._toast(_("Could not update the autostart setting"))
            self._guard = True
            row.set_active(autostart.is_enabled())   # revert to actual state
            self._guard = False

    def _start_sni(self):
        if self._sni is not None:
            return False
        from .tray import VantageSNI
        self.set_hide_on_close(True)
        self._sni = VantageSNI(
            backend   = self.backend,
            window_fn = lambda: self,
            quit_fn   = lambda: self.get_application().quit(),
        )
        self._sni.start()
        self._vis_handler = self.connect(
            "notify::visible",
            lambda *_: self._sni.notify_window_visibility_changed())
        return False

    def _stop_sni(self):
        if self._sni is None:
            return
        self.set_hide_on_close(False)
        if hasattr(self, "_vis_handler"):
            self.disconnect(self._vis_handler)
            del self._vis_handler
        self._sni.stop()
        self._sni = None

    # ---- about ---------------------------------------------------------------
    def _show_about(self, *_args):
        app = self.get_application()
        about = Adw.AboutDialog(
            application_name=_("Lenovo Vantage"),
            application_icon="org.vantage.Vantage",
            developer_name="isshin1",
            version=getattr(app, "version", None) or "",
            license_type=Gtk.License.GPL_3_0,
            website="https://github.com/isshin1/vantage",
            issue_url="https://github.com/isshin1/vantage/issues",
            copyright="© 2026 isshin1",
            comments=_("Native Lenovo hardware controls for Linux."),
        )
        about.present(self)

    def _show_device_info(self, *_args):
        info = self.backend.system_info()
        dialog = Adw.Dialog()
        dialog.set_title(_("About This Device"))
        dialog.set_content_width(460)

        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar())
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title=_("System"))

        def row(title, value):
            r = Adw.ActionRow(title=title, subtitle=value or _("Unknown"))
            r.set_subtitle_selectable(True)
            group.add(r)
            return r

        row(_("Device"), info["device"])
        family = {"thinkpad": "ThinkPad", "ideapad": "IdeaPad / Yoga"}.get(
            info.get("platform_family"))
        if family:
            row(_("Platform"), family)
        if info.get("machine_type"):
            row(_("Machine Type"), info["machine_type"])
        row(_("Processor"), info["cpu"])
        row(_("Memory"), info["ram"])
        row(_("Operating System"), info["os"])
        row(_("Kernel"), info["kernel"])
        row(_("Hostname"), info["hostname"])
        serial_row = row(_("Serial Number"), _("Loading…"))

        page.add(group)
        tv.set_content(page)
        dialog.set_child(tv)
        dialog.present(self)

        # Serial is root-only; fetch it off-thread so the pkexec call never
        # blocks the UI (the launch-time auth is cached, so this won't re-prompt).
        def got_serial(val):
            serial_row.set_subtitle((val if isinstance(val, str) else None)
                                    or _("Unavailable"))
        self.backend.call_async(self.backend.serial, got_serial)

    # ---- state plumbing ------------------------------------------------------
    def _sync(self, row, prop, value):
        """Set a widget property without re-triggering its handler."""
        if row.get_property(prop) != value:
            row.set_property(prop, value)

    def _handle(self, action):
        if self._guard:
            return
        # Run the (possibly pkexec-backed) write off the main loop, then refresh.
        self.backend.call_async(action, lambda _r: self.refresh())

    def refresh(self, *_):
        # State reads spawn pactl/nmcli/powerprofilesctl — do it off-thread and
        # apply the result back on the main loop.
        self.backend.get_state_async(self._apply_state)
        return False

    def _apply_state(self, state):
        if isinstance(state, BaseException):
            log.error("state refresh failed: %s", state)
            return
        self.state = state
        self._guard = True
        for upd in self._updaters:
            try:
                upd(self.state)
            except Exception:
                log.exception("updater failed during refresh")
        if getattr(self, "_fan_row", None) is not None:
            self._fan_row.set_subtitle(self._fan_text(self.backend.fan_rpms()))
        if getattr(self, "_batt_row", None) is not None:
            self._batt_row.set_subtitle(self._batt_text(self.backend.battery_info()))
        self._guard = False

    def _toast(self, msg):
        self.toasts.add_toast(Adw.Toast(title=msg))
