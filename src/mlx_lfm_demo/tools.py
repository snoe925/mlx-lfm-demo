import json
import re
import os

# Define the sandbox root
SANDBOX_ROOT = os.path.abspath(os.getcwd())

def get_safe_path(requested_path):
    """Expands relative paths and ensures they are within the SANDBOX_ROOT."""
    if os.path.isabs(requested_path):
        requested_path = requested_path.lstrip('/')
    
    full_path = os.path.abspath(os.path.join(SANDBOX_ROOT, requested_path))
    
    if full_path.startswith(SANDBOX_ROOT):
        return full_path
    return None

def handle_bash_execution(params):
    """Executes a bash command within the sandbox directory."""
    import subprocess
    import shlex
    
    bash_command = params.get('bash_command', '')
    if not bash_command:
        return {'error': 'Empty bash command'}
    
    try:
        # Execute command in sandbox directory with timeout
        result = subprocess.run(
            bash_command,
            shell=True,
            cwd=SANDBOX_ROOT,
            capture_output=True,
            text=True,
            timeout=30  # 30 second timeout
        )
        
        if result.returncode == 0:
            return result.stdout
        else:
            return {
                'error': 'Command failed',
                'stderr': result.stderr,
                'returncode': result.returncode
            }
    except subprocess.TimeoutExpired:
        return {'error': 'Command timed out after 30 seconds'}
    except Exception as e:
        return {'error': f'Execution failed: {str(e)}'}

def handle_list_files(params):
    directory = params.get('directory', '.')
    path = get_safe_path(directory)
    if path and os.path.isdir(path):
        try:
            return os.listdir(path)
        except Exception as e:
            return {'error': str(e)}
    return {'error': 'Invalid directory or access denied'}

def handle_read_file(params):
    path_str = params.get('file_path')
    if not path_str:
        return {'error': 'missing file_path'}
    
    path = get_safe_path(path_str)
    if path:
        try:
            with open(path, 'r') as f:
                return f.read()
        except Exception as e:
            return {'error': str(e)}
    return {'error': 'Access denied: Path outside sandbox'}

def handle_write_file(params):
    path_str = params.get('file_path')
    content = params.get('content')
    
    if not path_str or content is None:
        return {'error': 'missing file_path or content'}
    
    path = get_safe_path(path_str)
    if path:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write(content)
            return "File written successfully"
        except Exception as e:
            return {'error': str(e)}
    return {'error': 'Access denied: Path outside sandbox'}

# Map tool names to their handler functions
TOOL_HANDLERS = {
    "bash_execution": handle_bash_execution,
    "list_files": handle_list_files,
    "read_file": handle_read_file,
    "write_file": handle_write_file
}

def tool_call(tool_call_str):
    """
    Parses the tool call string and dispatches it to the correct handler.
    Note: The current main.py passes a string like '[tool_name(arg="val")]'
    """
    try:
        # Extract tool name: find characters before the parenthesis
        match = re.search(r'(\w+)\(', tool_call_str)
        if not match:
            return json.dumps({'error': 'Invalid tool call format'})
        
        tool_name = match.group(1)
        handler = TOOL_HANDLERS.get(tool_name)
        
        if not handler:
            return json.dumps({'error': f'Unknown tool: {tool_name}'})

        # Extract arguments: find everything inside the parentheses
        arg_start = tool_call_str.find('(') + 1
        arg_end = tool_call_str.rfind(')')
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
        return json.dumps({'error': f'Execution failed: {str(e)}'})

TOOLS = [
    {
        "name": "bash_execution",
        "description": "Runs a bash command line",
        "parameters": {
            "type": "object",
            "properties": {
                "bash_command": {
                    "type": "string",
                    "description": "bash command line to be parsed by bash",
                }
            },
            "required": ["bash_command"],
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
                }
            },
            "required": ["file_path", "content"],
        },
    },
]
