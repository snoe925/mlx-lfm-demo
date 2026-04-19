import json
import re
import os
import subprocess
from pathlib import Path
import stat
from urllib.parse import urlparse
from urllib.request import urlopen

# Define the sandbox root and the single user-visible share root.
# All tool I/O (read/write/list/exec/download/clean) is confined under ./share.
# ./share/tmp is the conventional scratch/script location; subdirectories are
# allowed anywhere under ./share.
SANDBOX_ROOT = os.path.abspath(os.getcwd())
SHARE_DIRNAME = "share"
SHARE_ROOT = os.path.join(SANDBOX_ROOT, SHARE_DIRNAME)
SHARE_TMP = os.path.join(SHARE_ROOT, "tmp")

# Whether the `linux` tool should execute scripts inside the QEMU sandbox.
# Default is False because QEMU boot is slow and painful in unit tests and
# most interactive flows. The chat application's `/sandbox` command flips
# this at runtime via set_sandbox_enabled().
USE_SANDBOX = False


def set_sandbox_enabled(enabled):
    """Enable or disable QEMU sandboxed execution for the `linux` tool.

    Returns the new value of the flag.
    """
    global USE_SANDBOX
    USE_SANDBOX = bool(enabled)
    return USE_SANDBOX


def is_sandbox_enabled():
    """True if the `linux` tool will run scripts inside the QEMU sandbox."""
    return USE_SANDBOX


def ensure_share_dirs():
    """Create ./share and ./share/tmp if they do not exist."""
    os.makedirs(SHARE_TMP, exist_ok=True)


SHARE_PREFIX = "./share"
SHARE_PREFIX_SLASH = SHARE_PREFIX + "/"


def get_safe_path(requested_path):
    """
    Resolve a requested path under ./share.

    The caller MUST use the './share' prefix explicitly. Accepted forms:
        "./share"                (the share root itself)
        "./share/a"              (a file or dir directly under ./share)
        "./share/tmp/run.sh"     (any depth under ./share)

    Anything else -- bare names, 'share/...' without './', absolute '/...',
    '../...', etc. -- is rejected and returns None.

    The resolved path must stay inside ./share after realpath (symlink)
    resolution. Returns the absolute filesystem path on success, or None
    on any violation.
    """
    if not isinstance(requested_path, str) or not requested_path:
        return None

    # Strip a single trailing slash for uniform handling, except when the
    # input is exactly "./share/" -> treat as "./share".
    if requested_path != SHARE_PREFIX_SLASH and requested_path.endswith("/"):
        requested_path = requested_path.rstrip("/")

    if requested_path == SHARE_PREFIX or requested_path == SHARE_PREFIX_SLASH:
        tail = ""
    elif requested_path.startswith(SHARE_PREFIX_SLASH):
        tail = requested_path[len(SHARE_PREFIX_SLASH) :]
    else:
        return None

    # Reject absolute components and traversal anywhere in the tail.
    if tail.startswith("/"):
        return None
    parts = tail.split("/") if tail else []
    if any(p in ("", ".", "..") for p in parts):
        return None

    full_path = os.path.abspath(os.path.join(SHARE_ROOT, tail))
    real_path = os.path.realpath(full_path)
    real_share = os.path.realpath(SHARE_ROOT)

    try:
        if os.path.commonpath([real_path, real_share]) == real_share:
            return full_path
    except ValueError:
        pass
    return None


def is_under_share_root(path):
    """True iff path (after realpath) is ./share itself or a descendant."""
    if path is None:
        return False
    real_path = os.path.realpath(path)
    real_share = os.path.realpath(SHARE_ROOT)
    try:
        return os.path.commonpath([real_path, real_share]) == real_share
    except ValueError:
        return False


def to_display_path(abs_path, is_dir=None):
    """
    Format an absolute path (under ./share) as a model-visible string.

    Always prefixed with './share/...'. Directories end with '/'. If is_dir
    is None, the filesystem is consulted.
    """
    rel = os.path.relpath(abs_path, SANDBOX_ROOT)  # e.g. "share/tmp/x"
    display = "./" + rel
    if is_dir is None:
        is_dir = os.path.isdir(abs_path)
    if is_dir and not display.endswith("/"):
        display += "/"
    return display


# Mapping of single-character escape sequences decoded by parse_tool_args.
# Any backslash escape not listed here passes through verbatim (the two
# characters '\' + x are preserved) so the parser never silently corrupts
# content it does not understand.
_SINGLE_CHAR_ESCAPES = {
    "n": "\n",
    "t": "\t",
    "r": "\r",
    "0": "\0",
    "\\": "\\",
    '"': '"',
    "'": "'",
}


def parse_tool_args(args_str):
    """
    Parse a comma-separated list of ``key="value"`` pairs from the inside of
    a tool call's parentheses.

    The value is a C-style double-quoted string that supports these escape
    sequences:

        \\n     newline       (0x0a)
        \\t     tab           (0x09)
        \\r     carriage ret  (0x0d)
        \\0     NUL           (0x00)
        \\\\    backslash     (\\)
        \\"     double quote  (")
        \\'     single quote  (')

    Any other backslash escape (e.g. ``\\x``) is preserved literally, so the
    parser never silently drops characters it does not understand.

    Whitespace and commas between ``key="value"`` pairs are skipped. The
    function is forgiving: on the first structural error it returns whatever
    has already been parsed rather than raising, so a model that emits a
    slightly malformed call can still make partial progress.
    """
    params = {}
    i = 0
    n = len(args_str)
    while i < n:
        # Skip inter-param whitespace and commas.
        while i < n and args_str[i] in " \t\r\n,":
            i += 1
        if i >= n:
            break

        # Parse a key: [A-Za-z_][A-Za-z0-9_]*
        key_start = i
        if not (args_str[i].isalpha() or args_str[i] == "_"):
            break
        i += 1
        while i < n and (args_str[i].isalnum() or args_str[i] == "_"):
            i += 1
        key = args_str[key_start:i]
        if not key:
            break

        # Optional whitespace, then '='.
        while i < n and args_str[i] in " \t":
            i += 1
        if i >= n or args_str[i] != "=":
            break
        i += 1
        while i < n and args_str[i] in " \t":
            i += 1

        # Opening quote.
        if i >= n or args_str[i] != '"':
            break
        i += 1

        # Scan the quoted value, decoding escapes.
        value_chars = []
        while i < n:
            c = args_str[i]
            if c == "\\" and i + 1 < n:
                nxt = args_str[i + 1]
                if nxt in _SINGLE_CHAR_ESCAPES:
                    value_chars.append(_SINGLE_CHAR_ESCAPES[nxt])
                else:
                    # Unknown escape: keep both bytes literal so content the
                    # parser does not understand is preserved rather than lost.
                    value_chars.append(c)
                    value_chars.append(nxt)
                i += 2
                continue
            if c == '"':
                i += 1
                break
            value_chars.append(c)
            i += 1

        params[key] = "".join(value_chars)

    return params


def handle_linux_execution(params):
    """
    Executes a script file located under ./share (typically ./share/tmp).
    The script runs with cwd set to its containing directory.
    """
    script_file_name = params.get("script_file_name", "")
    action = params.get("action", "run")

    if not script_file_name:
        return {"error": "No script_file_name provided"}
    if "run" not in action:
        return {"error": f"unknown action {action}"}

    safe = get_safe_path(script_file_name)
    if safe is None:
        return {
            "error": "script_file_name must start with './share' (e.g. './share/tmp/run.sh')"
        }
    if not os.path.isfile(safe):
        return {"error": f"script not found: {script_file_name}"}

    # Note: write_file now stores `content` with real newlines already, since
    # parse_tool_args decodes backslash escapes at parse time. No post-write
    # fixup is needed here.

    p = Path(safe)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)

    if USE_SANDBOX:
        # Defer the import so importing tools.py does not pull in the QEMU
        # plumbing (and its transitive Popen/socket imports) unless the user
        # has explicitly enabled sandbox mode.
        try:
            from sandbox import run_script_in_sandbox
        except ImportError as exc:
            return {
                "stdout": "",
                "stderr": f"sandbox module unavailable: {exc}",
                "exit_code": -2,
            }
        return run_script_in_sandbox(str(p))

    try:
        result = subprocess.run(
            "./" + p.name,
            shell=True,
            cwd=str(p.parent),
            capture_output=True,
            text=True,
            timeout=60,
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "exit_code": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"stdout": "", "stderr": "Command timed out", "exit_code": -1}
    except Exception as e:
        return {"stdout": "", "stderr": f"Execution failed: {str(e)}", "exit_code": -2}


def handle_list_files(params):
    location = params.get("location")
    if not location:
        return {"error": "missing location (must start with './share')"}

    path = get_safe_path(location)
    if path is None:
        return {"error": "paths must start with './share' (e.g. './share/tmp')"}
    if not os.path.isdir(path):
        return {"error": "Invalid directory or access denied"}

    try:
        visible_entries = []
        for name in os.listdir(path):
            entry_path = os.path.join(path, name)
            if not is_under_share_root(entry_path):
                continue
            visible_entries.append(
                to_display_path(entry_path, is_dir=os.path.isdir(entry_path))
            )
        return sorted(visible_entries)
    except Exception as e:
        return {"error": str(e)}


def handle_read_file(params):
    path_str = params.get("file_path")
    if not path_str:
        return {"error": "missing file_path"}

    path = get_safe_path(path_str)
    if path is None:
        return {
            "error": "file_path must start with './share' (e.g. './share/tmp/foo.sh')"
        }
    try:
        with open(path, "r") as f:
            return f.read()
    except Exception as e:
        return {"error": str(e)}


# Interpreters the model routinely picks when writing shell/python scripts.
# We use this list to detect and repair the common failure mode where the
# model emits a shebang without the required trailing newline, e.g.:
#
#     content="#!/bin/shdate"        -> becomes "#!/bin/sh\ndate"
#     content="#!/bin/bashecho hi"   -> becomes "#!/bin/bash\necho hi"
#
# A shebang line MUST be terminated by a newline (or at minimum a space
# before any interpreter args) for the kernel to execve it correctly. When
# the model forgets the `\n`, the resulting script looks like a single
# very long command name and the `linux` tool fails with "command not
# found" / "no such file". Normalising at write time is strictly safer
# than writing the broken file and letting the model try to debug it.
# NOTE: keep longer / more specific entries FIRST so the prefix match does
# not accidentally consume part of a longer interpreter name. For example,
# "#!/usr/bin/env python3print('x')" must match "#!/usr/bin/env python3",
# not "#!/usr/bin/env python" (which would leave "3print('x')" as tail).
_SHEBANG_INTERPRETERS = (
    "#!/usr/bin/env python3",
    "#!/usr/bin/env python",
    "#!/usr/bin/env bash",
    "#!/usr/bin/env zsh",
    "#!/usr/bin/env sh",
    "#!/usr/bin/python3",
    "#!/usr/bin/python",
    "#!/bin/bash",
    "#!/bin/zsh",
    "#!/bin/sh",
)


def _normalize_shebang_newline(content):
    """Ensure a known shebang at the start of ``content`` is followed by a
    newline.

    The model is instructed (see LFMAGENT.md) to always emit ``\\n`` after
    the shebang, but it frequently forgets. If we see one of the well-known
    interpreter prefixes at position 0 and the very next character is
    neither a newline nor whitespace (which would be valid interpreter
    arguments such as ``#!/bin/sh -eu\\n``), we insert a ``\\n`` between
    the shebang and the rest of the content.

    When the model's content already starts with a correctly terminated
    shebang, this function returns the original string unchanged.
    """
    if not isinstance(content, str) or not content.startswith("#!"):
        return content

    for prefix in _SHEBANG_INTERPRETERS:
        if content.startswith(prefix):
            tail = content[len(prefix) :]
            if tail and tail[0] not in ("\n", "\r", " ", "\t"):
                return prefix + "\n" + tail
            return content

    # Unknown shebang interpreter (e.g. "#!/opt/custom/bin/tool"). Fall back
    # to a generic rule: if the first line of content contains no newline
    # at all and begins with "#!", append a "\n" after the first token so
    # the rest of the content does not merge into the shebang path.
    if "\n" not in content:
        # No newline anywhere -> can't repair safely without guessing the
        # end of the shebang. Leave untouched; the script will fail loudly
        # at execution time and the model can retry.
        return content

    return content


def handle_write_file(params):
    path_str = params.get("file_path")
    content = params.get("content")

    if not path_str or content is None:
        return {"error": "missing file_path or content"}

    path = get_safe_path(path_str)
    if path is None:
        return {
            "error": "file_path must start with './share' (e.g. './share/tmp/foo.sh')"
        }

    # Repair a missing newline after a well-known shebang before writing.
    # This is forgiving-by-default so the model can keep moving even when
    # it forgets the `\n` escape after e.g. `#!/bin/sh`.
    content = _normalize_shebang_newline(content)

    try:
        ensure_share_dirs()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        return "SUCCESS"
    except Exception as e:
        return {"error": str(e)}


def handle_clean_tmp(params):
    """
    Remove files under ./share/tmp.
    With no arguments, removes all regular files (recursively) under ./share/tmp
    while preserving the directory structure.
    With file_name, removes a single file at ./share/tmp/<file_name> (or a
    relative sub-path under ./share/tmp).
    """
    ensure_share_dirs()
    tmp_root = os.path.realpath(SHARE_TMP)
    if not os.path.isdir(tmp_root):
        return {"error": "./share/tmp does not exist"}

    file_name = params.get("file_name") if params else None
    removed = []

    try:
        if file_name:
            # Normalize user-supplied value; forbid absolute and traversal.
            if os.path.isabs(file_name) or ".." in Path(file_name).parts:
                return {"error": "invalid file_name"}

            target = os.path.abspath(os.path.join(tmp_root, file_name))
            real_target = os.path.realpath(target)

            # Must resolve under ./share/tmp specifically.
            try:
                if os.path.commonpath([real_target, tmp_root]) != tmp_root:
                    return {"error": "file_name is outside ./share/tmp"}
            except ValueError:
                return {"error": "file_name is outside ./share/tmp"}

            if not os.path.exists(real_target):
                return {"error": f"{file_name} not found in ./share/tmp"}
            if os.path.isdir(real_target):
                return {"error": "clean_tmp only removes files"}

            os.remove(real_target)
            removed.append(to_display_path(real_target, is_dir=False))
        else:
            for dirpath, _dirnames, filenames in os.walk(tmp_root):
                for name in filenames:
                    entry = os.path.join(dirpath, name)
                    real_entry = os.path.realpath(entry)
                    try:
                        if os.path.commonpath([real_entry, tmp_root]) != tmp_root:
                            continue
                    except ValueError:
                        continue
                    try:
                        os.remove(entry)
                        removed.append(to_display_path(real_entry, is_dir=False))
                    except OSError:
                        continue

        return {"status": "SUCCESS", "removed": sorted(removed)}
    except Exception as e:
        return {"error": f"clean_tmp failed: {str(e)}"}


def handle_wget(params):
    """Download a URL into ./share/tmp and return metadata."""
    url = params.get("url")
    file_name = params.get("file_name")

    if not url:
        return {"error": "missing url"}

    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return {"error": "url must use http or https"}

    if not file_name:
        guessed_name = os.path.basename(parsed.path)
        file_name = guessed_name or "download.bin"

    # Allow a sub-path under ./share/tmp, but forbid absolute/traversal.
    if os.path.isabs(file_name) or ".." in Path(file_name).parts:
        return {"error": "invalid file_name"}
    if not file_name or file_name.endswith("/"):
        return {"error": "invalid file_name"}

    ensure_share_dirs()
    output_path = os.path.abspath(os.path.join(SHARE_TMP, file_name))
    tmp_real = os.path.realpath(SHARE_TMP)
    try:
        if (
            os.path.commonpath(
                [os.path.realpath(os.path.dirname(output_path)), tmp_real]
            )
            != tmp_real
        ):
            return {"error": "file_name is outside ./share/tmp"}
    except ValueError:
        return {"error": "file_name is outside ./share/tmp"}

    os.makedirs(os.path.dirname(output_path), exist_ok=True)

    try:
        with urlopen(url, timeout=30) as response:
            content = response.read()

        with open(output_path, "wb") as f:
            f.write(content)

        return {
            "status": "SUCCESS",
            "file_path": to_display_path(output_path, is_dir=False),
            "bytes": len(content),
        }
    except Exception as e:
        return {"error": f"download failed: {str(e)}"}


# Map tool names to their handler functions
TOOL_HANDLERS = {
    "linux": handle_linux_execution,
    "list_files": handle_list_files,
    "read_file": handle_read_file,
    "write_file": handle_write_file,
    "wget": handle_wget,
    "clean_tmp": handle_clean_tmp,
}


def _extract_args_span(tool_call_str):
    """
    Return the substring between the first '(' that follows the tool name
    and its matching ')'. The matcher walks the string respecting
    double-quoted regions and backslash escapes, so stray ')' inside a
    value (for example inside a shell command stored in `content`) does
    not close the argument list prematurely.

    Returns the args body string, or None if no balanced parenthesis pair
    can be located.
    """
    open_idx = tool_call_str.find("(")
    if open_idx < 0:
        return None

    i = open_idx + 1
    depth = 1
    in_str = False
    while i < len(tool_call_str):
        c = tool_call_str[i]
        if in_str:
            if c == "\\" and i + 1 < len(tool_call_str):
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            i += 1
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return tool_call_str[open_idx + 1 : i]
        i += 1
    return None


def tool_call(tool_call_str):
    """
    Parse a tool-call string and dispatch it to the matching handler.

    Input looks like (the wrappers are optional):

        <|tool_call_start|>[tool_name(key="value", key2="value")]<|tool_call_end|>

    Values are C-style double-quoted strings; parse_tool_args() decodes the
    common escape sequences (\\n, \\t, \\r, \\0, \\\\, \\", \\'). Any other
    backslash escape is preserved literally.
    """
    try:
        match = re.search(r"(\w+)\(", tool_call_str)
        if not match:
            return json.dumps({"error": "Invalid tool call format"})

        tool_name = match.group(1)
        handler = TOOL_HANDLERS.get(tool_name)

        if not handler:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

        args_str = _extract_args_span(tool_call_str[match.start() :])
        if args_str is None:
            return json.dumps(
                {"error": "Invalid tool call format: unbalanced parentheses"}
            )

        params = parse_tool_args(args_str)
        result = handler(params)

        if isinstance(result, (dict, list)):
            return json.dumps(result)
        return json.dumps(str(result))

    except Exception as e:
        return json.dumps({"error": f"Execution failed: {str(e)}"})


TOOLS = [
    {
        "name": "linux",
        "description": (
            "Runs a shell script located under ./share (typically ./share/tmp). "
            "The script runs with its containing directory as cwd."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "script_file_name": {
                    "type": "string",
                    "description": (
                        "Path to the script; MUST start with './share/' "
                        "(e.g. './share/tmp/run.sh'). First create it with write_file."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": ["run"],
                    "description": "run action will run the script.",
                },
            },
            "required": ["script_file_name", "action"],
        },
    },
    {
        "name": "list_files",
        "description": (
            "Lists files in a directory under ./share. Returned entries are "
            "prefixed with './share/...'; directories end with a trailing '/'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": (
                        "Directory path; MUST start with './share' "
                        "(e.g. './share', './share/tmp')."
                    ),
                },
            },
            "required": ["location"],
        },
    },
    {
        "name": "read_file",
        "description": "Reads the content of a file under ./share.",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": (
                        "Path to a file; MUST start with './share/' "
                        "(e.g. './share/tmp/foo.sh')."
                    ),
                }
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": (
            "Writes content to a file under ./share. Subdirectories under "
            "./share are created automatically."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": (
                        "Destination path; MUST start with './share/' "
                        "(e.g. './share/tmp/run.sh')."
                    ),
                },
                "content": {
                    "type": "string",
                    "description": "The content to write to the file",
                },
            },
            "required": ["file_path", "content"],
        },
    },
    {
        "name": "wget",
        "description": "Downloads a URL into ./share/tmp.",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "HTTP/HTTPS URL to download",
                },
                "file_name": {
                    "type": "string",
                    "description": (
                        "Optional output path relative to ./share/tmp "
                        "(e.g. 'a.zip' or 'nested/x.bin'). May include "
                        "subdirectories; must not start with '/' or contain '..'."
                    ),
                },
            },
            "required": ["url"],
        },
    },
    {
        "name": "clean_tmp",
        "description": (
            "Removes files from ./share/tmp. With no arguments, removes every "
            "regular file under ./share/tmp (recursively). With file_name, "
            "removes just that file (relative to ./share/tmp)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_name": {
                    "type": "string",
                    "description": (
                        "Optional file path relative to ./share/tmp "
                        "(e.g. 'run_date.sh' or 'sub/x.bin'). If omitted, "
                        "all files under ./share/tmp are removed."
                    ),
                },
            },
            "required": [],
        },
    },
]
