# mlx-lfm-demo
LiquidAI LFM in MLX

Run the LiquidAI LFM model giving it some linux super powers in QEMU.

# Unsafe tool calls
THIS PROGRAM MAKES UNSAFE AI TOOL CALLS

This program can make unsafe tool calls.
You probably want to run it in a container.
Included in the project is a QEMU based container in "sandbox.py".
But this is not a secure container.
The AI can still ruin your machine or worse.

# TODO
Rework the chat proper cli program for the last refactoring.
Add more step wise tests in pytest.

# Tools
The tools are: `read_file`, `write_file`, `list_files`, `wget`, `clean_tmp` and
`linux`.

All tool I/O is confined to `./share/`.  Scripts and downloads live under
`./share/tmp/`, and subdirectories anywhere under `./share/` are allowed.

Every path passed to a tool MUST start with `./share/` — for example
`./share/a`, `./share/tmp/`, `./share/tmp/run_date.sh`.  Bare names, `tmp/...`,
`share/...` (without `./`), absolute paths, and `..` traversal are all
rejected.  `list_files` returns entries prefixed with `./share/...`;
directories end with a trailing `/` so the model can tell files from
directories at a glance.

Tool-call string values (for example the `content` argument of `write_file`)
are parsed as C-style double-quoted strings. The parser decodes the usual
backslash escapes — `\n`, `\t`, `\r`, `\0`, `\\`, `\"`, `\'` — into real
characters. Unknown escapes (e.g. `\x`) are preserved verbatim. This is
documented for the model in `LFMAGENT.md`.

The `linux` tool runs a script located under `./share/`. It can execute
either on the host or inside the QEMU sandbox in `src/sandbox.py`; see
[Sandbox mode](#sandbox-mode) below.

The `clean_tmp` tool removes files from `./share/tmp/` — a single file via
`file_name`, or every file under `./share/tmp/` (recursively) when called with
no arguments.

# Chat interactions
The chat reads stdin for multiline inputs.
Send the messages to the model with the go command.
```
/go        process the accumulated messages (two blank lines works too)
/clear     wipe the conversation history
/context   dump the conversation as JSON
/sandbox   toggle QEMU sandbox mode for the linux tool
/quit      exit
```

`/sandbox` with no argument toggles the flag. You can also be explicit:
`/sandbox on`, `/sandbox off`, `/sandbox toggle`, or `/sandbox status`.

# Sandbox mode
The `linux` tool can run its generated shell scripts two different ways:

- **Sandbox ON** (default for `mlx-lfm-demo`): each script is staged into
  `./share/tmp/` and executed inside the QEMU guest built by
  `src/sandbox.py`. Output is captured back on the host and returned to
  the model. This is slower (QEMU boot) but isolates the `linux` tool
  from your machine.
- **Sandbox OFF**: each script runs directly on the host via
  `subprocess.run`. This is fast, which is why unit tests and the
  library default (`tools.USE_SANDBOX = False`) keep sandbox OFF. It is
  also what lets the AI ruin your machine, so use it deliberately.

How to control it:

- When you launch the CLI with `mlx-lfm-demo`, sandbox mode starts **ON**.
  Use `mlx-lfm-demo --no-sandbox` to launch with it OFF, or
  `mlx-lfm-demo --sandbox` to be explicit.
- Inside a session, type `/sandbox` (or `/sandbox on|off`) to flip at
  runtime.
- Programmatic callers of `mlx_lfm_demo.tools` can use
  `tools.set_sandbox_enabled(True|False)` and `tools.is_sandbox_enabled()`.

Sandbox mode requires a working `qemu-system-aarch64`, `./Image.gz`, and
`./sandbox.qcow2` prepared as described in
[QEMU kernel and root disk](#qemu-kernel-and-root-disk) below, plus a
prior one-run `--install` (see
[BusyBox for sandbox.py](#busybox-for-sandboxpy)).

# LFMAGENT.md
Setup the chat context with this file.
This helps tell the model about the tools.
Customize this file to make the model achieve a specific goal.

# QEMU kernel and root disk
I am using the Archlinux Linux from UTM. https://mac.getutm.app

You will need to copy the qcow2 image into the git checkout as sandbox.qcow2.
The easy way to find it.  Run ArchLinux in UTM; use the ps command to see qemu.

You will need the Image.Z kernel from the Arch linux.
The easiest way to get that is to cp /boot/Image.gz /mnt/share/Image.gz in the running ArchLinux.

# BusyBox for sandbox.py
You are going to need busybox to install the systemd service for the linux tool support.

For `src/sandbox.py --busybox`, download an ARMv7 BusyBox binary and place it at the repo root as `busybox-armv7l`:

https://busybox.net/downloads/binaries/1.21.1/busybox-armv7l

# Debugging the one-run service

Pass `--shell` (or set `ONERUN_DEBUG=1` on the host) to drop the guest into
an interactive `/bin/sh` on the serial console instead of running
`./share/tmp/*.sh`:

```
python src/sandbox.py --shell
# equivalent:
ONERUN_DEBUG=1 python src/sandbox.py
```

This appends `ONERUN_DEBUG=1` to the kernel command line.  The in-guest
`onerun-runner` detects that flag (via `/proc/cmdline`), skips the one-run
script selection, and execs `setsid -c /bin/sh -i </dev/ttyAMA0 >/dev/ttyAMA0`
so you get a job-controllable shell on the serial console.  No script is
required under `./share/tmp/` in this mode, and the guest does *not*
auto-power-off — `shutdown -P now` from the shell (or QMP) to stop it.

If you have already run `--install` before adding this feature, re-run
`python src/sandbox.py --install` once to refresh the updated
`onerun-runner` and `onerun.service` files on the disk image.
