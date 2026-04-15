import json
import re
import os
import subprocess
from pathlib import Path
import stat
from urllib.parse import urlparse
from urllib.request import urlopen

# Define the sandbox root
SANDBOX_ROOT = os.path.abspath(os.getcwd())


def get_safe_path(requested_path):
    """Expands relative paths and ensures they are within the SANDBOX_ROOT."""
    if os.path.isabs(requested_path):
        requested_path = requested_path.lstrip("/")

    full_path = os.path.abspath(os.path.join(SANDBOX_ROOT, requested_path))

    if full_path.startswith(SANDBOX_ROOT):
        return full_path
    return None


def rewrite_escapes(filename):
    """
    Read the file filename and re-write the file with \n as real new lines.
    """
    with open(filename, "r") as f:
        content = f.read()

    # Replace literal '\n' string with actual newline character
    new_content = content.replace("\\n", "\n")

    with open(filename, "w") as f:
        f.write(new_content)


def handle_linux_execution(params):
    """
    Executes a script in an isolated environment (simulated).
    Supports versioning and rollback.
    """

    script_file_name = params.get("script_file_name", "")
    action = params.get("action", "run")

    if not script_file_name:
        return {"error": "No script_file_name provided"}
    if "run" not in action:
        return {"error": f"unknown action {action}"}

    script_file_name = get_safe_path(script_file_name)
    if script_file_name is None:
        return {"error": "unsafe path"}

    rewrite_escapes(script_file_name)

    p = Path(script_file_name)
    # Add execute permission for everyone (+x)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)

    try:
        result = subprocess.run(
            "./" + p.name,
            shell=True,
            cwd=SANDBOX_ROOT + "/tmp",
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
    directory = params.get("directory", ".")
    path = get_safe_path(directory)
    if path and os.path.isdir(path):
        try:
            return os.listdir(path)
        except Exception as e:
            return {"error": str(e)}
    return {"error": "Invalid directory or access denied"}


def handle_read_file(params):
    path_str = params.get("file_path")
    if not path_str:
        return {"error": "missing file_path"}

    path = get_safe_path(path_str)
    if path:
        try:
            with open(path, "r") as f:
                return f.read()
        except Exception as e:
            return {"error": str(e)}
    return {"error": "Access denied: Path outside sandbox"}


def handle_write_file(params):
    path_str = params.get("file_path")
    content = params.get("content")

    if not path_str or content is None:
        return {"error": "missing file_path or content"}

    path = get_safe_path(path_str)
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as f:
                f.write(content)
            return "SUCCESS"
        except Exception as e:
            return {"error": str(e)}
    return {"error": "Access denied: Path outside sandbox"}


def handle_wget(params):
    """Download a URL into ./tmp and return metadata."""
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

    file_name = os.path.basename(file_name)
    if not file_name:
        return {"error": "invalid file_name"}

    tmp_dir = os.path.join(SANDBOX_ROOT, "tmp")
    os.makedirs(tmp_dir, exist_ok=True)
    output_path = os.path.join(tmp_dir, file_name)

    try:
        with urlopen(url, timeout=30) as response:
            content = response.read()

        with open(output_path, "wb") as f:
            f.write(content)

        return {
            "status": "SUCCESS",
            "file_path": os.path.relpath(output_path, SANDBOX_ROOT),
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
}


def tool_call(tool_call_str):
    """
    Parses the tool call string and dispatches it to the correct handler.
    Note: The current main.py passes a string like '[tool_name(arg="val")]'
    """
    try:
        # Extract tool name: find characters before the parenthesis
        match = re.search(r"(\w+)\(", tool_call_str)
        if not match:
            return json.dumps({"error": "Invalid tool call format"})

        tool_name = match.group(1)
        handler = TOOL_HANDLERS.get(tool_name)

        if not handler:
            return json.dumps({"error": f"Unknown tool: {tool_name}"})

        # Extract arguments: find everything inside the parentheses
        arg_start = tool_call_str.find("(") + 1
        arg_end = tool_call_str.find(")]<|tool_call_end|>")
        args_str = tool_call_str[arg_start:arg_end]

        # Parse key="value" pairs
        params = {}
        arg_pattern = re.compile(r'(\w+)="([^"]*)"')
        for key, value in arg_pattern.findall(args_str):
            params[key] = value

        result = handler(params)

        if isinstance(result, (dict, list)):
            return json.dumps(result)
        return json.dumps(str(result))

    except Exception as e:
        return json.dumps({"error": f"Execution failed: {str(e)}"})


TOOLS = [
    {
        "name": "linux",
        "description": "Runs shell scripts in an isolated environment.",
        "parameters": {
            "type": "object",
            "properties": {
                "script_file_name": {
                    "type": "string",
                    "description": "The script file name to run. First you must use write_file to create the script.",
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
        "description": "Lists files in a directory",
        "parameters": {
            "type": "object",
            "properties": {
                "directory": {
                    "type": "string",
                    "description": "The directory to list, defaults to current directory",
                }
            },
        },
    },
    {
        "name": "read_file",
        "description": "Reads the content of a file within the sandbox",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The relative path to the file to read",
                }
            },
            "required": ["file_path"],
        },
    },
    {
        "name": "write_file",
        "description": "Writes content to a file within the sandbox",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "The relative path to the file to write to",
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
        "description": "Downloads a URL into ./tmp",
        "parameters": {
            "type": "object",
            "properties": {
                "url": {
                    "type": "string",
                    "description": "HTTP/HTTPS URL to download",
                },
                "file_name": {
                    "type": "string",
                    "description": "Optional output file name under ./tmp",
                },
            },
            "required": ["url"],
        },
    },
]
