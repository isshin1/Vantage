#!/bin/bash

# Function to detect package manager
detect_package_manager() {
    if command -v pacman &> /dev/null; then
        echo "pacman"
    elif command -v apt &> /dev/null; then
        echo "apt"
    elif command -v dnf &> /dev/null; then
        echo "dnf"
    elif command -v zypper &> /dev/null; then
        echo "zypper"
    else
        echo "unknown"
    fi
}

# check for the distro
if [ -f /etc/os-release ]; then
    . /etc/os-release
    distro=$ID_LIKE

    # Some distros like Fedora doesn't have "ID_LIKE" in their /etc/os-release file, sadly
    if [ -z "$distro" ]; then
        distro=$ID
    fi
fi

case $distro in
  # Now Vantage can be installed on Cachy OS, ArcoLinux... you name it!
  "arch")
    echo "Installing on Arch Linux or derivative"
    pacman -Qi python-gobject gtk4 libadwaita polkit networkmanager meson ninja gettext glib2 appstream &> /dev/null || sudo pacman -S python-gobject gtk4 libadwaita polkit networkmanager meson ninja gettext glib2 appstream
    ;;

  # Now Vantage can not only be installed on Ubuntu or POP OS but also Kubuntu, KDE Neon, Xubuntu...
  "debian")
    echo "Installing on Debian or derivative"
    dpkg -s python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0 policykit-1 meson ninja-build gettext libglib2.0-bin appstream &> /dev/null || sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0 policykit-1 meson ninja-build gettext libglib2.0-bin appstream
    ;;

  # Entry for Linux Mint 21.3 Edge
  "ubuntu debian")
    echo "Installing on Linux Mint Edge"
    dpkg -s python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0 policykit-1 meson ninja-build gettext libglib2.0-bin appstream &> /dev/null || sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0 policykit-1 meson ninja-build gettext libglib2.0-bin appstream
    ;;

  "fedora")
    echo "Installing on Fedora"
    rpm -q python3-gobject gtk4 libadwaita polkit NetworkManager pipewire-pulseaudio meson ninja-build gettext glib2-devel appstream &> /dev/null || sudo dnf install python3-gobject gtk4 libadwaita polkit NetworkManager pipewire-pulseaudio meson ninja-build gettext glib2-devel appstream
    ;;

  "opensuse-tumbleweed")
    echo "Installing on OpenSuse"
    rpm -q python3-gobject gtk4 libadwaita typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 polkit NetworkManager pipewire-pulseaudio meson ninja gettext-tools glib2-tools appstream &> /dev/null || sudo zypper install python3-gobject gtk4 libadwaita typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 polkit NetworkManager pipewire-pulseaudio meson ninja gettext-tools glib2-tools appstream
    ;;

  *)
    echo "Unknown Distro, attempting package manager detection..."
    package_manager=$(detect_package_manager)

    case $package_manager in
        "pacman")
            echo "Detected pacman package manager"
            pacman -Qi python-gobject gtk4 libadwaita polkit networkmanager meson ninja gettext glib2 appstream &> /dev/null || sudo pacman -S python-gobject gtk4 libadwaita polkit networkmanager meson ninja gettext glib2 appstream
            ;;
        "apt")
            echo "Detected apt package manager"
            dpkg -s python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0 policykit-1 meson ninja-build gettext libglib2.0-bin appstream &> /dev/null || sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0 policykit-1 meson ninja-build gettext libglib2.0-bin appstream
            ;;
        "dnf")
            echo "Detected dnf package manager"
            rpm -q python3-gobject gtk4 libadwaita polkit NetworkManager pipewire-pulseaudio meson ninja-build gettext glib2-devel appstream &> /dev/null || sudo dnf install python3-gobject gtk4 libadwaita polkit NetworkManager pipewire-pulseaudio meson ninja-build gettext glib2-devel appstream
            ;;
        "zypper")
            echo "Detected zypper package manager"
            rpm -q python3-gobject gtk4 libadwaita typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 polkit NetworkManager pipewire-pulseaudio meson ninja gettext-tools glib2-tools appstream &> /dev/null || sudo zypper install python3-gobject gtk4 libadwaita typelib-1_0-Gtk-4_0 typelib-1_0-Adw-1 polkit NetworkManager pipewire-pulseaudio meson ninja gettext-tools glib2-tools appstream
            ;;
        *)
            echo "Unable to detect compatible package manager, exiting."
            exit 1
            ;;
    esac
    ;;
esac

echo "Requirements are installed"
