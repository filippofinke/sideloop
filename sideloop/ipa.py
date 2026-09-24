import json
import plistlib
import re
import struct
import zipfile

LC_ENCRYPTION_INFO = (0x21, 0x2C)
CPU_ARM64 = 0x0100000C
FAT_MAGIC = (0xCAFEBABE, 0xCAFEBABF)
MACHO_MAGIC = (0xFEEDFACF, 0xFEEDFACE)


def _cryptid(z, member):
    with z.open(member) as f:
        head = f.read(4096)
        base = 0
        magic = struct.unpack(">I", head[:4])[0]
        if magic in FAT_MAGIC:
            wide = magic == 0xCAFEBABF
            step = 32 if wide else 20
            for i in range(struct.unpack(">I", head[4:8])[0]):
                e = head[8 + i * step: 8 + (i + 1) * step]
                if struct.unpack(">I", e[:4])[0] == CPU_ARM64:
                    base = struct.unpack(">Q", e[8:16])[0] if wide else struct.unpack(">I", e[8:12])[0]
                    break
            f.seek(base)
            head = f.read(4096)
        magic = struct.unpack("<I", head[:4])[0]
        if magic not in MACHO_MAGIC:
            return None
        hsize = 32 if magic == 0xFEEDFACF else 28
        ncmds, sizeofcmds = struct.unpack("<II", head[16:24])
        if hsize + sizeofcmds > len(head):
            f.seek(base)
            head = f.read(hsize + sizeofcmds)
        off = hsize
        for _ in range(ncmds):
            cmd, size = struct.unpack("<II", head[off:off + 8])
            if cmd in LC_ENCRYPTION_INFO:
                return struct.unpack("<I", head[off + 16:off + 20])[0]
            off += size
        return 0


def inspect(path):
    try:
        z = zipfile.ZipFile(path)
    except (zipfile.BadZipFile, OSError):
        return {"error": "not an IPA (an IPA is a zip file)"}
    with z:
        names = z.namelist()
        info = next((n for n in names if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)), None)
        if not info:
            return {"error": "not an iOS app archive (no Payload/*.app/Info.plist)"}
        app = info.rsplit("/", 1)[0]
        p = plistlib.loads(z.read(info))
        exe = p.get("CFBundleExecutable", "")
        try:
            enc = _cryptid(z, f"{app}/{exe}")
        except Exception:
            enc = None
        tweaks = sorted({n.rsplit("/", 1)[1] for n in names if n.startswith(app + "/") and n.endswith(".dylib")})
        appex = sorted({m.group(1) for n in names
                        for m in [re.match(re.escape(app) + r"/PlugIns/([^/]+)\.appex/", n)] if m})
        size = sum(i.file_size for i in z.infolist())
    return {
        "name": p.get("CFBundleDisplayName") or p.get("CFBundleName") or exe,
        "bundle_id": p.get("CFBundleIdentifier", ""),
        "version": p.get("CFBundleShortVersionString", ""),
        "build": p.get("CFBundleVersion", ""),
        "min_ios": p.get("MinimumOSVersion", ""),
        "size_mb": f"{size / 1e6:.0f}",
        "encrypted": {1: "yes", 0: "no"}.get(enc, "unknown"),
        "tweaks": ", ".join(tweaks),
        "tweak_count": len(tweaks),
        "extensions": ", ".join(appex),
        "extension_count": len(appex),
    }


def cached(folder):
    ipa, cache = folder / "app.ipa", folder / "info.json"
    if not ipa.exists():
        return None
    mtime = ipa.stat().st_mtime
    try:
        info = json.loads(cache.read_text())
        if info.get("_mtime") == mtime:
            return info
    except (FileNotFoundError, ValueError):
        pass
    info = inspect(ipa) | {"_mtime": mtime}
    cache.write_text(json.dumps(info))
    return info
