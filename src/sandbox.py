#!/usr/bin/env python3
"""
Run scripts in QEMU pedantically.
We start QEMU on each run with no network services.
The ./share directory is exposed to the guest at /mnt/share via 9p.
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
import select
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import termios
import threading
import time
import tty
import urllib.request
from pathlib import Path

# Ctrl-] (ASCII 0x1d) is the standard telnet-style escape used to detach
# from the console and request a graceful guest shutdown.
CONSOLE_ESCAPE_BYTE = 0x1D

# Default upstream for the aarch64 kernel Image.gz. The rootfs tarball ships
# the kernel under boot/Image.gz, which is what `-kernel Image.gz` expects.
DEFAULT_KERNEL_TARBALL_URL = (
    "http://os.archlinuxarm.org/os/ArchLinuxARM-aarch64-latest.tar.gz"
)
DEFAULT_KERNEL_MEMBER_CANDIDATES = ("boot/Image.gz", "./boot/Image.gz", "Image.gz")


class QMPClient:
    """Minimal QMP client over a UNIX socket."""

    def __init__(self, socket_path):
        self.socket_path = str(socket_path)
        self.sock = None
        self._recv_buffer = ""
        self._lock = threading.Lock()

    def is_connected(self):
        return self.sock is not None

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
        with self._lock:
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


def build_default_serial_socket_path():
    serial_dir = Path("./tmp")
    serial_dir.mkdir(parents=True, exist_ok=True)
    return serial_dir / f"serial-{os.getpid()}-{int(time.time())}.sock"


def _request_guest_shutdown(qmp, qemu_proc):
    """Try to shut down the guest gracefully; fall back to terminating QEMU."""
    if qmp is not None and qmp.is_connected():
        try:
            qmp.execute("system_powerdown", timeout=2.0)
            return
        except Exception as exc:
            print(
                f"Warning: QMP system_powerdown failed: {exc}",
                file=sys.stderr,
            )

    if qemu_proc.poll() is None:
        try:
            qemu_proc.terminate()
        except Exception as exc:
            print(f"Warning: terminate failed: {exc}", file=sys.stderr)


def relay_serial_console(
    socket_path,
    qemu_proc,
    qmp=None,
    stop_event=None,
    timeout=10.0,
    retry_interval=0.1,
):
    """Connect to the QEMU serial socket and relay it to the local terminal.

    If stdin is a TTY, Ctrl-] triggers a graceful guest shutdown and detaches
    from the console.
    """
    path_str = str(socket_path)
    deadline = time.monotonic() + timeout
    sock = None
    last_error = None

    while time.monotonic() < deadline and qemu_proc.poll() is None:
        if stop_event is not None and stop_event.is_set():
            return
        candidate = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            candidate.connect(path_str)
            sock = candidate
            break
        except (FileNotFoundError, ConnectionRefusedError, OSError) as exc:
            last_error = exc
            candidate.close()
            time.sleep(retry_interval)

    if sock is None:
        if qemu_proc.poll() is None:
            print(
                f"Warning: Could not connect to serial console socket {path_str}: {last_error}",
                file=sys.stderr,
            )
        return

    stdin_fd = None
    stdin_is_tty = False
    restore_tty = None

    try:
        if sys.stdin.isatty():
            stdin_fd = sys.stdin.fileno()
            stdin_is_tty = True
            restore_tty = termios.tcgetattr(stdin_fd)
            # Raw mode: forward all keys (including Ctrl-C) to the guest.
            # Ctrl-] is intercepted by this relay as the detach/poweroff key.
            tty.setraw(stdin_fd)
        elif not sys.stdin.closed:
            try:
                stdin_fd = sys.stdin.fileno()
            except (ValueError, OSError):
                stdin_fd = None

        banner = f"[serial] Connected to {path_str}"
        if stdin_is_tty:
            banner += " (press Ctrl-] to detach / power off)"
        print(banner + "\r")

        sock_fd = sock.fileno()

        while qemu_proc.poll() is None:
            if stop_event is not None and stop_event.is_set():
                break

            read_fds = [sock_fd]
            if stdin_fd is not None:
                read_fds.append(stdin_fd)

            try:
                ready, _, _ = select.select(read_fds, [], [], 0.2)
            except (OSError, ValueError):
                break

            if sock_fd in ready:
                try:
                    data = sock.recv(4096)
                except (ConnectionResetError, BrokenPipeError, OSError):
                    break
                if not data:
                    break
                try:
                    os.write(sys.stdout.fileno(), data)
                except (BrokenPipeError, OSError):
                    break

            if stdin_fd is not None and stdin_fd in ready:
                try:
                    data = os.read(stdin_fd, 1024)
                except (OSError, ValueError):
                    stdin_fd = None
                    continue

                if not data:
                    stdin_fd = None
                    continue

                if stdin_is_tty and CONSOLE_ESCAPE_BYTE in data:
                    prefix = data.split(bytes([CONSOLE_ESCAPE_BYTE]), 1)[0]
                    if prefix:
                        try:
                            sock.sendall(prefix)
                        except (
                            ConnectionResetError,
                            BrokenPipeError,
                            OSError,
                        ):
                            pass
                    # Restore the tty before printing so the message renders
                    # normally and the user regains line-buffered input.
                    if restore_tty is not None and stdin_fd is not None:
                        termios.tcsetattr(stdin_fd, termios.TCSADRAIN, restore_tty)
                        restore_tty = None
                    print("\r\n[serial] Detach requested, powering off guest...")
                    _request_guest_shutdown(qmp, qemu_proc)
                    break

                try:
                    sock.sendall(data)
                except (ConnectionResetError, BrokenPipeError, OSError):
                    break

    finally:
        if restore_tty is not None and stdin_fd is not None:
            try:
                termios.tcsetattr(stdin_fd, termios.TCSADRAIN, restore_tty)
            except termios.error:
                pass
        try:
            sock.close()
        except OSError:
            pass


def cleanup_socket_file(path):
    socket_path = Path(path)
    if socket_path.exists() or socket_path.is_socket():
        socket_path.unlink()


def run_qemu_with_qmp(cmd, qmp_socket_path, serial_socket_path=None):
    """Start QEMU and control it through QMP where possible."""
    qemu_proc = subprocess.Popen(cmd)
    qmp = QMPClient(qmp_socket_path)
    qmp_connected = False
    relay_thread = None
    stop_event = threading.Event()

    try:
        # Start the serial relay early so early boot output is visible even
        # while QMP capability negotiation is still in progress.
        if serial_socket_path is not None:
            relay_thread = threading.Thread(
                target=relay_serial_console,
                name="serial-relay",
                args=(serial_socket_path, qemu_proc),
                kwargs={"qmp": qmp, "stop_event": stop_event},
                daemon=True,
            )
            relay_thread.start()

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
        stop_event.set()
        if relay_thread is not None:
            relay_thread.join(timeout=2.0)
        qmp.close()
        cleanup_socket_file(qmp_socket_path)
        if serial_socket_path is not None:
            cleanup_socket_file(serial_socket_path)


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
        print("  mkdir -p ./share/tmp", file=sys.stderr)
        sys.exit(1)

    return share_dir, tmp_dir


def check_onerun_script(tmp_dir):
    """Ensure exactly one shell script exists in the ./share/tmp directory."""
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


def _format_bytes(n):
    if n is None:
        return "?"
    units = ("B", "KiB", "MiB", "GiB")
    value = float(n)
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} GiB"


def _stream_download(url, dest_path, chunk_size=64 * 1024):
    """Stream a URL to dest_path with a simple progress indicator."""
    request = urllib.request.Request(
        url, headers={"User-Agent": "mlx-lfm-demo-sandbox/1.0"}
    )
    with urllib.request.urlopen(request) as response:
        total_header = response.headers.get("Content-Length")
        total = int(total_header) if total_header else None

        downloaded = 0
        last_report = 0.0
        with open(dest_path, "wb") as out:
            while True:
                chunk = response.read(chunk_size)
                if not chunk:
                    break
                out.write(chunk)
                downloaded += len(chunk)

                now = time.monotonic()
                if now - last_report > 0.5:
                    if total:
                        pct = downloaded / total * 100
                        sys.stdout.write(
                            f"\r  {_format_bytes(downloaded)} / "
                            f"{_format_bytes(total)} ({pct:.1f}%)"
                        )
                    else:
                        sys.stdout.write(f"\r  {_format_bytes(downloaded)}")
                    sys.stdout.flush()
                    last_report = now

        # Final newline after progress line.
        if last_report > 0.0:
            sys.stdout.write("\n")
            sys.stdout.flush()


def _find_kernel_member(tar, candidates):
    """Locate the Image.gz member inside an open tarfile."""
    for candidate in candidates:
        try:
            member = tar.getmember(candidate)
            if member.isfile():
                return member
        except KeyError:
            continue

    # Fallback: any file whose basename is Image.gz.
    for member in tar.getmembers():
        if member.isfile() and Path(member.name).name == "Image.gz":
            return member

    return None


def download_kernel_from_tarball(
    url,
    dest_path,
    tmp_dir,
    member_candidates=DEFAULT_KERNEL_MEMBER_CANDIDATES,
):
    """Download a compressed tarball and extract Image.gz into dest_path.

    Never overwrites an existing dest_path file.
    """
    dest_path = Path(dest_path)
    if dest_path.exists():
        print(
            f"Kernel already present at {dest_path.absolute()}; refusing to overwrite."
        )
        return dest_path

    tmp_dir = Path(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tarball_path = tmp_dir / f"kernel-{os.getpid()}-{int(time.time())}.tar.gz"

    print(f"Downloading kernel tarball: {url}")
    print(f"  -> {tarball_path}")
    try:
        _stream_download(url, tarball_path)
    except Exception as exc:
        if tarball_path.exists():
            try:
                tarball_path.unlink()
            except OSError:
                pass
        print(f"Error: failed to download {url}: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Extracting kernel from {tarball_path.name}...")
    temp_dest = dest_path.with_suffix(dest_path.suffix + ".partial")
    try:
        with tarfile.open(tarball_path, "r:*") as tar:
            member = _find_kernel_member(tar, member_candidates)
            if member is None:
                print(
                    "Error: could not locate Image.gz in tarball "
                    f"(tried {list(member_candidates)})",
                    file=sys.stderr,
                )
                sys.exit(1)

            print(f"  member: {member.name} ({_format_bytes(member.size)})")

            extracted = tar.extractfile(member)
            if extracted is None:
                print(
                    f"Error: tar member {member.name} is not a regular file",
                    file=sys.stderr,
                )
                sys.exit(1)

            with extracted as src, open(temp_dest, "wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)

        # Final clobber guard: re-check destination just before renaming so a
        # concurrent writer cannot be silently replaced.
        if dest_path.exists():
            print(
                f"Kernel appeared at {dest_path.absolute()} during extraction; "
                "leaving existing file in place.",
            )
            temp_dest.unlink()
            return dest_path

        os.replace(temp_dest, dest_path)
    except Exception as exc:
        if temp_dest.exists():
            try:
                temp_dest.unlink()
            except OSError:
                pass
        print(f"Error extracting kernel: {exc}", file=sys.stderr)
        sys.exit(1)
    finally:
        if tarball_path.exists():
            try:
                tarball_path.unlink()
            except OSError:
                pass

    print(f"Installed kernel: {dest_path.absolute()}")
    return dest_path


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
    """Generate the script invoked by systemd for one-run execution.

    When the kernel command line contains ONERUN_DEBUG=1, the runner skips the
    /mnt/share/tmp/*.sh selection and drops into an interactive /bin/sh on
    /dev/ttyAMA0 (the serial console) instead of powering the guest off. This
    is intended as a debug entry point the host enables via:

        ONERUN_DEBUG=1 python -m src.sandbox

    The host side appends ONERUN_DEBUG=1 to the kernel args in
    build_qemu_command.
    """
    return """#!/bin/sh
set -eu

# Debug entry point: drop to an interactive shell on the serial console.
if grep -q -w ONERUN_DEBUG=1 /proc/cmdline 2>/dev/null; then
    echo "onerun: ONERUN_DEBUG=1 detected -- starting /bin/sh on /dev/ttyAMA0" >&2
    # Become session leader with /dev/ttyAMA0 as controlling tty so job
    # control (Ctrl-C, fg/bg) works inside the guest shell.
    if command -v setsid >/dev/null 2>&1; then
        exec setsid -c /bin/sh -i </dev/ttyAMA0 >/dev/ttyAMA0 2>&1
    fi
    exec /bin/sh -i </dev/ttyAMA0 >/dev/ttyAMA0 2>&1
fi

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
    """Generate the systemd service that runs the one-run script at boot.

    The unit binds stdio to /dev/ttyAMA0 so the debug-mode interactive shell
    (ONERUN_DEBUG=1 on the kernel cmdline) can read from and write to the
    serial console. In normal one-run mode this is harmless: the script's
    output is already destined for the console.
    """
    return """[Unit]
Description=Run /mnt/share/tmp script once and power off
After=local-fs.target
RequiresMountsFor=/mnt/share/tmp

[Service]
Type=oneshot
ExecStart=/usr/local/bin/onerun-runner
TTYPath=/dev/ttyAMA0
TTYReset=yes
TTYVHangup=yes
StandardInput=tty-force
StandardOutput=tty
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
    serial_socket_path=None,
    nographic=False,
    debug=False,
    memory="256",
    initrd_path=None,
    rdinit_path=None,
    onerun_debug=False,
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
        "-no-reboot",
        # Kernel
        "-kernel",
        str(files["kernel"]),
    ]

    if nographic:
        cmd.append("-nographic")
    else:
        if serial_socket_path is None:
            raise ValueError(
                "serial_socket_path is required when nographic is disabled"
            )
        cmd.extend(
            [
                "-chardev",
                f"socket,id=console0,path={serial_socket_path},server=on,wait=off",
                "-serial",
                "chardev:console0",
                "-monitor",
                "none",
                "-display",
                "none",
            ]
        )

    if initrd_path:
        cmd.extend(["-initrd", str(initrd_path)])

    # Build kernel command line
    kernel_args = "root=/dev/vda2 HOST=aarch64 console=ttyAMA0"
    if rdinit_path:
        kernel_args += f" rdinit={rdinit_path}"
    # debug mode simply omits rdinit and lets the guest boot its own init.
    if onerun_debug:
        # Consumed by /usr/local/bin/onerun-runner inside the guest to drop
        # into an interactive /bin/sh on /dev/ttyAMA0 instead of running a
        # one-run script.
        kernel_args += " ONERUN_DEBUG=1"

    cmd.extend(["-append", kernel_args])

    return cmd


# ---------------------------------------------------------------------------
# Programmatic entry point: run a single script inside the QEMU sandbox and
# return its captured stdout/stderr/exit_code. Designed for the `linux` tool
# when the chat user has enabled sandbox mode via /sandbox. The sandbox disk
# image must already have the one-run systemd unit installed (via --install).
# ---------------------------------------------------------------------------

# Name of the aarch64 QEMU binary the sandbox drives. Kept as a module
# constant so the preflight check, the run path, and any future callers
# agree on what to look for.
SANDBOX_QEMU_BINARY = "qemu-system-aarch64"


def check_sandbox_preflight(
    disk_path="sandbox.qcow2",
    kernel_path="Image.gz",
    qemu_binary=SANDBOX_QEMU_BINARY,
    version_timeout=5.0,
):
    """Verify the environment has what the QEMU sandbox needs.

    Checks performed:
      * ``qemu-system-aarch64`` is on ``PATH``.
      * The binary actually runs with ``--version`` (catches broken
        installs, dylib problems, etc.) within ``version_timeout`` seconds.
      * ``./Image.gz`` (kernel) exists in the current working directory.
      * ``./sandbox.qcow2`` (disk image) exists in the current working
        directory.

    Returns a dict:

        {
            "ok": bool,
            "errors": [str, ...],
            "qemu_path": "/path/to/qemu-system-aarch64" or None,
            "qemu_version": "QEMU emulator version ..." or None,
        }

    This function never raises for missing files / missing binary / failed
    version probes; callers can decide whether to warn, downgrade, or exit.
    """
    errors = []

    qemu_path = shutil.which(qemu_binary)
    qemu_version = None
    if qemu_path is None:
        errors.append(
            f"{qemu_binary} not found on PATH (install QEMU or add it to PATH)"
        )
    else:
        try:
            proc = subprocess.run(
                [qemu_path, "--version"],
                capture_output=True,
                text=True,
                timeout=version_timeout,
            )
            if proc.returncode != 0:
                errors.append(
                    f"{qemu_binary} --version exited with {proc.returncode}: "
                    f"{(proc.stderr or proc.stdout).strip() or 'no output'}"
                )
            else:
                # Keep just the first line of the version banner for display.
                qemu_version = (proc.stdout or proc.stderr).splitlines()[0].strip()
        except subprocess.TimeoutExpired:
            errors.append(f"{qemu_binary} --version timed out after {version_timeout}s")
        except OSError as exc:
            errors.append(f"could not execute {qemu_path}: {exc}")

    kernel_file = Path(kernel_path)
    if not kernel_file.is_file():
        errors.append(
            f"kernel not found: {kernel_file} "
            "(copy the aarch64 Image.gz into the repo root, or run "
            "`python src/sandbox.py --download-kernel`)"
        )

    disk_file = Path(disk_path)
    if not disk_file.is_file():
        errors.append(
            f"disk image not found: {disk_file} "
            "(copy your prepared sandbox.qcow2 into the repo root)"
        )

    return {
        "ok": not errors,
        "errors": errors,
        "qemu_path": qemu_path,
        "qemu_version": qemu_version,
    }


# Backup suffix used to temporarily hide other *.sh files under ./share/tmp
# during a sandboxed run so the guest's one-run runner sees exactly the
# wrapper script we stage for it.
_SANDBOX_BAK_SUFFIX = ".sandboxbak"

_SANDBOX_WRAPPER_NAME = "_linux_tool_wrapper.sh"
_SANDBOX_INNER_NAME = "_linux_tool_inner"
_SANDBOX_STDOUT_NAME = ".linux_stdout"
_SANDBOX_STDERR_NAME = ".linux_stderr"
_SANDBOX_EXIT_NAME = ".linux_exit"


def _stage_other_scripts(tmp_dir, keep_names):
    """Rename every *.sh in tmp_dir (except ``keep_names``) with a backup
    suffix so the one-run runner sees exactly the wrapper script.

    Returns the list of (original_path, backup_path) pairs so the caller can
    restore them once the sandboxed run is done.
    """
    staged = []
    for path in tmp_dir.glob("*.sh"):
        if not path.is_file():
            continue
        if path.name in keep_names:
            continue
        backup = path.with_name(path.name + _SANDBOX_BAK_SUFFIX)
        # If a stale backup exists from a previous aborted run, remove it
        # so the rename does not collide.
        if backup.exists():
            try:
                backup.unlink()
            except OSError:
                continue
        try:
            path.rename(backup)
            staged.append((path, backup))
        except OSError:
            continue
    return staged


def _restore_staged_scripts(staged):
    for original, backup in staged:
        if backup.exists() and not original.exists():
            try:
                backup.rename(original)
            except OSError:
                pass


def run_script_in_sandbox(
    script_abs_path,
    disk_path="sandbox.qcow2",
    memory="256",
    timeout=120,
):
    """Run ``script_abs_path`` inside the QEMU sandbox and return a dict with
    ``stdout``, ``stderr``, and ``exit_code`` keys.

    The caller passes an absolute path to a script file that already lives
    somewhere on disk (typically under ``./share/tmp`` because that is where
    the ``linux`` tool writes its scripts). The sandbox itself always boots
    with ``./share`` mounted into the guest at ``/mnt/share`` via 9p, so we
    stage the run by:

      1. Copying the script's contents to ``./share/tmp/_linux_tool_inner``.
      2. Writing a ``./share/tmp/_linux_tool_wrapper.sh`` wrapper that
         executes the inner script with stdout/stderr/exit captured to
         known files under ``./share/tmp``.
      3. Renaming any other ``*.sh`` files in ``./share/tmp`` to
         ``*.sh.sandboxbak`` so the guest's one-run runner picks exactly our
         wrapper.
      4. Booting QEMU headless; the guest runs the wrapper and powers off.
      5. Reading the captured output files from the host side.
      6. Cleaning up the wrapper/inner/output files and restoring any
         renamed scripts.

    Any exception during setup or QEMU execution is converted into a dict
    result with a descriptive ``stderr`` and ``exit_code`` of ``-1``.
    """
    share_dir = Path("./share")
    tmp_dir = share_dir / "tmp"
    if not tmp_dir.exists():
        return {
            "stdout": "",
            "stderr": "sandbox: ./share/tmp does not exist",
            "exit_code": -1,
        }

    script_path = Path(script_abs_path)
    if not script_path.is_file():
        return {
            "stdout": "",
            "stderr": f"sandbox: script not found: {script_abs_path}",
            "exit_code": -1,
        }

    disk_file = Path(disk_path)
    kernel_file = Path("Image.gz")
    if not disk_file.exists():
        return {
            "stdout": "",
            "stderr": f"sandbox: disk image not found: {disk_path}",
            "exit_code": -1,
        }
    if not kernel_file.exists():
        return {
            "stdout": "",
            "stderr": "sandbox: kernel Image.gz not found in cwd",
            "exit_code": -1,
        }

    wrapper_path = tmp_dir / _SANDBOX_WRAPPER_NAME
    inner_path = tmp_dir / _SANDBOX_INNER_NAME
    stdout_path = tmp_dir / _SANDBOX_STDOUT_NAME
    stderr_path = tmp_dir / _SANDBOX_STDERR_NAME
    exit_path = tmp_dir / _SANDBOX_EXIT_NAME

    # Clear any stale capture files from a previous aborted run.
    for p in (stdout_path, stderr_path, exit_path, wrapper_path, inner_path):
        if p.exists():
            try:
                p.unlink()
            except OSError:
                pass

    staged = []
    qmp_socket = None
    serial_socket = None
    try:
        shutil.copyfile(script_path, inner_path)
        inner_path.chmod(0o755)

        wrapper_script = (
            "#!/bin/sh\n"
            "cd /mnt/share/tmp\n"
            "./" + _SANDBOX_INNER_NAME + " "
            "> /mnt/share/tmp/" + _SANDBOX_STDOUT_NAME + " "
            "2> /mnt/share/tmp/" + _SANDBOX_STDERR_NAME + "\n"
            "rc=$?\n"
            'echo "$rc" > /mnt/share/tmp/' + _SANDBOX_EXIT_NAME + "\n"
            "sync\n"
        )
        wrapper_path.write_text(wrapper_script)
        wrapper_path.chmod(0o755)

        # Hide every other .sh (including any backup file we made earlier)
        # so the one-run runner sees exactly our wrapper.
        staged = _stage_other_scripts(tmp_dir, keep_names={_SANDBOX_WRAPPER_NAME})

        files = {
            "kernel": kernel_file.absolute(),
            "disk": disk_file.absolute(),
        }
        qmp_socket = build_default_qmp_socket_path()
        serial_socket = build_default_serial_socket_path()
        for p in (qmp_socket, serial_socket):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass

        cmd = build_qemu_command(
            files=files,
            share_dir=share_dir,
            qmp_socket_path=qmp_socket,
            serial_socket_path=None,
            nographic=True,
            debug=False,
            memory=str(memory),
            initrd_path=None,
            rdinit_path=None,
            onerun_debug=False,
        )

        qemu_proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            qemu_proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if qemu_proc.poll() is None:
                qemu_proc.terminate()
                try:
                    qemu_proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    qemu_proc.kill()
                    qemu_proc.wait()
            return {
                "stdout": (
                    stdout_path.read_text(errors="replace")
                    if stdout_path.exists()
                    else ""
                ),
                "stderr": f"sandbox: guest timed out after {timeout}s",
                "exit_code": -1,
            }

        stdout = stdout_path.read_text(errors="replace") if stdout_path.exists() else ""
        stderr = stderr_path.read_text(errors="replace") if stderr_path.exists() else ""
        if exit_path.exists():
            try:
                exit_code = int(exit_path.read_text().strip())
            except (ValueError, OSError):
                exit_code = -1
        else:
            # Guest shut down without writing the exit file -- report the
            # QEMU process's return code so the caller still sees something
            # useful.
            exit_code = qemu_proc.returncode if qemu_proc.returncode is not None else -1
            if not stderr:
                stderr = "sandbox: guest did not record an exit code"

        return {"stdout": stdout, "stderr": stderr, "exit_code": exit_code}

    except Exception as exc:
        return {
            "stdout": "",
            "stderr": f"sandbox: execution failed: {exc}",
            "exit_code": -1,
        }
    finally:
        for p in (wrapper_path, inner_path, stdout_path, stderr_path, exit_path):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
        _restore_staged_scripts(staged)
        if qmp_socket is not None:
            cleanup_socket_file(qmp_socket)
        if serial_socket is not None:
            cleanup_socket_file(serial_socket)


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

  # Use classic QEMU -nographic mode (stdio serial, no socket relay)
  %(prog)s --nographic

  # Boot the one-run service into an interactive /bin/sh on the serial
  # console (no one-run script is executed, guest does not auto-power-off).
  # Equivalent to: ONERUN_DEBUG=1 %(prog)s
  %(prog)s --shell

  # Use a custom serial UNIX socket path
  %(prog)s --serial-socket /tmp/qemu-serial.sock

  # Download Image.gz from the default Arch Linux ARM aarch64 tarball
  %(prog)s --download-kernel --dry-run

  # Download Image.gz from a custom tarball URL
  %(prog)s --download-kernel --kernel-url https://example.com/kernel.tar.gz

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

    parser.add_argument(
        "--serial-socket",
        metavar="PATH",
        help="Serial UNIX socket path used for ttyAMA0 console (default: ./tmp/serial-<pid>-<time>.sock)",
    )

    parser.add_argument(
        "--nographic",
        action="store_true",
        help="Use QEMU -nographic mode instead of serial UNIX socket relay",
    )

    parser.add_argument(
        "--shell",
        action="store_true",
        help=(
            "Boot the one-run service into an interactive /bin/sh on the "
            "serial console instead of running a script under ./share/tmp. "
            "Requires a prior --install. Equivalent to ONERUN_DEBUG=1."
        ),
    )

    parser.add_argument(
        "--download-kernel",
        action="store_true",
        help="Download the aarch64 kernel Image.gz from a compressed tar archive if not already present. Never overwrites an existing ./Image.gz.",
    )

    parser.add_argument(
        "--kernel-url",
        default=DEFAULT_KERNEL_TARBALL_URL,
        metavar="URL",
        help=f"URL of the compressed tarball containing boot/Image.gz (default: {DEFAULT_KERNEL_TARBALL_URL})",
    )

    args = parser.parse_args()

    if args.install and args.debug:
        parser.error("--install cannot be combined with --debug")

    if args.busybox and args.debug:
        parser.error("--busybox cannot be combined with --debug")

    if args.shell and (args.install or args.debug or args.busybox):
        parser.error("--shell cannot be combined with --install, --debug, or --busybox")

    # Optionally fetch the kernel before any other checks so that a fresh
    # workspace can be bootstrapped with a single command.
    if args.download_kernel:
        download_kernel_from_tarball(
            url=args.kernel_url,
            dest_path=Path("./Image.gz"),
            tmp_dir=Path("./tmp"),
        )
        print()

    # Check directories exist
    share_dir, tmp_dir = check_directories()
    print(f"Share directory: {share_dir.absolute()}")
    print(f"Scratch directory: {tmp_dir.absolute()}")

    # --shell (or ONERUN_DEBUG=1 in the host environment) is propagated onto
    # the kernel cmdline and tells the in-guest onerun-runner to drop into an
    # interactive /bin/sh on /dev/ttyAMA0 instead of running a one-run script.
    onerun_debug = args.shell or os.environ.get("ONERUN_DEBUG") == "1"
    if onerun_debug:
        source = "--shell" if args.shell else "ONERUN_DEBUG=1"
        print(f"{source}: guest will drop to /bin/sh on serial console.")

    # Normal one-run mode requires exactly one shell script in ./share/tmp. Debug
    # mode skips the check because no script will be executed.
    if not args.install and not args.debug and not args.busybox and not onerun_debug:
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
    serial_socket_path = None
    if not args.nographic:
        serial_socket_path = (
            Path(args.serial_socket)
            if args.serial_socket
            else build_default_serial_socket_path()
        )

    qmp_socket_path.parent.mkdir(parents=True, exist_ok=True)
    if serial_socket_path is not None:
        serial_socket_path.parent.mkdir(parents=True, exist_ok=True)

    if qmp_socket_path.exists():
        qmp_socket_path.unlink()
    if serial_socket_path is not None and serial_socket_path.exists():
        serial_socket_path.unlink()

    print(f"QMP socket: {qmp_socket_path.absolute()}")
    if serial_socket_path is not None:
        print(f"Serial socket: {serial_socket_path.absolute()}")
    else:
        print("Console mode: -nographic")

    cmd = build_qemu_command(
        files=files,
        share_dir=share_dir,
        qmp_socket_path=qmp_socket_path,
        serial_socket_path=serial_socket_path,
        nographic=args.nographic,
        debug=use_debug,
        memory=args.memory,
        initrd_path=initrd_path,
        rdinit_path=rdinit_path,
        onerun_debug=onerun_debug,
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
        return run_qemu_with_qmp(cmd, qmp_socket_path, serial_socket_path)
    except Exception as e:
        print(f"\nError running QEMU: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
