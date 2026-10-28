"""Build and verify an offline Ubuntu 22.04 amd64 DEB inside Linux/WSL.

The target uses the bundled Python/Qt runtime; no pip/apt network operation runs
at installation or application startup. Build dependencies are installed once:
  apt-get install python3.10-venv libxcb-cursor0 libxkbcommon-x11-0 \
      libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 \
      libxcb-xinerama0 libxcb-shape0 fonts-noto-cjk xvfb xauth
"""

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile
from release_version import metadata, resolve_version

ROOT = Path(__file__).resolve().parents[1]
PACKAGING = ROOT / "packaging"
PACKAGE = "jm-studio"
EXECUTABLE = "JointMotorController"
GLIBC_LIBRARIES = {
    "ld-linux-x86-64.so.2", "libc.so.6", "libm.so.6", "libdl.so.2",
    "libpthread.so.0", "librt.so.1", "libresolv.so.2", "libutil.so.1",
    "libanl.so.1", "libnss_files.so.2", "libnss_dns.so.2",
}


def run(args, **kwargs):
    print("+", " ".join(map(str, args)), flush=True)
    return subprocess.run(list(map(str, args)), check=True, **kwargs)


def write(path, text, mode=0o644):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(mode)


def build_environment():
    requirements = PACKAGING / "requirements-ubuntu-build.txt"
    digest = hashlib.sha256(requirements.read_bytes()).hexdigest()
    cache = Path.home() / ".cache" / "jm-studio-deb" / "python310"
    python = cache / "bin" / "python"
    if not python.is_file():
        run([sys.executable, "-m", "venv", cache])
    marker = cache / "requirements.sha256"
    if not marker.is_file() or marker.read_text().strip() != digest:
        run([python, "-m", "pip", "install", "--disable-pip-version-check",
             "-r", requirements])
        marker.write_text(digest, encoding="ascii")
    return python


def stage_resources(work, full_features):
    staging = work / "staging"
    if not full_features:
        run([sys.executable, PACKAGING / "prepare_build.py", "--out", staging])
    else:
        shutil.copytree(ROOT / "resources", staging / "resources")
    resources = staging / "resources"
    flags = json.loads((resources / "feature_flags.json").read_text(encoding="utf-8"))
    if flags.get("features", {}).get("can_transport") is not True:
        raise ValueError("Release builds must enable can_transport")
    # Ship release defaults, not this developer's device paths/window/session.
    write(resources / "ui_layout.json", '{"version": 1}\n')
    write(resources / "user_motor_presets.json", '{"version": 1, "presets": {}}\n')
    font = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc")
    if not font.is_file():
        raise SystemExit("Missing Chinese font: install fonts-noto-cjk on the builder")
    (resources / "fonts").mkdir()
    shutil.copy2(font, resources / "fonts" / font.name)
    return resources


def freeze(python, work, resources):
    args = [python, "-m", "PyInstaller", "--noconfirm", "--clean", "--onedir",
            "--name", EXECUTABLE, "--paths", ROOT,
            "--distpath", work / "dist", "--workpath", work / "pyinstaller",
            "--specpath", work, "--add-data", f"{resources}:resources"]
    for module in ("pyqtgraph.graphicsItems", "pyqtgraph.widgets", "pyqtgraph.exporters",
                   "pyqtgraph.parametertree", "transport.virtual_engine",
                   "can.interfaces.pcan", "can.interfaces.socketcan", "can.interfaces.virtual"):
        args += ["--collect-submodules", module]
    for module in ("serial.tools.list_ports", "serial.tools.list_ports_linux",
                   "serial.serialposix", "PyQt6.sip", "PyQt6.QtOpenGLWidgets"):
        args += ["--hidden-import", module]
    for module in ("PyQt6.QtPdf", "PyQt6.QtNetwork", "PyQt6.QtTest", "PyQt6.QtQml",
                   "PyQt6.QtQuick", "PyQt6.QtWebEngineCore", "PyQt6.QtWebEngineWidgets",
                   "PyQt6.QtSql", "PyQt6.QtXml", "PyQt6.QtMultimedia",
                   "PyQt6.QtPrintSupport", "PyQt6.QtBluetooth", "PyQt6.QtNfc",
                   "pyqtgraph.examples", "pyqtgraph.tests", "tkinter"):
        args += ["--exclude-module", module]
    for distribution in ("PyQt6", "PyQt6-Qt6", "PyQt6-sip", "pyqtgraph", "numpy",
                         "pyserial", "python-can"):
        args += ["--copy-metadata", distribution]
    args.append(PACKAGING / "linux_entry.py")
    run(args, cwd=ROOT)
    return work / "dist" / EXECUTABLE


def is_elf(path):
    if not path.is_file() or path.is_symlink():
        return False
    with path.open("rb") as source:
        return source.read(4) == b"\x7fELF"


def complete_shared_libraries(bundle):
    """Bundle the ELF dependency closure, retaining Ubuntu's own glibc/loader."""
    internal = bundle / "_internal"
    paths = {str(internal)}
    queue = [path for path in bundle.rglob("*") if is_elf(path)]
    paths.update(str(path.parent) for path in queue)
    env = dict(os.environ, LD_LIBRARY_PATH=":".join(sorted(paths)))
    copied = {}
    baseline = set()
    for binary in queue:
        result = subprocess.run(["ldd", str(binary)], text=True, capture_output=True,
                                env=env, check=False)
        if result.returncode and not any(message in result.stdout + result.stderr
                                        for message in ("not a dynamic executable",
                                                        "statically linked")):
            raise RuntimeError(f"ldd failed: {binary}\n{result.stdout}{result.stderr}")
        for line in result.stdout.splitlines():
            match = re.match(r"\s*(\S+)\s+=>\s+(.+)", line)
            if not match:
                continue
            name, target = match.groups()
            if target.strip() == "not found":
                raise RuntimeError(f"Missing shared library {name}, required by {binary}")
            if name in GLIBC_LIBRARIES:
                baseline.add(name)
                continue
            source = Path(target.split()[0]).resolve()
            if not source.is_file() or source.is_relative_to(bundle):
                continue
            destination = internal / name
            if not destination.exists():
                shutil.copy2(source, destination)
                queue.append(destination)
                copied[name] = str(source)
    return {"additional_bundled_libraries": copied,
            "elf_basenames": sorted({path.name for path in queue}),
            "system_glibc_libraries": sorted(baseline), "elf_files_checked": len(queue)}


def add_licenses(docs, library_report):
    notices = docs / "third-party"
    notices.mkdir(parents=True)
    packages = {"fonts-noto-cjk", "python3.10-minimal", "libpython3.10-stdlib"}
    for name in library_report["elf_basenames"]:
        result = subprocess.run(["dpkg-query", "-S", "*/" + name], text=True,
                                capture_output=True, check=False)
        if result.returncode == 0:
            packages.update(line.split(": ", 1)[0].split(":", 1)[0]
                            for line in result.stdout.splitlines() if ": " in line)
    for source in library_report["additional_bundled_libraries"].values():
        result = subprocess.run(["dpkg-query", "-S", source], text=True,
                                capture_output=True, check=False)
        if result.returncode == 0:
            packages.update(line.split(": ", 1)[0].split(":", 1)[0]
                            for line in result.stdout.splitlines() if ": " in line)
    for package in sorted(packages):
        copyright_file = Path("/usr/share/doc") / package / "copyright"
        if copyright_file.is_file():
            shutil.copy2(copyright_file, notices / f"{package}.copyright")
    write(docs / "copyright", "JM Studio: JointMotor project source.\n"
          "Third-party Python/Qt wheel licenses are in /opt/jm-studio/_internal/"
          "*.dist-info/.\nUbuntu shared library and Noto font notices are in third-party/.\n")


def assemble(bundle, work, version, library_report, python):
    package_root = work / "package"
    installed = package_root / "opt" / PACKAGE
    installed.parent.mkdir(parents=True)
    shutil.move(str(bundle), installed)
    write(package_root / "usr/bin/jm-studio", "#!/bin/sh\nset -eu\n"
          'state="${XDG_DATA_HOME:-$HOME/.local/share}/jm-studio"\n'
          'mkdir -p "$state"\ncd "$state"\n'
          'exec /opt/jm-studio/JointMotorController "$@"\n', 0o755)
    write(package_root / "usr/share/applications/jm-studio.desktop",
          "[Desktop Entry]\nType=Application\nName=JM Studio\n"
          "Name[zh_CN]=JM Studio 关节电机调试\n"
          "Comment=Joint motor configuration and diagnostics\n"
          "Exec=/usr/bin/jm-studio\nIcon=jm-studio\nTerminal=false\n"
          "Categories=Development;Science;\nStartupNotify=true\n")
    icon = package_root / "usr/share/icons/hicolor/256x256/apps/jm-studio.png"
    icon.parent.mkdir(parents=True)
    shutil.copy2(ROOT / "resources/pic/log_ioc.png", icon)
    docs = package_root / "usr/share/doc" / PACKAGE
    add_licenses(docs, library_report)
    freeze_result = run([python, "-m", "pip", "freeze"], text=True, capture_output=True)
    write(docs / "python-packages.txt", freeze_result.stdout)
    write(docs / "runtime-audit.json", json.dumps(library_report, indent=2) + "\n")
    write(docs / "INSTALL.txt", "Ubuntu 22.04 amd64 offline installation:\n"
          "  sudo dpkg -i ./JMStudio_*_Ubuntu22.04-amd64.deb\n"
          "Launch JM Studio from the desktop application menu or run jm-studio.\n"
          "Python, Qt, plotting libraries, Chinese font and non-glibc ELF dependencies "
          "are bundled. No installation/startup downloads are used.\n"
          "User settings: ${XDG_CONFIG_HOME:-~/.config}/jm-studio/\n"
          "Application data: ${XDG_DATA_HOME:-~/.local/share}/jm-studio/\n"
          "A graphical Ubuntu desktop and working device drivers are required.\n"
          "Serial ports may require adding the user to dialout and signing in again.\n"
          "Linux CAN uses SocketCAN channels (can0, etc.), including PCAN-USB "
          "adapters supported by the kernel. Configure the interface before connecting:\n"
          "  sudo ip link set can0 up type can bitrate 1000000\n"
          "The selected GUI bitrate must match the system interface configuration.\n"
          "For FD, configure the interface with dbitrate and fd on first.\n")
    files = [p for p in package_root.rglob("*") if p.is_file() and not p.is_symlink()]
    size = math.ceil(sum(path.stat().st_size for path in files) / 1024)
    write(package_root / "DEBIAN/control", f"Package: {PACKAGE}\nVersion: {version}\n"
          "Section: electronics\nPriority: optional\nArchitecture: amd64\n"
          "Maintainer: JointMotor project <maintainers@localhost>\n"
          f"Installed-Size: {size}\nDepends: libc6 (>= 2.35)\n"
          "Description: Joint motor desktop configuration and diagnostics\n"
          " Bundled Python and Qt runtime for offline Ubuntu 22.04 amd64 desktops.\n")
    write(package_root / "DEBIAN/md5sums", "".join(
        f"{hashlib.md5(path.read_bytes()).hexdigest()}  {path.relative_to(package_root)}\n"
        for path in sorted(files)))
    for directory in package_root.rglob("*"):
        if directory.is_dir():
            directory.chmod(0o755)
    package_root.chmod(0o755)
    return package_root


def smoke_test(deb, work, output):
    extracted = work / "extracted"
    run(["dpkg-deb", "--extract", deb, extracted])
    home = work / "smoke-home"
    for relative in ("", "config", "data", "runtime", "tmp"):
        path = home / relative
        path.mkdir(exist_ok=True)
        path.chmod(0o700)
        if os.geteuid() == 0:
            os.chown(path, 65534, 65534)
    work.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("PYTHON", "QT_", "QML", "LD_"))}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / "config"),
               XDG_DATA_HOME=str(home / "data"), XDG_RUNTIME_DIR=str(home / "runtime"),
               TMPDIR=str(home / "tmp"), PATH="/usr/bin:/bin", QT_QPA_PLATFORM="xcb",
               JM_PACKAGE_SMOKE_TEST="1",
               JM_PACKAGE_SMOKE_SCREENSHOT=str(home / "smoke.png"),
               JM_PACKAGE_SMOKE_REPORT=str(home / "smoke.json"))
    command = ["xvfb-run", "-a", "-s", "-screen 0 1600x1200x24",
               extracted / "opt" / PACKAGE / EXECUTABLE]
    if os.geteuid() == 0:
        command = ["setpriv", "--reuid=65534", "--regid=65534", "--clear-groups"] + command
        # Verify real SocketCAN TX/RX on an isolated kernel vcan interface.
        env["JM_PACKAGE_CAN_CHANNEL"] = "vcan0"
        command = ["unshare", "--net", "--", "/bin/sh", "-c",
                   '/usr/sbin/ip link add dev vcan0 type vcan && '
                   '/usr/sbin/ip link set vcan0 up && exec "$@"', "jm-can-smoke"] + command
    checks = []
    for restart in (False, True):
        env["JM_PACKAGE_SMOKE_RESTART"] = "1" if restart else "0"
        result = subprocess.run(list(map(str, command)), env=env, cwd=home, timeout=60,
                                text=True, capture_output=True, check=False)
        print(result.stdout, flush=True)
        if result.returncode or "JM_PACKAGE_SMOKE_OK" not in result.stdout:
            raise RuntimeError(f"Package smoke failed ({result.returncode})\n"
                               f"{result.stdout}\n{result.stderr}")
        program_report = json.loads((home / "smoke.json").read_text(encoding="utf-8"))
        if program_report["result"] != "passed" or program_report["version"] != metadata()["version"]:
            raise RuntimeError(f"Frozen release version/diagnostics mismatch: {program_report}")
        checks.append({"restart": restart, "stdout": result.stdout, "stderr": result.stderr,
                       "program": program_report})
    shutil.copy2(home / "smoke.png", output / f"{PACKAGE}_smoke.png")
    return {"result": "passed", "user": "nobody" if os.geteuid() == 0 else "builder",
            "network_isolated": os.geteuid() == 0, "platform": "xcb/Xvfb", "checks": checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "dist/linux")
    parser.add_argument("--full-features", action="store_true")
    parser.add_argument("--release-version", help="Use the version already allocated by the paired build")
    args = parser.parse_args()
    if sys.platform != "linux" or platform.machine() != "x86_64":
        parser.error("Run this script in Ubuntu 22.04 x86_64 (or use build_deb.ps1 in Windows)")
    release = Path("/etc/os-release").read_text()
    if 'VERSION_ID="22.04"' not in release or 'ID=ubuntu\n' not in release:
        parser.error("The supported build baseline is Ubuntu 22.04")
    if sys.version_info[:2] != (3, 10):
        parser.error("Use Ubuntu 22.04's Python 3.10")
    for program in ("dpkg-deb", "ldd", "xvfb-run", "xauth"):
        if not shutil.which(program):
            parser.error(f"Missing {program}; see build prerequisites in this script")
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    python = build_environment()
    application_version = resolve_version(args.release_version)
    version = application_version + "-1ubuntu22.04"
    destination = output / f"{metadata()['slug']}_{application_version}_Ubuntu22.04-amd64.deb"
    with tempfile.TemporaryDirectory(prefix="jm-studio-deb-") as temporary:
        work = Path(temporary)
        resources = stage_resources(work, args.full_features)
        bundle = freeze(python, work, resources)
        library_report = complete_shared_libraries(bundle)
        package_root = assemble(bundle, work, version, library_report, python)
        candidate = work / destination.name
        run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", package_root, candidate])
        run(["dpkg-deb", "--info", candidate])
        smoke = smoke_test(candidate, work, output)
        # Only publish a DEB after the extracted frozen application passes.
        shutil.copy2(candidate, destination)
        report = {"package": destination.name, "version": version,
                  "application_version": application_version, "can_enabled": True,
                  "target": "Ubuntu 22.04 amd64", "builder": platform.platform(),
                  "python": platform.python_version(), "full_features": args.full_features,
                  "runtime_audit": library_report, "smoke_test": smoke,
                  "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}
        write(output / f"{destination.stem}.build.json", json.dumps(report, indent=2) + "\n")
        write(output / f"{destination.name}.sha256", f"{report['sha256']}  {destination.name}\n")
    print(f"\nOffline DEB verified: {destination}\n", flush=True)


if __name__ == "__main__":
    main()
