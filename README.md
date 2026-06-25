# Lenovo Vantage for Linux

A native GTK4 app that brings Lenovo Vantage controls to Linux — battery conservation, fan modes, thermal profiles, keyboard backlight, and more. Works across IdeaPad, Yoga, and Legion models.

![preview](images/preview.png)

## Features

**Power & Battery**
- Conservation Mode — cap charge at ~80% to extend battery lifespan
- Always-On USB — keep USB ports powered while suspended
- Power Profile — Low Power / Balanced / Performance (via `power-profiles-daemon`)
- Battery Health — shows capacity vs. design capacity, charge cycles, and current charge

**Thermal**
- Fan Mode — Super Silent, Standard, Dust Cleaning, Efficient Thermal Dissipation
- Live fan RPM readout for both fans

**Input**
- Fn Lock — use multimedia keys without holding Fn
- Keyboard Backlight — set illumination level
- Touchpad toggle — Wayland-native via kernel `inhibited` attribute

**Privacy & Network**
- Microphone mute via `pactl`
- Wi-Fi toggle via `nmcli`

**About**
- Device info panel — model, CPU, RAM, OS, serial number

Controls auto-hide when the underlying hardware isn't present, so the same app works across different Lenovo models. Legion-only controls (Super key lock, fast charge, display overdrive, hybrid graphics) appear automatically when the `LenovoLegionLinux` kernel module is loaded.

> **Note:** Camera privacy is not controlled in software. On many models (e.g. Yoga Pro 7i Gen 11) the `camera_power` sysfs bit is cosmetic and doesn't actually gate the sensor — use the laptop's physical camera key instead, which is EC-backed.

## Installation

```bash
git clone https://github.com/isshin1/vantage.git
cd vantage
sudo make install
```

Then launch **Lenovo Vantage** from your applications menu, or run `vantage` / `vantage-tray` from the terminal.

### Manual dependency install

`sudo make install` handles dependencies automatically. If you prefer to install them yourself:

**Arch Linux**
```bash
sudo pacman -S python-gobject libayatana-appindicator gtk4 libadwaita polkit networkmanager
```

**Debian / Ubuntu / Mint / Pop!_OS**
```bash
sudo apt install python3-gi gir1.2-ayatanaappindicator3-0.1 gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0 policykit-1
```

**Fedora**
```bash
sudo dnf install python3-gobject libayatana-appindicator-gtk3 gtk4 libadwaita polkit NetworkManager pipewire-pulseaudio
```

**openSUSE Tumbleweed**
```bash
sudo zypper install python3-gobject libayatana-appindicator3-1 typelib-1_0-AyatanaAppIndicator3-0_1 gtk4 libadwaita typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 polkit NetworkManager pipewire-pulseaudio
```

## Uninstall

```bash
sudo make uninstall
```

## Architecture

Vantage is split into three components:

- **`vantage`** — GTK4 + libadwaita settings window with grouped switch/combo rows for every control
- **`vantage-tray`** — system tray indicator (AyatanaAppIndicator3/GTK3, since GTK4 has no tray API) with quick toggles and an *Open Vantage…* menu item. Works on Wayland compositors including Sway, Hyprland, KDE, and GNOME
- **`vantage-helper`** — a minimal root helper that performs privileged sysfs writes. Invoked via `pkexec`, gated by polkit (`auth_admin_keep`) — you authenticate once per session, not per change. No long-running daemon

State is read directly from sysfs (no root needed for reads). Both the window and tray share one client (`vantage_client.py`) so they behave identically. Unprivileged operations (microphone, Wi-Fi, power profile) run with no prompt.

## Requirements

- Python 3 + `python-gobject`
- GTK4 + libadwaita
- `libayatana-appindicator` (tray)
- `polkit` / `pkexec`
- `networkmanager`
- `pulseaudio` or `pipewire-pulse`
- `power-profiles-daemon` *(optional — required for Power Profile control)*
