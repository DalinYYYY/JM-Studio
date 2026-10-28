"""Allocate one application version for a standalone build or a paired release."""

import argparse
import ast
import json
import os
from pathlib import Path
import re
import stat
import tempfile

ROOT = Path(__file__).resolve().parents[1]
VERSION_FILE = ROOT / "ui/__init__.py"


def metadata(path=VERSION_FILE):
    values = {}
    for node in ast.parse(path.read_text(encoding="utf-8-sig")).body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ("APP_NAME", "APP_VERSION"):
                    values[target.id] = node.value.value
    version = values["APP_VERSION"]
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError(f"Expected major.minor.patch, got {version!r}")
    return {"name": values["APP_NAME"], "version": version,
            "slug": re.sub(r"[^A-Za-z0-9-]", "", values["APP_NAME"])}


def resolve_version(expected=None, path=VERSION_FILE):
    """Bump patch by default; child builds must verify the parent's exact version."""
    lock = path.parent.parent / "build" / "release-version.lock"
    lock.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError(f"Another release is updating the version: {lock}") from exc
    temporary = None
    try:
        os.close(descriptor)
        current = metadata(path)["version"]
        if expected is not None:
            if current != expected:
                raise ValueError(f"Release version mismatch: source={current}, requested={expected}")
            return current
        parts = list(map(int, current.split(".")))
        parts[2] += 1
        updated = ".".join(map(str, parts))
        original = path.read_bytes()
        pattern = rb'^(APP_VERSION\s*=\s*")' + re.escape(current.encode("ascii")) + rb'(")'
        replacement, count = re.subn(pattern, lambda m: m[1] + updated.encode("ascii") + m[2],
                                     original, count=1, flags=re.MULTILINE)
        if count != 1:
            raise ValueError("APP_VERSION assignment was not found exactly once")
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".release-version-",
                                         delete=False) as output:
            temporary = Path(output.name)
            output.write(replacement)
        temporary.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary, path)
        temporary = None
        return updated
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        lock.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--bump", action="store_true")
    group.add_argument("--expect")
    group.add_argument("--metadata", action="store_true")
    parser.add_argument("--windows-version-file", type=Path)
    args = parser.parse_args()
    if args.metadata:
        print(json.dumps(metadata(), ensure_ascii=True))
    else:
        version = resolve_version(args.expect)
        if args.windows_version_file:
            parts = tuple(map(int, version.split("."))) + (0,)
            name = metadata()["name"]
            content = ("VSVersionInfo(ffi=FixedFileInfo("
                       f"filevers={parts!r}, prodvers={parts!r}, mask=0x3f, flags=0, "
                       "OS=0x40004, fileType=1, subtype=0, date=(0, 0)), kids=["
                       "StringFileInfo([StringTable('040904B0', ["
                       f"StringStruct('FileDescription', {name!r}), "
                       f"StringStruct('ProductName', {name!r}), "
                       f"StringStruct('FileVersion', {version!r}), "
                       f"StringStruct('ProductVersion', {version!r})])]), "
                       "VarFileInfo([VarStruct('Translation', [1033, 1200])])])\n")
            args.windows_version_file.parent.mkdir(parents=True, exist_ok=True)
            args.windows_version_file.write_text(content, encoding="utf-8")
        print(version)


if __name__ == "__main__":
    main()
