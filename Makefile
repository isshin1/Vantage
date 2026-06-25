.PHONY: install install-files uninstall

LIBDIR := /usr/lib/vantage

# Native GTK4 app + tray + pkexec helper (default): deps + files.
install:
	chmod +x ./install.sh
	./install.sh
	$(MAKE) install-files

# Install only the application files (assumes dependencies already present).
install-files:
	# Icons: colourful app logo + monochrome symbolic icon for the tray
	install -Dm644 ./icon.png /usr/share/icons/hicolor/scalable/apps/vantage.png
	install -Dm644 ./icon-symbolic.svg \
		/usr/share/icons/hicolor/symbolic/apps/vantage-symbolic.svg

	# Python modules (shared by the window, the tray and the helper)
	install -d $(LIBDIR)
	install -m644 ./vantage_common.py $(LIBDIR)/vantage_common.py
	install -m644 ./vantage_client.py $(LIBDIR)/vantage_client.py
	install -m644 ./vantage_helper.py $(LIBDIR)/vantage_helper.py
	install -m644 ./vantage_window.py $(LIBDIR)/vantage_window.py
	install -m644 ./vantage_tray.py   $(LIBDIR)/vantage_tray.py

	# Executable wrappers. vantage-helper is root-owned and run via pkexec;
	# its path must match the polkit action's exec.path annotation.
	printf '#!/bin/sh\nexec python3 $(LIBDIR)/vantage_window.py "$$@"\n' > /usr/bin/vantage
	printf '#!/bin/sh\nexec python3 $(LIBDIR)/vantage_tray.py "$$@"\n'   > /usr/bin/vantage-tray
	printf '#!/bin/sh\nexec python3 $(LIBDIR)/vantage_helper.py "$$@"\n' > /usr/bin/vantage-helper
	chmod a+rx /usr/bin/vantage /usr/bin/vantage-tray
	chown root:root /usr/bin/vantage-helper
	chmod 755 /usr/bin/vantage-helper

	# polkit policy (pkexec auth for the helper; auth_admin_keep = ask once)
	install -Dm644 ./dist/polkit/org.vantage.helper.policy \
		/usr/share/polkit-1/actions/org.vantage.helper.policy

	# Application launcher (GTK4 settings window) + tray autostart entry
	install -Dm644 ./dist/vantage.desktop \
		/usr/share/applications/vantage.desktop
	install -Dm644 ./dist/vantage-tray.desktop \
		/usr/share/applications/vantage-tray.desktop

	@echo "Installed. Run 'vantage' for the settings window, or 'vantage-tray' for the tray."

uninstall:
	rm -f /usr/share/icons/hicolor/scalable/apps/vantage.png
	rm -f /usr/share/icons/hicolor/symbolic/apps/vantage-symbolic.svg
	rm -f /usr/bin/vantage /usr/bin/vantage-tray /usr/bin/vantage-helper
	rm -rf $(LIBDIR)
	rm -f /usr/share/polkit-1/actions/org.vantage.helper.policy
	rm -f /usr/share/applications/vantage.desktop
	rm -f /usr/share/applications/vantage-tray.desktop

