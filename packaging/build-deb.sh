#!/bin/sh
# Build a binary .deb without debhelper (for quick local packaging).
# For a proper source package: cp -r packaging/debian debian && dpkg-buildpackage -us -uc -b
set -eu
cd "$(dirname "$0")/.."
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' netwatch/__init__.py)
STAGE=build/deb/netwatch_${VERSION}_all
rm -rf "$STAGE"
mkdir -p "$STAGE/DEBIAN" "$STAGE/usr/lib/python3/dist-packages" "$STAGE/usr/bin" "$STAGE/usr/share/doc/netwatch"
cp -r netwatch "$STAGE/usr/lib/python3/dist-packages/"
find "$STAGE" -name __pycache__ -prune -exec rm -rf {} +
cat > "$STAGE/usr/bin/netwatch" <<'PY'
#!/usr/bin/python3
from netwatch.main import main

raise SystemExit(main())
PY
chmod 755 "$STAGE/usr/bin/netwatch"
make --no-print-directory install-data DESTDIR="$STAGE" PREFIX=/usr
cp packaging/debian/copyright "$STAGE/usr/share/doc/netwatch/copyright"
gzip -9n -c packaging/debian/changelog > "$STAGE/usr/share/doc/netwatch/changelog.Debian.gz"
SIZE=$(du -sk "$STAGE/usr" | cut -f1)
cat > "$STAGE/DEBIAN/control" <<CTRL
Package: netwatch
Version: ${VERSION}-1
Architecture: all
Maintainer: NetWatch contributors <netwatch@example.invalid>
Installed-Size: ${SIZE}
Depends: python3 (>= 3.10), python3-gi (>= 3.42), python3-gi-cairo, gir1.2-gtk-4.0 (>= 4.12), gir1.2-adw-1 (>= 1.4), iputils-ping
Recommends: network-manager, pipewire-bin | pulseaudio-utils | alsa-utils
Suggests: iw
Section: net
Priority: optional
Homepage: https://github.com/yassermaina/netwatch
Description: local-first network connectivity and outage monitor
 NetWatch monitors internet connectivity, detects outages, measures latency
 and packet loss, keeps a local SQLite history and exports TXT/CSV/HTML reports.
CTRL
cat > "$STAGE/DEBIAN/postinst" <<'SH'
#!/bin/sh
set -e
if [ "$1" = configure ]; then
  py3compile -p netwatch 2>/dev/null || true
fi
SH
cat > "$STAGE/DEBIAN/prerm" <<'SH'
#!/bin/sh
set -e
py3clean -p netwatch 2>/dev/null || find /usr/lib/python3/dist-packages/netwatch -name __pycache__ -prune -exec rm -rf {} + 2>/dev/null || true
SH
chmod 755 "$STAGE/DEBIAN/postinst" "$STAGE/DEBIAN/prerm"
dpkg-deb --root-owner-group --build "$STAGE" "build/netwatch_${VERSION}-1_all.deb"
echo "built build/netwatch_${VERSION}-1_all.deb"
