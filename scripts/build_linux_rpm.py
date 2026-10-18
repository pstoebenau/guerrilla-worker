"""Stream the complete Tauri DEB filesystem into an RPM without buffering the complete payload in RAM."""
import argparse
from pathlib import Path
import re
import subprocess


def build(bundle, version):
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('RPM version must be numeric major.minor.patch')
    roots = list((bundle / 'deb').glob('*/data'))
    if len(roots) != 1 or not (roots[0] / 'usr/bin/guerrilla-worker-desktop').is_file():
        raise ValueError('Expected one complete Tauri DEB staging filesystem')
    filesystem = roots[0].resolve()
    output = (bundle / 'rpm').resolve()
    output.mkdir(parents=True, exist_ok=True)
    # Reuse the native Tauri layout, icons and desktop entry. System rpmbuild
    # streams its cpio payload; Tauri's RPM builder retains all files in RAM.
    owned = [file for folder in ('usr/bin', 'usr/lib', 'usr/share/applications')
             for file in (filesystem / folder).iterdir()]
    owned += [file for file in (filesystem / 'usr/share/icons').rglob('*') if file.is_file()]
    file_list = '\n'.join('"/' + file.relative_to(filesystem).as_posix().replace('%', '%%') + '"' for file in sorted(owned))
    spec = output / 'guerrilla-worker.spec'
    spec.write_text(f'''%global _build_id_links none
%global __os_install_post %{{nil}}
%global _binary_payload w1.gzdio
Name: guerrilla-worker
Version: {version}
Release: 1
Summary: Guerrilla GPU worker management
License: GPL-3.0-only
URL: https://github.com/pstoebenau/guerrilla-worker
BuildArch: x86_64
AutoReqProv: no
Requires: gtk3, webkit2gtk4.1

%description
Guerrilla Worker management app with native sidecars and supporting runtimes.
Spirula and its reconstruction model are installed automatically on first use.

%files
%defattr(-,root,root,-)
{file_list}
''')
    expected_files = {'/' + file.relative_to(filesystem).as_posix() for file in filesystem.rglob('*') if file.is_file()}
    subprocess.run(['rpmbuild', '-bb', '--buildroot', str(filesystem),
                    '--define', f'_topdir {output}', '--define', f'_rpmdir {output}',
                    '--define', '_rpmfilename Guerrilla-Worker-%{VERSION}-%{RELEASE}.%{ARCH}.rpm',
                    str(spec)], check=True)
    rpm = output / f'Guerrilla-Worker-{version}-1.x86_64.rpm'
    subprocess.run(['rpm', '--checksig', str(rpm)], check=True)
    inventory = set(subprocess.check_output(['rpm', '-qpl', str(rpm)], text=True).splitlines())
    missing = expected_files - inventory
    if missing:
        raise ValueError(f'RPM omitted staged files: {sorted(missing)[:10]}')
    print(f'Complete RPM prepared: {rpm}')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', type=Path, required=True)
    parser.add_argument('--version', required=True)
    args = parser.parse_args()
    build(args.bundle, args.version)
