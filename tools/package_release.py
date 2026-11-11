"""Build DSH and ZCode distributions from the same compiled engine."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_FILES = ("package.json", "index.js", "controller.mjs", "client.js",
                "cordis.patch.yml", "README.md", "LICENSE", "install.ps1",
                "Install.cmd", "bss-credentials.example.json")
PRIVATE_NAMES = {".hud_prefs.json", ".hud_state.json", ".hud_instance.json",
                 ".hud.lock", ".hud.log", "local-config.json", "bailian_usage.jsonl",
                 "bss-credentials.json"}
SOURCE_FILES = ("BailianHUD.py", "hud_runtime.py", "hud_tray.py", "README.md",
                "LICENSE", ".gitignore", "requirements.txt", "requirements-build.txt",
                "bss-credentials.example.json")
SOURCE_DIRS = ("assets", "docs", "extras", "licenses", "tests", ".github", "tools")


def files_under(root):
    return sorted(p for p in root.rglob("*") if p.is_file()
                  and "__pycache__" not in p.parts and p.suffix not in {".pyc", ".log"})


def local_secrets(private_roots):
    values = {v for k, v in os.environ.items() if v and any(s in k.upper() for s in
              ("AK_ID", "AK_SECRET", "ACCESS_KEY", "SECURITY_TOKEN", "STS_TOKEN"))}
    paths = [Path.home() / ".dsh" / "qwen-coupon.json",
             Path.home() / ".dsh" / "storages" / "bailian-hud" / ".hud_prefs.json"]
    for root in private_roots:
        paths.extend(root / name for name in (".hud_prefs.json", ".hud_state.json", "bss-credentials.json"))
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError):
            continue
        for key in ("api_token", "coupon_no", "coupon_id", "coupon", "aliyunAkId", "aliyunAkSecret",
                    "accessKeyId", "accessKeySecret", "securityToken", "aliyunStsToken"):
            value = data.get(key)
            if isinstance(value, str):
                values.add(value)
    return [v.encode("utf-8") for v in values if len(v) >= 8]


def check_files(files, needles):
    for path in files:
        if path.name in PRIVATE_NAMES or path.suffix == ".lnk":
            raise RuntimeError(f"Runtime data found: {path.name}")
        content = path.read_bytes()
        if any(value in content for value in needles):
            raise RuntimeError(f"Local credential or coupon identifier found: {path.name}")


def make_zip(path, members):
    with zipfile.ZipFile(path, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source, target in members:
            archive.write(source, target)
    with zipfile.ZipFile(path) as archive:
        if archive.testzip():
            raise RuntimeError(f"Invalid ZIP: {path.name}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, default=ROOT / "dist" / "BailianHUD")
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "packages")
    parser.add_argument("--private-root", type=Path, action="append", default=[ROOT])
    args = parser.parse_args()
    binary, output = args.binary.resolve(), args.output.resolve()
    if not (binary / "BailianHUD.exe").is_file():
        parser.error("Build BailianHUD.exe first")
    if output == ROOT:
        parser.error("Output must be a distribution directory")
    output.mkdir(parents=True, exist_ok=True)
    version = json.loads((ROOT / "dsh-plugin" / "package.json").read_text(encoding="utf-8"))["version"]
    final_targets = [output / name for name in ("BailianHUD-DSH-windows-x64.zip",
               f"dsh-bailian-hud-{version}.tgz", "BailianHUD-ZCode-windows-x64.zip", "BailianHUD-source.zip")]
    if any(path.exists() for path in final_targets):
        parser.error("Distribution already exists; choose a new output directory")
    source = [(ROOT / name, Path("BailianHUD") / name) for name in SOURCE_FILES]
    for name in SOURCE_DIRS:
        source.extend((p, Path("BailianHUD") / p.relative_to(ROOT)) for p in files_under(ROOT / name))
    for name in PLUGIN_FILES:
        source.append((ROOT / "dsh-plugin" / name, Path("BailianHUD/dsh-plugin") / name))
    source.extend((p, Path("BailianHUD/dsh-plugin") / p.relative_to(ROOT / "dsh-plugin"))
                  for p in files_under(ROOT / "dsh-plugin" / "tests"))
    needles = local_secrets(args.private_root)
    check_files([p for p, _ in source] + files_under(binary), needles)

    with tempfile.TemporaryDirectory(prefix="bailian-package-", dir=output) as raw:
        built = Path(raw) / "packages"
        built.mkdir()
        targets = [built / path.name for path in final_targets]
        stage = Path(raw) / "dsh-bailian-hud"
        stage.mkdir()
        for name in PLUGIN_FILES:
            shutil.copy2(ROOT / "dsh-plugin" / name, stage / name)
        shutil.copy2(ROOT / "README.md", stage / "README.md")
        shutil.copytree(ROOT / "docs", stage / "docs")
        shutil.copytree(ROOT / "licenses", stage / "licenses")
        shutil.copytree(binary, stage / "bin" / "windows-x64")
        check_files(files_under(stage), needles)
        make_zip(targets[0], [(p, Path("dsh-bailian-hud") / p.relative_to(stage)) for p in files_under(stage)])
        npm = shutil.which("npm.cmd") or shutil.which("npm")
        if not npm:
            raise RuntimeError("npm is required to create the standard DSH plugin package")
        packed = subprocess.run([npm, "pack", "--ignore-scripts", "--offline", "--json",
                                 "--cache", str(Path(raw) / "npm-cache"),
                                 "--pack-destination", str(built)], cwd=stage,
                                capture_output=True, text=True)
        if packed.returncode:
            raise RuntimeError(f"npm pack failed: {packed.stderr}")
        if json.loads(packed.stdout)[0]["filename"] != targets[1].name:
            raise RuntimeError("Unexpected npm package name")
        with tarfile.open(targets[1], "r:gz") as archive:
            members = archive.getmembers()
            names = {m.name for m in members if m.isfile()}
            if "package/bin/windows-x64/BailianHUD.exe" not in names:
                raise RuntimeError("Plugin engine missing from TGZ")
            if any(Path(name).name in PRIVATE_NAMES for name in names):
                raise RuntimeError("Runtime data in TGZ")
            for member in members:
                if member.isfile():
                    content = archive.extractfile(member).read()
                    if any(value in content for value in needles):
                        raise RuntimeError("Local credential in TGZ")

        windows = [(p, p.relative_to(binary)) for p in files_under(binary)]
        windows += [(ROOT / name, Path(name)) for name in ("README.md", "LICENSE", "bss-credentials.example.json")]
        for name in ("licenses", "extras", "docs"):
            windows.extend((p, p.relative_to(ROOT)) for p in files_under(ROOT / name))
        make_zip(targets[2], windows)
        make_zip(targets[3], source)
        for temporary, final in zip(targets, final_targets):
            if final.exists():
                raise RuntimeError(f"Distribution appeared during packaging: {final.name}")
            temporary.rename(final)
    targets = final_targets
    (output / "SHA256SUMS.txt").write_text("".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in targets), encoding="utf-8")
    for path in targets:
        print(f"{path.name}: {path.stat().st_size / 1048576:.2f} MiB; integrity OK")
    print(f"Known local credential/identifier scan: {len(needles)} values; no matches")


if __name__ == "__main__":
    main()
