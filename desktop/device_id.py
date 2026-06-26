"""
Stable machine fingerprint for license binding.
Uses MAC address + platform info hashed to a short string.
"""
import hashlib
import platform
import uuid


def get_device_id() -> str:
    mac = uuid.getnode()
    raw = f"{mac}-{platform.node()}-{platform.system()}-{platform.machine()}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]
