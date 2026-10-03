from pathlib import Path
from typing import Optional

WORKSPACE = Path("./workspace").resolve()
WORKSPACE.mkdir(exist_ok=True)

# 三档沙箱：0=read-only, 1=workspace(写+执行前审批), 2=full-auto
SANDBOX_MODE = 1

def set_sandbox_mode(mode: int):
    global SANDBOX_MODE
    if mode not in (0, 1, 2):
        raise ValueError(f"沙箱档位必须是 0/1/2，收到 {mode}")
    SANDBOX_MODE = mode

def safe_path(raw_path: str) -> Optional[Path]:
    """所有文件操作必须经过这里。返回 None 表示越界。"""
    target = (WORKSPACE / raw_path).resolve()
    if WORKSPACE in target.parents or target == WORKSPACE:
        return target
    return None

def is_read_only() -> bool:
    return SANDBOX_MODE == 0

def is_full_auto() -> bool:
    return SANDBOX_MODE == 2

def is_safe_command(cmd: str) -> bool:
    """黑名单过滤：不论档位，下列命令一律拒绝。"""
    blacklist = [
        "rm -rf /", "rm -rf /*", "mkfs", ":(){ :|:& };:",
        "chmod 777 /", "sudo", "shutdown", "reboot",
        "dd if=/dev/", ">: /dev/",
    ]
    for bad in blacklist:
        if bad in cmd:
            return False
    return True
