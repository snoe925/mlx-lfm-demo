import mlx_lfm_demo.lfm_chat as lfm_chat_module
from mlx_lfm_demo.lfm_chat import LfmChat


def test_execute_tool_calls_accepts_lfm_nonstandard_tool_call(monkeypatch):
    raw_tool_call = (
        '<|tool_call_start|>[write_file(file_path="./tmp/date_script.sh", '
        'content="#!/bin/bash\\ndate")]<|tool_call_end|>'
    )
    seen = {}

    def fake_tool_call(content):
        seen["content"] = content
        return '"SUCCESS"'

    monkeypatch.setattr(lfm_chat_module, "tool_call", fake_tool_call)

    chat = LfmChat.__new__(LfmChat)
    messages = [{"role": "assistant", "content": raw_tool_call}]

    updated = chat.execute_tool_calls(messages)

    assert seen["content"] == raw_tool_call
    assert updated[0]["tool_executed"] is True
    assert updated[-1] == {"role": "tool", "content": '"SUCCESS"'}


def test_execute_tool_call_passes_through_nonstandard_payload(monkeypatch):
    raw_tool_call = (
        '<|tool_call_start|>[linux(script_file_name="./tmp/date_script.sh", '
        'action="run")]<|tool_call_end|>'
    )

    def fake_tool_call(content):
        assert content == raw_tool_call
        return '{"exit_code":0}'

    monkeypatch.setattr(lfm_chat_module, "tool_call", fake_tool_call)

    chat = LfmChat.__new__(LfmChat)
    result = chat.execute_tool_call(raw_tool_call)

    assert result == {"role": "tool", "content": '{"exit_code":0}'}
