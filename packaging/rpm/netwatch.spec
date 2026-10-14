Name:           netwatch
Version:        1.0.0
Release:        1%{?dist}
Summary:        Local-first network connectivity and outage monitor (GTK4/Libadwaita)

License:        MIT
URL:            https://github.com/netwatch-app/netwatch
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

BuildRequires:  python3-devel
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-setuptools
BuildRequires:  desktop-file-utils
BuildRequires:  libappstream-glib
BuildRequires:  make

Requires:       python3 >= 3.10
Requires:       python3-gobject >= 3.42
Requires:       python3-cairo
Requires:       gtk4 >= 4.12
Requires:       libadwaita >= 1.4
Requires:       iputils
Requires:       hicolor-icon-theme
Recommends:     NetworkManager
Recommends:     (pipewire-utils or pulseaudio-utils or alsa-utils)
Suggests:       iw

%description
NetWatch continuously monitors internet connectivity, detects outages,
measures latency and packet loss, records a local SQLite history and
generates TXT, CSV and HTML reports. It distinguishes "no internet" from
"not connected" and from periods when the application was not running.

%prep
%autosetup -n %{name}-%{version}

%generate_buildrequires
%pyproject_buildrequires

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files netwatch
make install-data DESTDIR=%{buildroot} PREFIX=%{_prefix}

%check
desktop-file-validate %{buildroot}%{_datadir}/applications/io.github.netwatch.NetWatch.desktop
appstream-util validate-relax --nonet %{buildroot}%{_metainfodir}/io.github.netwatch.NetWatch.metainfo.xml
%pyproject_check_import -e 'netwatch.ui*' -e 'netwatch.tray' -e 'netwatch.notify'

%files -f %{pyproject_files}
%license LICENSE
%doc README.md
%{_bindir}/netwatch
%{_datadir}/applications/io.github.netwatch.NetWatch.desktop
%{_metainfodir}/io.github.netwatch.NetWatch.metainfo.xml
%{_datadir}/icons/hicolor/scalable/apps/io.github.netwatch.NetWatch.svg
%{_datadir}/icons/hicolor/symbolic/apps/io.github.netwatch.NetWatch*-symbolic.svg

%changelog
* Wed Oct 14 2026 NetWatch contributors - 1.0.0-1
- First release
