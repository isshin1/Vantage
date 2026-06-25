#!/usr/bin/env python3
"""Application entry point.

Parses the command line, configures logging, and runs the GApplication. The
launcher script (vantage.in, installed as /usr/bin/vantage) sets up sys.path and
the gettext domain before calling main().
"""
import argparse
import logging
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib  # noqa: E402

from .client import Vantage, VantageConfig  # noqa: E402
from .window import VantageWindow  # noqa: E402

APP_ID = "org.vantage.Vantage"

log = logging.getLogger("vantage")
log.addHandler(logging.NullHandler())  # silent unless --debug adds a handler


class VantageApp(Adw.Application):
    def __init__(self, tray_only=False, version=None):
        super().__init__(application_id=APP_ID,
                         flags=Gio.ApplicationFlags.DEFAULT_FLAGS)
        self.backend    = Vantage()
        self.config     = VantageConfig()
        self.version    = version
        self._tray_only = tray_only
        self.win        = None

    def do_activate(self):
        if self.win is None:
            self.win = VantageWindow(self, self.backend, self.config)
        if self._tray_only:
            # --tray: start with the window hidden. Bring the tray up for this
            # run only — without flipping the persisted run-in-background pref
            # (a transient CLI flag shouldn't rewrite stored settings). If the
            # pref is already on, the window's __init__ has queued _start_sni and
            # the guard inside it makes a second call a no-op.
            if self.win._sni is None:
                GLib.idle_add(self.win._start_sni)
            # Don't present the window — the user opens it via the tray icon.
        else:
            self.win.present()


def main(version=None):
    parser = argparse.ArgumentParser(
        prog="vantage",
        description="Lenovo Vantage for Linux — native hardware control panel.",
        epilog="Run with no options to open the settings window.",
    )
    parser.add_argument("-t", "--tray", action="store_true",
                        help="start minimised to the system tray (no window)")
    parser.add_argument("-d", "--debug", action="store_true",
                        help="enable verbose debug logging to stderr")
    if version:
        parser.add_argument("-v", "--version", action="version",
                            version="vantage %s" % version)
    args = parser.parse_args()

    if args.debug:
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
            stream=sys.stderr,
        )
        log.setLevel(logging.DEBUG)
        log.debug("debug logging enabled; tray_only=%s", args.tray)

    app = VantageApp(tray_only=args.tray, version=version)
    # GApplication would otherwise try to parse our argv itself; pass an empty
    # list so argparse remains the single source of truth.
    return app.run([])


if __name__ == "__main__":
    raise SystemExit(main())
