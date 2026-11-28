"""Inspect the container and CPU of a 64-bit native library without loading it."""

import struct
from itertools import pairwise

from packaging.tags import parse_tag


def _mach_cpu(data):
    endian = {b"\xcf\xfa\xed\xfe": "<", b"\xfe\xed\xfa\xcf": ">"}.get(data[:4])
    if not endian or len(data) < 32:
        return None
    cpu, _, kind = struct.unpack_from(endian + "III", data, 4)
    if kind in (6, 8):  # MH_DYLIB or MH_BUNDLE
        return {0x01000007: "x86_64", 0x0100000C: "arm64"}.get(cpu)
    return None


def _architectures(data):
    cpu = _mach_cpu(data)
    if cpu:
        return "macos", {cpu}
    fat = {
        b"\xca\xfe\xba\xbe": (">", False),
        b"\xbe\xba\xfe\xca": ("<", False),
        b"\xca\xfe\xba\xbf": (">", True),
        b"\xbf\xba\xfe\xca": ("<", True),
    }.get(data[:4])
    if fat and len(data) >= 8:
        endian, wide = fat
        count = struct.unpack_from(endian + "I", data, 4)[0]
        stride = 32 if wide else 20
        table_end = 8 + count * stride
        if not 1 <= count <= 32 or table_end > len(data):
            return "", set()
        slices, cpus = [], set()
        for index in range(count):
            entry = 8 + index * stride
            declared, _, offset, size = struct.unpack_from(
                endian + ("IIQQ" if wide else "IIII"), data, entry
            )
            if size < 32 or offset < table_end or offset + size > len(data):
                return "", set()
            actual = _mach_cpu(data[offset : offset + 32])
            if (
                actual is None
                or actual != {0x01000007: "x86_64", 0x0100000C: "arm64"}.get(declared)
                or actual in cpus
            ):
                return "", set()
            slices.append((offset, offset + size))
            cpus.add(actual)
        slices.sort()
        if any(a[1] > b[0] for a, b in pairwise(slices)):
            return "", set()
        return "macos", cpus
    if len(data) >= 64 and data.startswith(b"\x7fELF\x02\x01\x01"):
        kind, machine = struct.unpack_from("<HH", data, 16)
        if kind == 3 and machine in (62, 183):  # ET_DYN
            return "linux", {"x86_64" if machine == 62 else "arm64"}
    if len(data) >= 64 and data.startswith(b"MZ"):
        offset = struct.unpack_from("<I", data, 60)[0]
        if offset >= 64 and offset + 26 <= len(data) and data[offset : offset + 4] == b"PE\0\0":
            machine = struct.unpack_from("<H", data, offset + 4)[0]
            size, flags, magic = struct.unpack_from("<HHH", data, offset + 20)
            if (
                size >= 2
                and offset + 24 + size <= len(data)
                and flags & 0x2000
                and magic == 0x20B
                and machine in (0x8664, 0xAA64)
            ):
                return "windows", {"x86_64" if machine == 0x8664 else "arm64"}
    return "", set()


def matches_native_targets(data: bytes, targets: tuple[str, ...]) -> bool:
    system, cpus = _architectures(data)
    prefixes = {
        "macos": ("macosx_",),
        "linux": ("manylinux", "musllinux", "linux_"),
        "windows": ("win_",),
    }
    if not cpus:
        return False
    for target in targets:
        for tag in parse_tag(target):
            platform = tag.platform
            if not platform.startswith(prefixes[system]):
                return False
            if platform.endswith("_universal2") and system == "macos":
                required = {"x86_64", "arm64"}
            elif platform.endswith(("_arm64", "_aarch64")):
                required = {"arm64"}
            elif platform.endswith(("_amd64", "_x86_64")):
                required = {"x86_64"}
            else:
                return False
            if not required.issubset(cpus):
                return False
    return True
