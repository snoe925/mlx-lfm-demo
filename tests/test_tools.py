import os
import json
import pytest
from tools import (
    get_safe_path, 
    handle_list_files, 
    handle_read_file, 
    handle_write_file, 
    tool_call, 
    SANDBOX_ROOT
)

def test_get_safe_path_valid():
    # Test relative path
    path = get_safe_path("main.py")
    assert path == os.path.join(SANDBOX_ROOT, "main.py")
    
    # Test absolute path (should be treated as relative to sandbox)
    path = get_safe_path("/main.py")
    assert path == os.path.join(SANDBOX_ROOT, "main.py")

def test_get_safe_path_invalid():
    # Test directory traversal
    path = get_safe_path("../outside.txt")
    assert path is None

def test_handle_list_files_success(tmp_path):
    # We need to mock SANDBOX_ROOT or use a controlled environment
    # For testing, it's better to let tools use the actual SANDBOX_ROOT if possible, 
    # but for unit tests, we might want to monkeypatch it.
    # However, since SANDBOX_ROOT is a constant in tools.py, we can't easily change it 
    # unless we use patch.
    pass

# Since SANDBOX_ROOT is hardcoded to os.getcwd() at import time, 
# let's write tests that work within the current directory structure.

def test_handle_list_files_current_dir():
    result = handle_list_files({"directory": "."})
    assert isinstance(result, list)
    assert "main.py" in result or "tools.py" in result

def test_handle_list_files_invalid_dir():
    result = handle_list_files({"directory": "non_existent_dir_12345"})
    assert isinstance(result, dict)
    assert "error" in result

def test_handle_read_file_success():
    result = handle_read_file({"file_path": "README.md"})
    assert isinstance(result, str)
    assert "# mlx-lfm-demo" in result

def test_handle_read_file_not_found():
    result = handle_read_file({"file_path": "non_existent.txt"})
    assert isinstance(result, dict)
    assert "error" in result

def test_handle_write_file_success():
    test_file = "test_output.txt"
    test_content = "hello world"
    result = handle_write_file({"file_path": test_file, "content": test_content})
    assert result == "File written successfully"
    
    with open(test_file, "r") as f:
        assert f.read() == test_content
    
    os.remove(test_file)

def test_handle_write_file_outside_sandbox():
    # This is tricky because get_safe_path uses SANDBOX_ROOT
    # Trying to use a path that resolves outside
    result = handle_write_file({"file_path": "../outside.txt", "content": "hack"})
    assert isinstance(result, dict)
    assert "error" in result

def test_tool_call_parsing_list_files():
    res_str = tool_call('list_files(directory=".")')
    res = json.loads(res_str)
    assert isinstance(res, list)

def test_tool_call_parsing_read_file():
    res_str = tool_call('read_file(file_path="README.md")')
    res = json.loads(res_str)
    assert isinstance(res, str)
    assert "# mlx-lfm-demo" in res

def test_tool_call_parsing_write_file():
    res_str = tool_call('write_file(file_path="test_write.txt", content="test content")')
    res = json.loads(res_str)
    assert res == "File written successfully"
    
    with open("test_write.txt", "r") as f:
        assert f.read() == "test content"
    os.remove("test_write.txt")

def test_tool_call_unknown_tool():
    res_str = tool_call('unknown_tool(arg="val")')
    res = json.loads(res_str)
    assert "error" in res
    assert "Unknown tool" in res["error"]

def test_tool_call_malformed():
    res_str = tool_call('not_a_tool_call')
    res = json.loads(res_str)
    assert "error" in res
    assert "Invalid tool call format" in res["error"]
