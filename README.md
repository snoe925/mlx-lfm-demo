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
Use the sandbox.py with the linux tool runner.

# Tools
The tools are:
read file, write file, list files and linux.
The linux tool can run a script in QEMU.
This script can do anything in the VM (and probably on your machine).

# Chat interactions
The chat reads stdin for multiline inputs.
Send the messages to the model with the go command.
```
/go
/clear
/quit
```

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
