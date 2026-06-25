"""XDG autostart helper.

Toggles whether Vantage starts on login by writing (or removing) a .desktop
entry in ~/.config/autostart, per the freedesktop Autostart spec.

Full desktop environments (GNOME, KDE, XFCE, …) read that directory on login.
Bare window managers / standalone compositors (Hyprland, sway, i3, …) generally
do not — they use their own config (e.g. Hyprland's `exec-once`). The UI uses
desktop_supports_autostart() to warn when that's the case. The truth source for
"is autostart on" is simply whether the file exists.
"""
import logging
import os

from gi.repository import GLib

log = logging.getLogger("vantage.autostart")

APP_ID = "org.vantage.Vantage"
DESKTOP_BASENAME = APP_ID + ".desktop"

AUTOSTART_DIR  = os.path.join(GLib.get_user_config_dir(), "autostart")
AUTOSTART_FILE = os.path.join(AUTOSTART_DIR, DESKTOP_BASENAME)

# Command the autostart entry runs: start hidden in the tray on login.
AUTOSTART_EXEC = "vantage --tray"

# Desktops known to implement the XDG Autostart spec out of the box. Matched
# case-insensitively against the XDG_CURRENT_DESKTOP token list.
_AUTOSTART_DESKTOPS = {
    "GNOME", "KDE", "XFCE", "LXQT", "MATE", "CINNAMON", "X-CINNAMON",
    "BUDGIE", "DEEPIN", "PANTHEON", "LXDE", "UKUI", "ENLIGHTENMENT", "COSMIC",
}


def is_enabled():
    """True if the autostart entry is currently present."""
    return os.path.exists(AUTOSTART_FILE)


def current_desktop():
    """Best-effort name of the current desktop/compositor, or '' if unknown."""
    val = (os.environ.get("XDG_CURRENT_DESKTOP")
           or os.environ.get("XDG_SESSION_DESKTOP", ""))
    return val.split(":")[0]


def desktop_supports_autostart():
    """True if XDG_CURRENT_DESKTOP names a desktop that reads ~/.config/autostart.

    XDG_CURRENT_DESKTOP is the variable the Autostart spec matches OnlyShowIn /
    NotShowIn against, so it's the right signal here. A bare compositor such as
    Hyprland reports its own name (e.g. "Hyprland") and returns False.
    """
    cur = os.environ.get("XDG_CURRENT_DESKTOP", "")
    tokens = {t.strip().upper() for t in cur.split(":") if t.strip()}
    return bool(tokens & _AUTOSTART_DESKTOPS)


def set_enabled(enabled):
    """Create or remove the autostart entry. Raises OSError on write failure."""
    if enabled:
        _write_entry()
    else:
        _remove_entry()


def _installed_desktop_path():
    """Locate the installed application desktop entry across XDG data dirs."""
    for base in [GLib.get_user_data_dir(), *GLib.get_system_data_dirs()]:
        path = os.path.join(base, "applications", DESKTOP_BASENAME)
        if os.path.exists(path):
            return path
    return None


def _write_entry():
    os.makedirs(AUTOSTART_DIR, exist_ok=True)

    kf = GLib.KeyFile()
    source = _installed_desktop_path()
    if source is not None:
        # Start from the installed desktop entry so Name/Icon/translations carry
        # over, then override the bits specific to autostart.
        kf.load_from_file(source, GLib.KeyFileFlags.KEEP_TRANSLATIONS)
    else:
        # Uninstalled / not on the desktop search path: synthesise a minimal one.
        log.debug("installed desktop file not found; writing a minimal entry")
        kf.set_string("Desktop Entry", "Type", "Application")
        kf.set_string("Desktop Entry", "Name", "Lenovo Vantage")
        kf.set_string("Desktop Entry", "Icon", APP_ID)

    grp = "Desktop Entry"
    kf.set_string(grp, "Exec", AUTOSTART_EXEC)
    # GNOME/KDE honour this flag for enable/disable without deleting the file;
    # we delete on disable anyway, but set it true so the entry is never treated
    # as disabled while present.
    kf.set_boolean(grp, "X-GNOME-Autostart-enabled", True)

    kf.save_to_file(AUTOSTART_FILE)
    log.debug("wrote autostart entry %s", AUTOSTART_FILE)


def _remove_entry():
    try:
        os.remove(AUTOSTART_FILE)
        log.debug("removed autostart entry %s", AUTOSTART_FILE)
    except FileNotFoundError:
        pass
