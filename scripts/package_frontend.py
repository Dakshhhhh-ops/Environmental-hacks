"""Create a reproducible static-site ZIP for Amplify manual deployment."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

root = Path(__file__).resolve().parents[1]
source = root / "frontend"
output = root / "dist" / "underpass-sentinel-frontend.zip"
output.parent.mkdir(exist_ok=True)
excluded = {".DS_Store", "Thumbs.db"}
with ZipFile(output, "w", ZIP_DEFLATED, compresslevel=9) as archive:
    for path in sorted(p for p in source.rglob("*") if p.is_file() and p.name not in excluded):
        relative = path.relative_to(source).as_posix()
        info = ZipInfo(relative, date_time=(2020, 1, 1, 0, 0, 0))
        info.compress_type = ZIP_DEFLATED
        info.external_attr = 0o100644 << 16
        archive.writestr(info, path.read_bytes(), compress_type=ZIP_DEFLATED, compresslevel=9)
print(output)
