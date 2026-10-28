"""Build one versioned Windows + offline Ubuntu release and a combined ZIP."""

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import zipfile

from release_version import ROOT, metadata, resolve_version


def sha256(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def run_logged(command, log):
    print("Building:", log.name, flush=True)
    with log.open("wb") as output:
        result = subprocess.run(list(map(str, command)), stdout=output, stderr=subprocess.STDOUT)
    if result.returncode:
        print(log.read_text(encoding="utf-8", errors="replace")[-10000:], flush=True)
        raise RuntimeError(f"Build failed ({result.returncode}); see {log}")


def windows_smoke(executable, work, docs, version):
    home = work / "windows-smoke"
    for name in ("Roaming", "Local", "tmp"):
        (home / name).mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, APPDATA=str(home / "Roaming"), LOCALAPPDATA=str(home / "Local"),
               TEMP=str(home / "tmp"), TMP=str(home / "tmp"), JM_PACKAGE_SMOKE_TEST="1",
               JM_PACKAGE_SMOKE_REPORT=str(home / "result.json"),
               JM_PACKAGE_SMOKE_SCREENSHOT=str(docs / f"Windows_{version}.png"))
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = 0
    reports = []
    for restart in (False, True):
        env["JM_PACKAGE_SMOKE_RESTART"] = "1" if restart else "0"
        result_path = home / "result.json"
        result_path.unlink(missing_ok=True)
        result = subprocess.run([str(executable)], env=env, cwd=home, timeout=120,
                                startupinfo=startup)
        if not result_path.is_file():
            raise RuntimeError(f"Windows executable did not write a smoke report: exit={result.returncode}")
        report = json.loads(result_path.read_text(encoding="utf-8"))
        if result.returncode or report["result"] != "passed" or report["version"] != version:
            raise RuntimeError(f"Windows package smoke failed: {report}")
        if report["can"]["backend"] != "pcan" or not report["can"].get("pcan_dll_loaded"):
            raise RuntimeError(f"Frozen PCAN backend is unavailable: {report}")
        reports.append(report)
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--distribution", default="Ubuntu-22.04")
    parser.add_argument("--release-version", help="Resume a failed paired build without allocating another version")
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("Run build_release.ps1 on Windows")
    if not Path(os.environ["WINDIR"], "System32/PCANBasic.dll").is_file():
        parser.error("64-bit PCANBasic.dll is required")
    prerequisites = subprocess.run(["wsl.exe", "-d", args.distribution, "--exec", "python3", "--version"])
    if prerequisites.returncode:
        parser.error(f"WSL distribution unavailable: {args.distribution}")
    version = resolve_version(args.release_version)
    app = metadata()
    base = f"{app['slug']}_{version}_Windows-x64+Ubuntu22.04-amd64"
    releases = ROOT / "dist/releases"
    releases.mkdir(parents=True, exist_ok=True)
    directory = releases / base
    archive = releases / f"{base}.zip"
    if directory.exists() or archive.exists():
        raise RuntimeError(f"Release already exists; refusing to overwrite {base}")
    day = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d")
    docs = ROOT.parents[2] / ".doc/系统设计" / f"双平台发布_{day}"
    docs.mkdir(parents=True, exist_ok=True)
    build_root = ROOT / "build"
    build_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="paired-release-", dir=build_root) as temporary:
        work = Path(temporary).resolve()
        if not work.is_relative_to(build_root.resolve()):
            raise RuntimeError("Release work directory escaped the workspace")
        staged = work / base
        staged.mkdir()
        windows = work / "windows"
        linux = work / "linux"
        run_logged(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    ROOT / "packaging/build_exe.ps1", "-Python", sys.executable, "-OneFile",
                    "-SkipInstall", "-Clean", "-ReleaseVersion", version, "-OutputDir", windows],
                   docs / f"Windows_{version}.log")
        exe_name = f"{app['slug']}_{version}_Windows-x64.exe"
        import pefile
        pe = pefile.PE(str(windows / exe_name))
        fixed = pe.VS_FIXEDFILEINFO[0]
        file_version = (fixed.FileVersionMS >> 16, fixed.FileVersionMS & 0xffff,
                        fixed.FileVersionLS >> 16, fixed.FileVersionLS & 0xffff)
        pe.close()
        if file_version[:3] != tuple(map(int, version.split("."))):
            raise RuntimeError(f"Windows file version differs: {file_version}")
        win_reports = windows_smoke(windows / exe_name, work, docs, version)
        run_logged(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                    ROOT / "packaging/build_deb.ps1", "-Distribution", args.distribution,
                    "-ReleaseVersion", version, "-OutputDir", linux], docs / f"Ubuntu_{version}.log")
        deb_name = f"{app['slug']}_{version}_Ubuntu22.04-amd64.deb"
        linux_report = json.loads((linux / f"{Path(deb_name).stem}.build.json").read_text(encoding="utf-8"))
        if linux_report["application_version"] != version or not linux_report["can_enabled"]:
            raise RuntimeError("Ubuntu release version/CAN configuration differs")
        for check in linux_report["smoke_test"]["checks"]:
            if check["program"]["can"].get("test_interface") != "socketcan":
                raise RuntimeError("Ubuntu package did not pass native SocketCAN TX/RX verification")
        shutil.copy2(linux / f"{Path(deb_name).stem}.build.json", docs / f"Ubuntu_{version}.build.json")
        shutil.copy2(linux / "jm-studio_smoke.png", docs / f"Ubuntu_{version}.png")
        for source in (windows / exe_name, linux / deb_name):
            shutil.copy2(source, staged / source.name)
        artifacts = [{"file": name, "software_version": version, "platform": platform,
                      "can_backend": backend, "bytes": (staged / name).stat().st_size,
                      "sha256": sha256(staged / name)}
                     for name, platform, backend in ((exe_name, "Windows-x64", "pcan"),
                                                     (deb_name, "Ubuntu22.04-amd64", "socketcan"))]
        manifest = {"software": app["name"], "version": version, "can_enabled": True,
                    "windows_file_version": ".".join(map(str, file_version)),
                    "artifacts": artifacts, "windows_verification": win_reports,
                    "ubuntu_verification": linux_report["smoke_test"]}
        (staged / "release.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        (staged / "SHA256SUMS.txt").write_text("".join(
            f"{artifact['sha256']}  {artifact['file']}\n" for artifact in artifacts), encoding="ascii")
        guide = (f"{app['name']} {version} / Windows x64 + Ubuntu 22.04 amd64\n\n"
                 f"Windows: run {exe_name}. PCAN-Basic runtime is bundled; install the PEAK hardware driver for CAN.\n"
                 f"Ubuntu offline installation: sudo dpkg -i ./{deb_name}\n"
                 "Ubuntu launch: jm-studio\n"
                 "Ubuntu CAN: configure the SocketCAN device first, for example:\n"
                 "  sudo ip link set can0 up type can bitrate 1000000\n"
                 "Select CAN / can0 in the app; match the GUI bitrate to the system interface.\n"
                 "Both programs bundle Python, Qt, plotting libraries and CAN backends.\n"
                 "Settings: Windows %APPDATA%/JMStudio; Ubuntu ~/.config/jm-studio.\n\n"
                 "Verified: packaged GUI, matching versions, CAN TX/RX, restart persistence.\n"
                 "Windows: PCAN backend and bundled DLL load; protocol TX/RX through a virtual bus.\n"
                 "Ubuntu: SocketCAN TX/RX through an isolated kernel vcan interface; "
                 "the diagnostic peer's local-origin flag is normalized to model an external device.\n"
                 "Physical adapter/motor communication has not been tested for this release.\n")
        (staged / "使用说明.txt").write_text(guide, encoding="utf-8-sig")
        (docs / f"{base}.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        if metadata()["version"] != version:
            raise RuntimeError("Source version changed during the paired build")
        shutil.copytree(staged, directory)
        with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as output:
            for path in sorted(directory.iterdir()):
                output.write(path, f"{base}/{path.name}")
        with zipfile.ZipFile(archive) as output:
            bad = output.testzip()
            if bad:
                raise RuntimeError(f"ZIP integrity check failed: {bad}")
        archive.with_suffix(".zip.sha256").write_text(f"{sha256(archive)}  {archive.name}\n", encoding="ascii")
    print(f"\nRelease version: {version}\nRelease folder: {directory}\nRelease ZIP: {archive}", flush=True)


if __name__ == "__main__":
    main()
