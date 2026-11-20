"""Control an actual container or SSH guest without changing the construction notebooks."""
from dataclasses import dataclass
import os
import shlex
import subprocess
import time


@dataclass
class Guest:
    name: str
    address: str
    transport: str = "docker"

    def command(self, argv, *, stdin=False):
        if self.transport == "docker":
            return ["docker", "exec", *(["-i"] if stdin else []), self.name, *argv]
        if self.transport == "ssh":
            return ["ssh", *shlex.split(os.environ["ANSIBLE_SSH_ARGS"]), "centos@" + self.address,
                    "sudo -- " + shlex.join(argv)]
        raise ValueError("Unknown guest transport: " + self.transport)

    def run(self, argv, *, input=None, check=True, timeout=180):
        return subprocess.run(self.command(argv, stdin=input is not None), input=input,
            text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, check=check, timeout=timeout)

    def output(self, argv):
        return self.run(argv).stdout.strip()

    def restart(self):
        if self.transport == "docker":
            subprocess.run(["docker", "restart", "--time", "30", self.name], check=True, timeout=180)
            return {"operation": "container_restart"}
        before = self.output(["cat", "/proc/sys/kernel/random/boot_id"])
        self.run(["systemctl", "reboot"], check=False, timeout=15)
        for attempt in range(120):
            try:
                after = self.output(["cat", "/proc/sys/kernel/random/boot_id"])
                if after != before:
                    return {"operation": "os_reboot", "boot_id_before": before, "boot_id_after": after}
            except (subprocess.SubprocessError, OSError):
                pass
            time.sleep(2)
        raise AssertionError("Guest OS did not reboot: " + self.name)
