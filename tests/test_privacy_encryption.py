"""Encryption-at-rest self-check: parses manage-bde / lsblk output (mocked here), warns, never blocks."""
import pytest

from ledger_room import privacy

BDE_ON = """BitLocker Drive Encryption: Configuration Tool version 10.0.26100
Volume E: [Data]
    Size:                 931.51 GB
    Conversion Status:    Fully Encrypted
    Percentage Encrypted: 100.0%
    Protection Status:    Protection On
"""
BDE_OFF = """Volume E: [Data]
    Conversion Status:    Fully Decrypted
    Percentage Encrypted: 0.0%
    Protection Status:    Protection Off
"""
BDE_SUSPENDED = BDE_ON.replace("Protection On", "Protection Off")
BDE_ZH_OFF = """磁碟區 E: [Data]
    轉換狀態:    完全解密
    加密百分比: 0.0%
    保護狀態:    保護關閉
"""
BDE_DENIED = "錯誤: 存取必要資源的嘗試被拒。\n請確認您擁有電腦的系統管理權限。"


def fake(answers):
    calls = []

    def run(cmd):
        calls.append(cmd)
        for key, val in answers.items():
            if cmd[0] == key:
                return val
        return None, ""
    run.calls = calls
    return run


@pytest.mark.parametrize("text,status", [(BDE_ON, "encrypted"), (BDE_OFF, "not_encrypted"),
                                         (BDE_SUSPENDED, "suspended"), (BDE_ZH_OFF, "not_encrypted"),
                                         (BDE_DENIED, None), ("", None)])
def test_parse_manage_bde(text, status):
    assert privacy.parse_manage_bde(text) == status


def test_windows_uses_manage_bde_then_falls_back_to_shell_property():
    r = privacy.volume_encryption("E:/x", run=fake({"manage-bde": (0, BDE_ON)}), system="Windows")
    assert r["status"] == "encrypted" and r["volume"].upper() == "E:"
    run = fake({"manage-bde": (0, BDE_DENIED), "powershell": (0, "2\r\n")})
    r = privacy.volume_encryption("E:/x", run=run, system="Windows")
    assert r["status"] == "not_encrypted" and "Shell" in r["method"] and len(run.calls) == 2
    assert privacy.volume_encryption("E:/x", run=fake({"manage-bde": (0, BDE_DENIED), "powershell": (0, "1")}),
                                     system="Windows")["status"] == "encrypted"
    assert privacy.volume_encryption("E:/x", run=fake({}), system="Windows")["status"] == "unknown"


def test_linux_lsblk_crypt():
    run = fake({"findmnt": (0, "/dev/mapper/vg-root\n"), "lsblk": (0, "lvm\ncrypt\npart\ndisk\n")})
    r = privacy.volume_encryption("/srv/ledger/data", run=run, system="Linux")
    assert r["status"] == "encrypted" and r["volume"] == "/dev/mapper/vg-root"
    assert run.calls[1] == ["lsblk", "-s", "-n", "-o", "TYPE", "/dev/mapper/vg-root"]
    run = fake({"findmnt": (0, "/dev/nvme0n1p2\n"), "lsblk": (0, "part\ndisk\n")})
    assert privacy.volume_encryption("/srv", run=run, system="Linux")["status"] == "not_encrypted"
    assert privacy.volume_encryption("/srv", run=fake({"findmnt": (0, "tmpfs\n")}), system="Linux")["status"] == "unknown"
    assert privacy.volume_encryption("/srv", run=fake({}), system="Linux")["status"] == "unknown"


def test_self_check_warns_but_never_raises():
    msgs = []
    r = privacy.encryption_self_check("E:/data", run=fake({"manage-bde": (0, BDE_OFF)}), system="Windows",
                                      warn=msgs.append)
    assert r["status"] == "not_encrypted" and len(msgs) == 1 and "NOT encrypted" in msgs[0]
    msgs.clear()
    r = privacy.encryption_self_check("E:/data", run=fake({"manage-bde": (0, BDE_ON)}), system="Windows",
                                      warn=msgs.append)
    assert r["status"] == "encrypted" and msgs == [] and "warning" not in r

    def boom(cmd):
        raise RuntimeError("no shell")
    r = privacy.encryption_self_check("E:/data", run=boom, system="Windows", warn=msgs.append)
    assert r["status"] == "unknown" and "could not be checked" in msgs[0]

    def bad_warn(m):
        raise OSError("closed stderr")
    assert privacy.encryption_self_check("/x", run=fake({}), system="Linux", warn=bad_warn)["status"] == "unknown"
