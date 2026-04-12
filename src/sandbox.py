#!/usr/bin/env python3
"""
Run scripts in QEMU pedantically.
We start QEMU on each run with no network services.
The ./share directory is shared into the guest at /mnt/share via 9p.
For normal runs, exactly one *.sh script is expected under ./share/tmp.
For --install, we boot an initramfs helper that mounts /dev/vda2 at /newroot,
installs one-run systemd files into that rootfs, syncs, and powers off.
Before install boot, we create an automatic disk snapshot for rollback safety.
On the next normal boot, the one-run service executes the single /mnt/share/tmp/*.sh and powers off.
"""

import argparse
import gzip
import json
import os
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path


class QMPClient:
    """Minimal QMP client over a UNIX socket."""

    def __init__(self, socket_path):
        self.socket_path = str(socket_path)
        self.sock = None
        self._recv_buffer = ""

    def connect(self, timeout=10.0, retry_interval=0.1):
        """Connect and negotiate QMP capabilities."""
        deadline = time.monotonic() + timeout
        last_error = None

        while time.monotonic() < deadline:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                sock.connect(self.socket_path)
                self.sock = sock

                greeting = self._read_message(timeout=2.0)
                if "QMP" not in greeting:
                    raise RuntimeError(f"Invalid QMP greeting: {greeting}")

                self.execute("qmp_capabilities", timeout=2.0)
                return greeting

            except (
                FileNotFoundError,
                ConnectionRefusedError,
                TimeoutError,
                OSError,
                RuntimeError,
                json.JSONDecodeError,
            ) as exc:
                last_error = exc
                sock.close()
                self.sock = None
                time.sleep(retry_interval)

        raise RuntimeError(
            f"Failed to connect to QMP socket {self.socket_path}: {last_error}"
        )

    def close(self):
        if self.sock is not None:
            self.sock.close()
            self.sock = None

    def execute(self, command, arguments=None, timeout=5.0):
        """Execute a QMP command and return its result."""
        if self.sock is None:
            raise RuntimeError("QMP socket is not connected")

        payload = {"execute": command}
        if arguments:
            payload["arguments"] = arguments

        self.sock.sendall((json.dumps(payload) + "\r\n").encode("utf-8"))

        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Timed out waiting for QMP command '{command}'")

            msg = self._read_message(timeout=remaining)
            if "event" in msg:
                continue

            if "error" in msg:
                err = msg["error"]
                raise RuntimeError(
                    f"QMP {command} failed ({err.get('class', 'Error')}): {err.get('desc', 'unknown')}"
                )

            if "return" in msg:
                return msg["return"]

    def _read_message(self, timeout=5.0):
        if self.sock is None:
            raise RuntimeError("QMP socket is not connected")

        deadline = time.monotonic() + timeout
        while True:
            newline_index = self._recv_buffer.find("\n")
            if newline_index != -1:
                line = self._recv_buffer[:newline_index].strip()
                self._recv_buffer = self._recv_buffer[newline_index + 1 :]
                if not line:
                    continue
                return json.loads(line)

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("Timed out waiting for QMP response")

            self.sock.settimeout(min(0.2, remaining))
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("QMP socket closed by peer")
            self._recv_buffer += chunk.decode("utf-8")


def build_default_qmp_socket_path():
    qmp_dir = Path("./tmp")
    qmp_dir.mkdir(parents=True, exist_ok=True)
    return qmp_dir / f"qmp-{os.getpid()}-{int(time.time())}.sock"


def run_qemu_with_qmp(cmd, qmp_socket_path):
    """Start QEMU and control it through QMP where possible."""
    qemu_proc = subprocess.Popen(cmd)
    qmp = QMPClient(qmp_socket_path)
    qmp_connected = False

    try:
        try:
            greeting = qmp.connect(timeout=10.0)
            qmp_connected = True

            qemu_version = greeting.get("QMP", {}).get("version", {}).get("qemu", {})
            if qemu_version:
                print(
                    "QMP connected to QEMU "
                    f"{qemu_version.get('major', 0)}."
                    f"{qemu_version.get('minor', 0)}."
                    f"{qemu_version.get('micro', 0)}"
                )

            status = qmp.execute("query-status")
            runstate = status.get("status")
            if runstate:
                print(f"QMP runstate: {runstate}")

        except Exception as exc:
            print(
                f"Warning: Could not establish QMP control ({exc}). "
                "QEMU will continue without QMP commands.",
                file=sys.stderr,
            )

        return qemu_proc.wait()

    except KeyboardInterrupt:
        print("\nInterrupted by user")

        if qmp_connected:
            try:
                print("Sending QMP system_powerdown request...")
                qmp.execute("system_powerdown", timeout=2.0)
                try:
                    return qemu_proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass

                print("Guest did not power down in time. Sending QMP quit...")
                qmp.execute("quit", timeout=2.0)
            except Exception as exc:
                print(f"Warning: QMP shutdown failed: {exc}", file=sys.stderr)

        if qemu_proc.poll() is None:
            qemu_proc.terminate()
            try:
                qemu_proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                qemu_proc.kill()
                qemu_proc.wait()
        return 130

    finally:
        qmp.close()
        qmp_socket_path = Path(qmp_socket_path)
        if qmp_socket_path.exists() or qmp_socket_path.is_socket():
            qmp_socket_path.unlink()


def check_directories():
    """Check that required directories exist."""
    share_dir = Path("./share")
    tmp_dir = share_dir / "tmp"

    missing = []
    if not share_dir.exists():
        missing.append(str(share_dir))
    if not tmp_dir.exists():
        missing.append(str(tmp_dir))

    if missing:
        print(
            f"Error: Missing required directories: {', '.join(missing)}",
            file=sys.stderr,
        )
        print("Please create the directories before running:", file=sys.stderr)
        print(f"  mkdir -p ./share/tmp", file=sys.stderr)
        sys.exit(1)

    return share_dir, tmp_dir


def check_onerun_script(tmp_dir):
    """Ensure exactly one shell script exists in the shared tmp directory."""
    scripts = sorted(path for path in tmp_dir.glob("*.sh") if path.is_file())

    if len(scripts) == 1:
        return scripts[0]

    if len(scripts) == 0:
        detail = "none"
    else:
        detail = ", ".join(path.name for path in scripts)

    print(
        "Error: Expected exactly one *.sh in ./share/tmp for one-run boot, "
        f"found {len(scripts)} ({detail})",
        file=sys.stderr,
    )
    sys.exit(1)


def _append_newc_entry(archive, name, mode, mtime, data=b"", inode=1, nlink=1):
    """Append one cpio newc entry to an in-memory archive."""
    if "/" in name:
        name = name.lstrip("/")

    payload = bytes(data)
    name_bytes = name.encode("utf-8") + b"\0"
    fields = (
        inode,
        mode,
        0,  # uid
        0,  # gid
        nlink,
        mtime,
        len(payload),
        0,  # devmajor
        0,  # devminor
        0,  # rdevmajor
        0,  # rdevminor
        len(name_bytes),
        0,  # check
    )
    header = "070701" + "".join(f"{value:08x}" for value in fields)

    archive.extend(header.encode("ascii"))
    archive.extend(name_bytes)
    archive.extend(b"\0" * ((4 - (len(archive) % 4)) % 4))
    archive.extend(payload)
    archive.extend(b"\0" * ((4 - (len(archive) % 4)) % 4))


def create_busybox_initramfs(busybox_path, output_dir):
    """Create a gzip-compressed cpio initramfs with BusyBox and helper scripts."""
    busybox_path = Path(busybox_path)
    if not busybox_path.exists() or not busybox_path.is_file():
        print(
            f"Error: BusyBox binary not found: {busybox_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        busybox_data = busybox_path.read_bytes()
    except OSError as exc:
        print(f"Error reading BusyBox binary: {exc}", file=sys.stderr)
        sys.exit(1)

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    initramfs_path = output_dir / "busybox-initramfs.cpio.gz"

    now = int(time.time())
    archive = bytearray()
    inode = 1
    install_mount_data = create_install_mount_script().encode("utf-8")
    runner_data = create_onerun_runner_script().encode("utf-8")
    service_data = create_onerun_systemd_service().encode("utf-8")

    _append_newc_entry(
        archive,
        name="bin",
        mode=stat.S_IFDIR | 0o755,
        mtime=now,
        inode=inode,
        nlink=2,
    )
    inode += 1

    _append_newc_entry(
        archive,
        name="bin/busybox",
        mode=stat.S_IFREG | 0o755,
        mtime=now,
        data=busybox_data,
        inode=inode,
        nlink=1,
    )
    inode += 1

    _append_newc_entry(
        archive,
        name="bin/sh",
        mode=stat.S_IFLNK | 0o777,
        mtime=now,
        data=b"busybox",
        inode=inode,
        nlink=1,
    )
    inode += 1

    _append_newc_entry(
        archive,
        name="install-mount",
        mode=stat.S_IFREG | 0o755,
        mtime=now,
        data=install_mount_data,
        inode=inode,
        nlink=1,
    )
    inode += 1

    _append_newc_entry(
        archive,
        name="onerun-runner",
        mode=stat.S_IFREG | 0o755,
        mtime=now,
        data=runner_data,
        inode=inode,
        nlink=1,
    )
    inode += 1

    _append_newc_entry(
        archive,
        name="onerun.service",
        mode=stat.S_IFREG | 0o644,
        mtime=now,
        data=service_data,
        inode=inode,
        nlink=1,
    )
    inode += 1

    _append_newc_entry(
        archive,
        name="TRAILER!!!",
        mode=0,
        mtime=now,
        inode=inode,
        nlink=1,
    )

    try:
        with gzip.open(initramfs_path, "wb") as f:
            f.write(archive)
    except OSError as exc:
        print(f"Error writing initramfs: {exc}", file=sys.stderr)
        sys.exit(1)

    return initramfs_path


def resolve_busybox_binary_path(configured_path=None):
    """Resolve BusyBox binary path from explicit value or common defaults."""
    if configured_path is not None:
        busybox_path = Path(configured_path)
        if busybox_path.exists() and busybox_path.is_file():
            return busybox_path

        print(
            f"Error: BusyBox binary not found: {busybox_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    candidates = [
        Path("./busybox-arm71"),
        Path("./busybox-armv7l"),
        Path("./busybox"),
    ]
    for candidate in candidates:
        if candidate.exists() and candidate.is_file():
            return candidate

    print(
        "Error: BusyBox binary not found. Checked ./busybox-arm71, "
        "./busybox-armv7l, and ./busybox.",
        file=sys.stderr,
    )
    sys.exit(1)


def create_install_mount_script():
    """Create helper script used inside BusyBox initramfs to mount and install one-run service."""
    return """#!/bin/sh
set -eu

for cmd in ln cat mknod mkdir mount ls chmod sync; do
    /bin/busybox ln -sf /bin/busybox "/bin/$cmd"
done

/bin/mkdir -p /dev/sysfs
/bin/mount -t sysfs -o nosuid,nodev,noexec sysfs /dev/sysfs

/bin/mkdir -p /proc
/bin/mount -t proc -o nosuid,nodev,noexec proc /proc

i=0
while [ "$i" -lt 50 ] && [ ! -r /dev/sysfs/class/block/vda2/dev ]; do
    /bin/busybox sleep 0.1
    i=$((i + 1))
done

if [ ! -r /dev/sysfs/class/block/vda2/dev ]; then
    /bin/busybox echo "install-mount: missing /dev/sysfs/class/block/vda2/dev" >&2
    exit 1
fi

majmin="$(/bin/cat /dev/sysfs/class/block/vda2/dev)"
major="${majmin%:*}"
minor="${majmin#*:}"

if [ ! -b /dev/vda2 ]; then
    /bin/mknod /dev/vda2 b "$major" "$minor"
fi

/bin/mkdir -p /newroot
/bin/mount -t ext4 -o rw /dev/vda2 /newroot
/bin/busybox echo "install-mount: mounted /dev/vda2 at /newroot"

if [ -f /onerun-runner ] && [ -f /onerun.service ]; then
    if [ -d /newroot/etc/systemd/system ]; then
        /bin/mkdir -p /newroot/usr/local/bin
        /bin/mkdir -p /newroot/etc/systemd/system/multi-user.target.wants

        /bin/cat /onerun-runner > /newroot/usr/local/bin/onerun-runner
        /bin/cat /onerun.service > /newroot/etc/systemd/system/onerun.service

        /bin/chmod 755 /newroot/usr/local/bin/onerun-runner
        /bin/ln -sf /etc/systemd/system/onerun.service /newroot/etc/systemd/system/multi-user.target.wants/onerun.service
        /bin/busybox echo "install-mount: installed onerun service in /newroot"
    else
        /bin/busybox echo "install-mount: /newroot/etc/systemd/system missing; skipping service install" >&2
    fi
else
    /bin/busybox echo "install-mount: service payload files missing on ramdisk; skipping service install" >&2
fi

if [ -w /proc/sysrq-trigger ]; then
    /bin/busybox echo s > /proc/sysrq-trigger
    /bin/busybox sleep 0.2
    /bin/busybox echo o > /proc/sysrq-trigger
else
    /bin/sync
fi

/bin/busybox poweroff -f || true
/bin/busybox echo "install-mount: poweroff did not trigger; entering BusyBox shell"
exec /bin/sh
"""


def check_required_files(disk_path):
    """Check that required QEMU files exist."""
    files = {}
    missing = []

    # Check kernel (always Image.gz in current directory)
    kernel_path = Path("Image.gz")
    if kernel_path.exists():
        files["kernel"] = kernel_path.absolute()
    else:
        missing.append("Image.gz")

    # Check disk
    disk_file = Path(disk_path)
    if disk_file.exists():
        files["disk"] = disk_file.absolute()
    else:
        missing.append(str(disk_path))

    if missing:
        print(f"Error: Missing required files: {', '.join(missing)}", file=sys.stderr)
        sys.exit(1)

    return files


def create_onerun_runner_script():
    """Generate the script invoked by systemd for one-run execution."""
    return """#!/bin/sh
set -eu

set -- /mnt/share/tmp/*.sh
if [ "$1" = "/mnt/share/tmp/*.sh" ]; then
    echo "onerun: expected exactly one script in /mnt/share/tmp, found none" >&2
    shutdown -P now
    exit 1
fi

if [ "$#" -ne 1 ]; then
    echo "onerun: expected exactly one script in /mnt/share/tmp, found $#" >&2
    shutdown -P now
    exit 1
fi

script="$1"
if [ ! -x "$script" ]; then
    chmod +x "$script" || true
fi

status=0
"$script" || status=$?

shutdown -P now
exit "$status"
"""


def create_onerun_systemd_service():
    """Generate the systemd service that runs the one-run script at boot."""
    return """[Unit]
Description=Run shared script once and power off
After=local-fs.target
RequiresMountsFor=/mnt/share/tmp

[Service]
Type=oneshot
ExecStart=/usr/local/bin/onerun-runner
StandardOutput=journal+console
StandardError=journal+console
RemainAfterExit=no

[Install]
WantedBy=multi-user.target
"""


def create_snapshot(disk_path, snapshot_name):
    """Create a QEMU snapshot of the disk image."""
    cmd = ["qemu-img", "snapshot", "-c", snapshot_name, str(disk_path)]

    print(f"Creating snapshot '{snapshot_name}' on {disk_path}...")
    try:
        subprocess.run(cmd, capture_output=True, text=True, check=True)
        print("Snapshot created successfully")
    except subprocess.CalledProcessError as e:
        print(f"Error creating snapshot: {e.stderr}", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError:
        print("Error: qemu-img command not found in PATH", file=sys.stderr)
        sys.exit(1)


def build_qemu_command(
    files,
    share_dir,
    qmp_socket_path,
    debug=False,
    memory="256",
    initrd_path=None,
    rdinit_path=None,
):
    """Build the QEMU command line arguments."""
    cmd = [
        "qemu-system-aarch64",
        "-M",
        "virt",
        "-cpu",
        "cortex-a57",
        "-m",
        memory,
        # Disk configuration
        "-device",
        "virtio-blk-pci,drive=rootdisk,bootindex=1",
        "-drive",
        f"if=none,media=disk,id=rootdisk,file={files['disk']},discard=unmap,detect-zeroes=unmap",
        # Shared filesystem (9p)
        "-fsdev",
        f"local,id=share,path={share_dir.absolute()},security_model=mapped-xattr",
        "-device",
        "virtio-9p-pci,fsdev=share,mount_tag=share",
        # No network
        "-nic",
        "none",
        # QMP control socket
        "-qmp",
        f"unix:{qmp_socket_path},server=on,wait=off",
        # Console
        "-nographic",
        "-no-reboot",
        # Kernel
        "-kernel",
        str(files["kernel"]),
    ]

    if initrd_path:
        cmd.extend(["-initrd", str(initrd_path)])

    # Build kernel command line
    kernel_args = "root=/dev/vda2 HOST=aarch64 console=ttyAMA0"

    if rdinit_path:
        kernel_args += f" rdinit={rdinit_path}"
    elif debug:
        # Debug boot mode - let QEMU boot normally without init override
        pass

    cmd.extend(["-append", kernel_args])

    return cmd


def main():
    parser = argparse.ArgumentParser(
        description="Run a script in QEMU with isolated environment.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run the one-run service (requires exactly one ./share/tmp/*.sh)
  %(prog)s

  # Let QEMU boot normally (debug boot process)
  %(prog)s --debug
  
  # Patch the disk image with one-run systemd files
  %(prog)s --install

  # Boot to BusyBox shell from initramfs
  %(prog)s --busybox

  # Create a snapshot before running (for rollback)
  %(prog)s --snapshot presave
  
  # Use a different disk image
  %(prog)s --disk mydisk.qcow2
  
  # Allocate more memory
  %(prog)s --memory 512

  # Prepare one-run install and print QEMU command (no execution)
  %(prog)s --install --dry-run
  
Note: For normal runs, keep exactly one *.sh script in ./share/tmp/.
The kernel Image.gz must be in the current directory.
Directories ./share and ./share/tmp must exist.
The --install option uses an initramfs helper to patch /dev/vda2 and then powers off.
--install also creates an automatic preinstall snapshot on the disk image first.
On the next normal boot, the guest runs the single /mnt/share/tmp/*.sh script and shuts down.
No external dependencies required.
        """,
    )

    parser.add_argument(
        "--disk",
        default="sandbox.qcow2",
        metavar="FILE",
        help="Path to QEMU disk image (default: sandbox.qcow2)",
    )

    parser.add_argument(
        "--debug",
        action="store_true",
        help="Let QEMU boot normally without init override (for debugging boot process)",
    )

    parser.add_argument(
        "--install",
        action="store_true",
        help="Create a preinstall snapshot, then use BusyBox initramfs installer to mount rootfs, install one-run systemd files, sync, and power off.",
    )

    parser.add_argument(
        "--busybox",
        nargs="?",
        const="auto",
        metavar="FILE",
        help="Boot with initramfs + rdinit shell using BusyBox as /bin/sh (default: auto-detect ./busybox-arm71, ./busybox-armv7l, or ./busybox).",
    )

    parser.add_argument(
        "--memory",
        default="256",
        metavar="MB",
        help="Memory allocation in MB (default: 256)",
    )

    parser.add_argument(
        "--snapshot",
        metavar="NAME",
        help="Create a QEMU disk snapshot with given name before running",
    )

    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the QEMU command without executing it",
    )

    parser.add_argument(
        "--qmp-socket",
        metavar="PATH",
        help="QMP UNIX socket path (default: ./tmp/qmp-<pid>-<time>.sock)",
    )

    args = parser.parse_args()

    if args.install and args.debug:
        parser.error("--install cannot be combined with --debug")

    if args.busybox and args.debug:
        parser.error("--busybox cannot be combined with --debug")

    # Check directories exist
    share_dir, tmp_dir = check_directories()
    print(f"Share directory: {share_dir.absolute()}")
    print(f"Scratch directory: {tmp_dir.absolute()}")

    # Normal one-run mode requires exactly one shared shell script
    if not args.install and not args.debug and not args.busybox:
        script_path = check_onerun_script(tmp_dir)
        print(f"One-run script: {script_path.absolute()}")

    # Check for required files
    print("Checking for required files...")
    files = check_required_files(args.disk)
    print(f"Kernel: {files['kernel']}")
    print(f"Disk: {files['disk']}")

    # For install mode, create a rollback snapshot first
    if args.install:
        install_snapshot_name = f"preinstall-{int(time.time())}-{os.getpid()}"
        create_snapshot(files["disk"], install_snapshot_name)
        print(f"Automatic install snapshot: {install_snapshot_name}")
        print()

    # Handle initramfs-based install and/or BusyBox shell mode
    initrd_path = None
    rdinit_path = None

    if args.install or args.busybox:
        configured_busybox = None if args.busybox in (None, "auto") else args.busybox
        busybox_path = resolve_busybox_binary_path(configured_busybox)
        initrd_path = create_busybox_initramfs(
            busybox_path=busybox_path,
            output_dir=Path("./tmp"),
        )

        print(f"Using BusyBox binary: {busybox_path.absolute()}")
        print(f"Prepared BusyBox initramfs: {initrd_path}")

        if args.install:
            rdinit_path = "/install-mount"
            print(
                "Booting with rdinit=/install-mount to install service, sync, and power off."
            )
        else:
            rdinit_path = "/bin/sh"
            print("Booting with rdinit=/bin/sh from initramfs.")

        print()

    # Create additional snapshot if requested
    if args.snapshot:
        create_snapshot(files["disk"], args.snapshot)
        print()

    # Build QEMU command
    use_debug = args.debug
    qmp_socket_path = (
        Path(args.qmp_socket) if args.qmp_socket else build_default_qmp_socket_path()
    )
    qmp_socket_path.parent.mkdir(parents=True, exist_ok=True)
    if qmp_socket_path.exists():
        qmp_socket_path.unlink()

    print(f"QMP socket: {qmp_socket_path.absolute()}")

    cmd = build_qemu_command(
        files=files,
        share_dir=share_dir,
        qmp_socket_path=qmp_socket_path,
        debug=use_debug,
        memory=args.memory,
        initrd_path=initrd_path,
        rdinit_path=rdinit_path,
    )

    print("\nQEMU command:")
    print(" ".join(str(c) for c in cmd))
    print()

    if args.dry_run:
        print("Dry run - not executing.")
        return 0

    # Run QEMU
    print("Starting QEMU...")
    print("=" * 60)
    try:
        return run_qemu_with_qmp(cmd, qmp_socket_path)
    except Exception as e:
        print(f"\nError running QEMU: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
