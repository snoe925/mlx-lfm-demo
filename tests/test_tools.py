import os
import json
import mlx_lfm_demo.tools as tools_module
from mlx_lfm_demo.tools import (
    get_safe_path,
    handle_list_files,
    handle_read_file,
    handle_write_file,
    handle_wget,
    handle_clean_tmp,
    parse_tool_args,
    tool_call,
    SANDBOX_ROOT,
    SHARE_ROOT,
    SHARE_TMP,
    ensure_share_dirs,
)


def setup_module(_module):
    ensure_share_dirs()


# ---------------------------------------------------------------------------
# get_safe_path: only './share...' is accepted
# ---------------------------------------------------------------------------


def test_get_safe_path_accepts_share_prefix():
    assert get_safe_path("./share") == SHARE_ROOT
    assert get_safe_path("./share/") == SHARE_ROOT
    assert get_safe_path("./share/a.txt") == os.path.join(SHARE_ROOT, "a.txt")
    assert get_safe_path("./share/tmp") == SHARE_TMP
    assert get_safe_path("./share/tmp/") == SHARE_TMP
    assert get_safe_path("./share/tmp/run_date.sh") == os.path.join(
        SHARE_TMP, "run_date.sh"
    )
    assert get_safe_path("./share/deep/deeper/x.txt") == os.path.join(
        SHARE_ROOT, "deep", "deeper", "x.txt"
    )


def test_get_safe_path_rejects_other_forms():
    for bad in [
        "",
        None,
        "main.py",
        "share/main.py",  # missing './'
        "/share/main.py",  # absolute
        "/main.py",
        "tmp/foo.sh",
        "share/tmp/foo.sh",
        "./shared/foo",  # typo / wrong dir
        "./share-bad/foo",
        "../outside.txt",
        "./share/../outside.txt",
        "./share//double",  # empty component
        "./share/./x",  # '.' component
        "./share/tmp/../../x",  # traversal
    ]:
        assert get_safe_path(bad) is None, f"expected None for {bad!r}"


# ---------------------------------------------------------------------------
# handle_list_files: output format prefixes './share/' and ends dirs with '/'
# ---------------------------------------------------------------------------


def test_handle_list_files_share_tmp_formats_entries():
    marker = os.path.join(SHARE_TMP, "_list_marker.txt")
    sub = os.path.join(SHARE_TMP, "_list_sub")
    os.makedirs(sub, exist_ok=True)
    with open(marker, "w") as f:
        f.write("x")
    try:
        result = handle_list_files({"location": "./share/tmp"})
        assert isinstance(result, list)
        assert "./share/tmp/_list_marker.txt" in result
        assert "./share/tmp/_list_sub/" in result
        # No entries should ever lack the './share/' prefix.
        for entry in result:
            assert entry.startswith("./share/"), entry
    finally:
        os.remove(marker)
        os.rmdir(sub)


def test_handle_list_files_root_of_share():
    ensure_share_dirs()
    result = handle_list_files({"location": "./share"})
    assert isinstance(result, list)
    # tmp dir inside share is guaranteed to exist and must be rendered as a dir.
    assert "./share/tmp/" in result


def test_handle_list_files_subdirectory():
    sub = os.path.join(SHARE_TMP, "sub_a")
    nested_dir = os.path.join(sub, "inner")
    os.makedirs(nested_dir, exist_ok=True)
    marker = os.path.join(sub, "nested.txt")
    with open(marker, "w") as f:
        f.write("x")
    try:
        result = handle_list_files({"location": "./share/tmp/sub_a"})
        assert "./share/tmp/sub_a/nested.txt" in result
        assert "./share/tmp/sub_a/inner/" in result
    finally:
        os.remove(marker)
        os.rmdir(nested_dir)
        os.rmdir(sub)


def test_handle_list_files_missing_location():
    result = handle_list_files({})
    assert isinstance(result, dict)
    assert "error" in result


def test_handle_list_files_rejects_non_share_prefix():
    for bad in ["tmp", "share/tmp", "../", ".", "/etc"]:
        result = handle_list_files({"location": bad})
        assert isinstance(result, dict), f"expected error dict for {bad!r}"
        assert "error" in result


def test_handle_list_files_invalid_dir():
    result = handle_list_files({"location": "./share/tmp/non_existent_dir_12345"})
    assert isinstance(result, dict)
    assert "error" in result


# ---------------------------------------------------------------------------
# handle_read_file / handle_write_file
# ---------------------------------------------------------------------------


def test_handle_read_file_success():
    target = os.path.join(SHARE_ROOT, "_read_me.txt")
    with open(target, "w") as f:
        f.write("hello from share\n")
    try:
        result = handle_read_file({"file_path": "./share/_read_me.txt"})
        assert isinstance(result, str)
        assert "hello from share" in result
    finally:
        os.remove(target)


def test_handle_read_file_rejects_non_share_prefix():
    for bad in ["_read_me.txt", "share/_read_me.txt", "/etc/passwd", "../README.md"]:
        result = handle_read_file({"file_path": bad})
        assert isinstance(result, dict), f"expected error dict for {bad!r}"
        assert "error" in result


def test_handle_read_file_not_found():
    result = handle_read_file({"file_path": "./share/non_existent.txt"})
    assert isinstance(result, dict)
    assert "error" in result


def test_handle_write_file_success():
    rel = "./share/tmp/_test_output.txt"
    test_content = "hello world"
    result = handle_write_file({"file_path": rel, "content": test_content})
    assert result == "SUCCESS"

    abs_path = os.path.join(SHARE_TMP, "_test_output.txt")
    with open(abs_path, "r") as f:
        assert f.read() == test_content
    os.remove(abs_path)


def test_handle_write_file_creates_subdirs():
    rel = "./share/tmp/deep/deeper/_w.txt"
    result = handle_write_file({"file_path": rel, "content": "nested"})
    assert result == "SUCCESS"

    abs_path = os.path.join(SHARE_TMP, "deep", "deeper", "_w.txt")
    assert os.path.isfile(abs_path)

    os.remove(abs_path)
    os.rmdir(os.path.dirname(abs_path))
    os.rmdir(os.path.dirname(os.path.dirname(abs_path)))


def test_handle_write_file_rejects_non_share_prefix():
    for bad in ["out.txt", "tmp/out.txt", "../outside.txt", "/etc/foo"]:
        result = handle_write_file({"file_path": bad, "content": "x"})
        assert isinstance(result, dict), f"expected error dict for {bad!r}"
        assert "error" in result


def test_handle_write_file_repairs_missing_shebang_newline():
    """A missing newline after a known shebang is repaired at write time."""
    cases = [
        ("#!/bin/shdate", "#!/bin/sh\ndate"),
        ("#!/bin/bashecho hi", "#!/bin/bash\necho hi"),
        ("#!/usr/bin/env bashecho hi", "#!/usr/bin/env bash\necho hi"),
        ("#!/usr/bin/env python3print('x')", "#!/usr/bin/env python3\nprint('x')"),
    ]
    for i, (broken, expected) in enumerate(cases):
        rel = f"./share/tmp/_shebang_fix_{i}.sh"
        result = handle_write_file({"file_path": rel, "content": broken})
        assert result == "SUCCESS", (broken, result)
        abs_path = os.path.join(SHARE_TMP, f"_shebang_fix_{i}.sh")
        with open(abs_path, "r") as f:
            assert f.read() == expected, (broken, expected)
        os.remove(abs_path)


def test_handle_write_file_preserves_valid_shebangs():
    """Correctly terminated shebangs (\\n or a space for flags) pass through."""
    valid_contents = [
        "#!/bin/sh\ndate\n",
        "#!/bin/sh -eu\ndate\n",
        "#!/bin/bash\necho ok\n",
        "#!/usr/bin/env python3\nprint('ok')\n",
        # Unknown interpreter: untouched.
        "#!/opt/custom/bin/tool\nrun\n",
        # Not a shebang at all: untouched.
        "plain text without shebang",
    ]
    for i, content in enumerate(valid_contents):
        rel = f"./share/tmp/_shebang_ok_{i}.sh"
        result = handle_write_file({"file_path": rel, "content": content})
        assert result == "SUCCESS"
        abs_path = os.path.join(SHARE_TMP, f"_shebang_ok_{i}.sh")
        with open(abs_path, "r") as f:
            assert f.read() == content, content
        os.remove(abs_path)


# ---------------------------------------------------------------------------
# tool_call() string-parse path
# ---------------------------------------------------------------------------


def test_tool_call_parsing_list_files():
    marker = os.path.join(SHARE_TMP, "_list_marker2.txt")
    with open(marker, "w") as f:
        f.write("x")
    try:
        res_str = tool_call('list_files(location="./share/tmp")')
        res = json.loads(res_str)
        assert isinstance(res, list)
        assert "./share/tmp/_list_marker2.txt" in res
    finally:
        os.remove(marker)


def test_tool_call_parsing_read_file():
    target = os.path.join(SHARE_ROOT, "_rf.txt")
    with open(target, "w") as f:
        f.write("tool-call read test\n")
    try:
        res_str = tool_call('read_file(file_path="./share/_rf.txt")')
        res = json.loads(res_str)
        assert isinstance(res, str)
        assert "tool-call read test" in res
    finally:
        os.remove(target)


def test_tool_call_parsing_write_file():
    res_str = tool_call(
        'write_file(file_path="./share/tmp/_test_write.txt", content="test content")'
    )
    res = json.loads(res_str)
    assert res == "SUCCESS"

    abs_path = os.path.join(SHARE_TMP, "_test_write.txt")
    with open(abs_path, "r") as f:
        assert f.read() == "test content"
    os.remove(abs_path)


def test_parse_tool_args_basic():
    assert parse_tool_args('a="1"') == {"a": "1"}
    assert parse_tool_args('a="1", b="two"') == {"a": "1", "b": "two"}
    assert parse_tool_args('  a="1" ,  b="two"  ') == {"a": "1", "b": "two"}


def test_parse_tool_args_escapes():
    # \n, \t, \r, \0 decode to real control characters.
    parsed = parse_tool_args(r'x="line1\nline2\tend"')
    assert parsed == {"x": "line1\nline2\tend"}
    parsed = parse_tool_args(r'x="\r\0"')
    assert parsed == {"x": "\r\0"}


def test_parse_tool_args_embedded_quotes():
    # \" and \\ decode; single quote works with or without an escape.
    parsed = parse_tool_args(r'x="say \"hi\"", y="a\\b", z="don\'t"')
    assert parsed == {"x": 'say "hi"', "y": "a\\b", "z": "don't"}


def test_parse_tool_args_unknown_escape_passes_through():
    # \x is not a supported escape; both characters must survive.
    parsed = parse_tool_args(r'x="a\xb"')
    assert parsed == {"x": "a\\xb"}


def test_parse_tool_args_closing_paren_in_value_is_preserved():
    # The scanner must not terminate on ) inside a quoted value.
    parsed = parse_tool_args(r'cmd="echo \"hi)\""')
    assert parsed == {"cmd": 'echo "hi)"'}


def test_tool_call_write_file_decodes_newline_escape():
    res_str = tool_call(
        'write_file(file_path="./share/tmp/_nl.sh", content="#!/bin/sh\\ndate\\n")'
    )
    assert json.loads(res_str) == "SUCCESS"
    abs_path = os.path.join(SHARE_TMP, "_nl.sh")
    try:
        with open(abs_path, "r") as f:
            contents = f.read()
        # Real newlines are stored; no literal backslash-n sequences.
        assert contents == "#!/bin/sh\ndate\n"
        assert "\\n" not in contents
    finally:
        os.remove(abs_path)


def test_tool_call_write_file_decodes_embedded_double_quotes():
    # With the new parser the model can include escaped \" inside content.
    res_str = tool_call(
        'write_file(file_path="./share/tmp/_q.sh", '
        'content="#!/bin/sh\\ndate \'+%Y\' \\"label\\"\\n")'
    )
    assert json.loads(res_str) == "SUCCESS"
    abs_path = os.path.join(SHARE_TMP, "_q.sh")
    try:
        with open(abs_path, "r") as f:
            contents = f.read()
        assert contents == "#!/bin/sh\ndate '+%Y' \"label\"\n"
    finally:
        os.remove(abs_path)


def test_tool_call_supports_outer_wrappers_and_trailing_text():
    # The model typically wraps calls with <|tool_call_start|>[ ... ]<|tool_call_end|>
    # and may append commentary afterwards. The parser must only consume the
    # balanced parentheses for the tool call itself.
    wrapped = (
        '<|tool_call_start|>[write_file(file_path="./share/tmp/_w.txt", '
        'content="a\\nb")]<|tool_call_end|> and some trailing commentary '
        'that includes ) and even a fake "'
        "tool_call_start"
        '" marker.'
    )
    res_str = tool_call(wrapped)
    assert json.loads(res_str) == "SUCCESS"
    abs_path = os.path.join(SHARE_TMP, "_w.txt")
    try:
        with open(abs_path, "r") as f:
            assert f.read() == "a\nb"
    finally:
        os.remove(abs_path)


def test_tool_call_unknown_tool():
    res_str = tool_call('unknown_tool(arg="val")')
    res = json.loads(res_str)
    assert "error" in res
    assert "Unknown tool" in res["error"]


def test_tool_call_malformed():
    res_str = tool_call("not_a_tool_call")
    res = json.loads(res_str)
    assert "error" in res
    assert "Invalid tool call format" in res["error"]


# ---------------------------------------------------------------------------
# handle_wget
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, data: bytes):
        self._data = data

    def read(self):
        return self._data

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def test_handle_wget_success(monkeypatch):
    payload = b"fake zip bytes"

    def fake_urlopen(url, timeout=30):
        assert url == "https://example.com/file.zip"
        assert timeout == 30
        return _FakeResponse(payload)

    monkeypatch.setattr(tools_module, "urlopen", fake_urlopen)

    result = handle_wget(
        {"url": "https://example.com/file.zip", "file_name": "test-file.zip"}
    )

    assert result["status"] == "SUCCESS"
    assert result["file_path"] == "./share/tmp/test-file.zip"
    assert result["bytes"] == len(payload)

    abs_path = os.path.join(SHARE_TMP, "test-file.zip")
    with open(abs_path, "rb") as f:
        assert f.read() == payload
    os.remove(abs_path)


def test_handle_wget_subpath(monkeypatch):
    payload = b"sub"

    def fake_urlopen(url, timeout=30):
        return _FakeResponse(payload)

    monkeypatch.setattr(tools_module, "urlopen", fake_urlopen)

    result = handle_wget(
        {"url": "https://example.com/x.bin", "file_name": "nested/inner/x.bin"}
    )
    assert result["status"] == "SUCCESS"
    assert result["file_path"] == "./share/tmp/nested/inner/x.bin"

    abs_path = os.path.join(SHARE_TMP, "nested", "inner", "x.bin")
    assert os.path.isfile(abs_path)
    os.remove(abs_path)
    os.rmdir(os.path.dirname(abs_path))
    os.rmdir(os.path.dirname(os.path.dirname(abs_path)))


def test_handle_wget_rejects_traversal(monkeypatch):
    monkeypatch.setattr(tools_module, "urlopen", lambda *a, **k: _FakeResponse(b"x"))
    result = handle_wget({"url": "https://example.com/x", "file_name": "../escape.bin"})
    assert isinstance(result, dict)
    assert "error" in result


def test_tool_call_parsing_wget(monkeypatch):
    payload = b"hello"

    def fake_urlopen(url, timeout=30):
        assert url == "https://example.com/a.zip"
        return _FakeResponse(payload)

    monkeypatch.setattr(tools_module, "urlopen", fake_urlopen)

    res_str = tool_call('wget(url="https://example.com/a.zip", file_name="a.zip")')
    res = json.loads(res_str)
    assert res["status"] == "SUCCESS"
    assert res["file_path"] == "./share/tmp/a.zip"

    abs_path = os.path.join(SHARE_TMP, "a.zip")
    with open(abs_path, "rb") as f:
        assert f.read() == payload
    os.remove(abs_path)


# ---------------------------------------------------------------------------
# handle_clean_tmp
# ---------------------------------------------------------------------------


def test_handle_clean_tmp_single_file():
    a = os.path.join(SHARE_TMP, "_ct_a.txt")
    b = os.path.join(SHARE_TMP, "_ct_b.txt")
    with open(a, "w") as f:
        f.write("a")
    with open(b, "w") as f:
        f.write("b")
    try:
        res = handle_clean_tmp({"file_name": "_ct_a.txt"})
        assert res["status"] == "SUCCESS"
        assert "./share/tmp/_ct_a.txt" in res["removed"]
        assert not os.path.exists(a)
        assert os.path.exists(b)
    finally:
        if os.path.exists(a):
            os.remove(a)
        if os.path.exists(b):
            os.remove(b)


def test_handle_clean_tmp_rejects_traversal():
    res = handle_clean_tmp({"file_name": "../escape.txt"})
    assert isinstance(res, dict)
    assert "error" in res


def test_handle_clean_tmp_all_recursive():
    sub = os.path.join(SHARE_TMP, "_ct_sub")
    os.makedirs(sub, exist_ok=True)
    top = os.path.join(SHARE_TMP, "_ct_top.txt")
    nested = os.path.join(sub, "_ct_nested.txt")
    with open(top, "w") as f:
        f.write("t")
    with open(nested, "w") as f:
        f.write("n")
    try:
        res = handle_clean_tmp({})
        assert res["status"] == "SUCCESS"
        removed = set(res["removed"])
        assert "./share/tmp/_ct_top.txt" in removed
        assert "./share/tmp/_ct_sub/_ct_nested.txt" in removed
        assert not os.path.exists(top)
        assert not os.path.exists(nested)
        # Directory itself is preserved
        assert os.path.isdir(sub)
    finally:
        if os.path.exists(top):
            os.remove(top)
        if os.path.exists(nested):
            os.remove(nested)
        if os.path.isdir(sub):
            os.rmdir(sub)


# Silence unused-import warning for SANDBOX_ROOT when not referenced.
assert SANDBOX_ROOT
