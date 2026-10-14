#!/bin/sh
# Build a noarch RPM in build/rpm/ (Fedora: sudo dnf install rpm-build make python3).
set -eu
cd "$(dirname "$0")/.."
command -v rpmbuild >/dev/null || { echo "rpmbuild not found. Fedora: sudo dnf install rpm-build make python3"; exit 1; }
VERSION=$(sed -n 's/^__version__ = "\(.*\)"/\1/p' netwatch/__init__.py)
TOP="$PWD/build/rpm"
rm -rf "$TOP"
mkdir -p "$TOP/SOURCES" "$TOP/SPECS"
# Package the current working tree (including uncommitted changes).
tar --exclude=./build --exclude=./.git --exclude='*.zip' --exclude='__pycache__' --exclude=.pytest_cache \
    --transform "s,^\.,netwatch-${VERSION}," -czf "$TOP/SOURCES/netwatch-${VERSION}.tar.gz" .
cp packaging/rpm/netwatch.spec "$TOP/SPECS/"
rpmbuild --define "_topdir $TOP" "$@" -ba "$TOP/SPECS/netwatch.spec"
echo
echo "Built:"; ls "$TOP"/RPMS/noarch/*.rpm
