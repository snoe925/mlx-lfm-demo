from datetime import datetime

from mlx_lfm_demo.lfm_chat import LfmChat


def _chat_until_tool_call(chat, messages, expected_fragment, max_turns=3):
    current = list(messages)
    for _ in range(max_turns):
        current = chat.chat(current)
        last = current[-1]["content"]
        if expected_fragment in last:
            return current, last

        # The default system prompt often requires explicit user confirmation.
        if "confirm" in last.lower() or '"yes"' in last.lower():
            current.append({"role": "user", "content": "yes"})
            continue

        break

    return current, current[-1]["content"]


def test_lfm_chat_nearest_star():
    """LfmChat answers a simple factual question."""
    chat = LfmChat()
    messages = [{"role": "user", "content": "What is the star nearest to Earth?"}]

    updated_messages = chat.chat(messages)

    assert updated_messages[-1]["role"] == "assistant"
    assert "sun" in updated_messages[-1]["content"].lower()


def test_lfm_chat_write_a_date_script():
    """LfmChat emits a write_file tool call for a date script."""
    chat = LfmChat()
    messages = [
        {"role": "user", "content": "Write a script to call the linux date program."}
    ]

    updated_messages, last = _chat_until_tool_call(
        chat,
        messages,
        '<|tool_call_start|>[write_file(file_path="./tmp/',
    )

    assert updated_messages[-1]["role"] == "assistant"
    tool_call_1 = (
        '<|tool_call_start|>[write_file(file_path="./tmp/'  # script name can vary
    )
    tool_call_2 = 'content="#!/bin/bash\\ndate")]<|tool_call_end|>'
    assert tool_call_1 in last
    assert tool_call_2 in last


def test_lfm_chat_write_a_date_script_and_run():
    """LfmChat emits and executes a simple linux tool workflow."""
    chat = LfmChat()
    messages = [
        {
            "role": "user",
            "content": "Write a script to call the linux date program.  Run the script and tell me the result.",
        }
    ]

    updated_messages, last = _chat_until_tool_call(
        chat,
        messages,
        '<|tool_call_start|>[write_file(file_path="./tmp/',
    )

    assert updated_messages[-1]["role"] == "assistant"
    tool_call_1 = (
        '<|tool_call_start|>[write_file(file_path="./tmp/'  # script name can vary
    )
    tool_call_2 = 'content="#!/bin/bash\\ndate")]<|tool_call_end|>'
    assert tool_call_1 in last
    assert tool_call_2 in last
    tool_call_result = chat.execute_tool_call(last)
    assert "SUCCESS" in tool_call_result["content"]

    updated_messages.append(tool_call_result)

    after_run, last = _chat_until_tool_call(
        chat,
        updated_messages,
        "linux(script_file_name",
    )
    assert 'action="run"' in last
    assert "linux(script_file_name" in last
    tool_call_result = chat.execute_tool_call(last)
    assert str(datetime.now().year) in tool_call_result["content"]
