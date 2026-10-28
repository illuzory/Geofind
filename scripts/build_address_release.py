"""Package the working tree and examples without Git, secrets or runtime state.

Does not commit, push, clone or publish. The original workbook is included only
when explicitly supplied for this private downloadable archive.
"""

import argparse
import hashlib
import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]


def build(output, original=None):
    files = {}
    root_names = [
        "README.md",
        "app.py",
        "pyproject.toml",
        "requirements.lock",
        ".gitignore",
        ".gitattributes",
    ]
    for name in root_names:
        files[name] = (ROOT / name).read_bytes()
    for folder in [
        "geofind",
        "scripts",
        "tests",
        "assets",
        "docs",
        "data",
        "examples",
        ".streamlit",
        ".github",
    ]:
        for path in (ROOT / folder).rglob("*"):
            if (
                not path.is_file()
                or path.is_symlink()
                or any(
                    p in {"__pycache__", ".pytest_cache", ".ruff_cache"}
                    for p in path.parts
                )
            ):
                continue
            relative = path.relative_to(ROOT).as_posix()
            if (
                path.suffix in {".sqlite", ".pyc"}
                or path.name == "secrets.toml"
                or relative == "data/equipment_inventory_mvp.xlsx"
            ):
                continue
            files[relative] = path.read_bytes()
    files["examples/01_ural_base.xlsx"] = (
        ROOT / "data/ural_inventory.xlsx"
    ).read_bytes()
    reference = json.loads((ROOT / "data/address_reference.json").read_text())
    discovery = {
        value["query"]: value["candidates"] for value in reference["responses"].values()
    }
    files["source/address_discovery.json"] = json.dumps(
        discovery, ensure_ascii=False, indent=2
    ).encode()
    if original:
        files["source/equipment_inventory_original.xlsx"] = Path(original).read_bytes()
    snapshots = ROOT / "artifacts/15_sites_validation"
    for path in snapshots.iterdir():
        if path.is_file() and path.suffix in {".html", ".csv", ".json", ".png"}:
            files["snapshots/" + path.name] = path.read_bytes()
    files["MANIFEST.sha256.json"] = (
        json.dumps(
            {
                name: hashlib.sha256(raw).hexdigest()
                for name, raw in sorted(files.items())
            },
            indent=2,
        ).encode()
        + b"\n"
    )
    output = Path(output)
    output.parent.mkdir(exist_ok=True, parents=True)
    with ZipFile(output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, raw in sorted(files.items()):
            info = ZipInfo("geofind_address_mvp/" + name)
            info.compress_type = ZIP_DEFLATED
            info.external_attr = (0o100755 if name.endswith(".sh") else 0o100644) << 16
            archive.writestr(info, raw, compresslevel=9)
    print(
        json.dumps(
            {
                "archive": str(output),
                "files": len(files),
                "bytes": output.stat().st_size,
                "git_operations": False,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="artifacts/geofind-15-sites-mvp.zip")
    parser.add_argument("--original")
    args = parser.parse_args()
    build(args.output, args.original)
