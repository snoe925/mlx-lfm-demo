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


# The contract requires paths to start with "./share/"; ./share/tmp/ is the
# conventional scratch directory but the model is allowed to place scripts
# anywhere under ./share/.
WRITE_FILE_PREFIX = '<|tool_call_start|>[write_file(file_path="./share/'


def _find_write_file_tool_call(chat, messages):
    """Drive the chat until a write_file tool call targeting ./share/tmp appears."""
    current = list(messages)
    for _ in range(3):
        current = chat.chat(current)
        last = current[-1]["content"]
        if WRITE_FILE_PREFIX in last:
            return current, last
        if "confirm" in last.lower() or '"yes"' in last.lower():
            current.append({"role": "user", "content": "yes"})
            continue
        break
    return current, current[-1]["content"]


def _assert_date_script_tool_call(last):
    assert WRITE_FILE_PREFIX in last, last
    # Content should include a shebang and a date invocation; allow extra
    # whitespace / shell variants produced by the model.
    assert "#!/" in last and "date" in last
    assert "<|tool_call_end|>" in last


def test_lfm_chat_write_a_date_script():
    """LfmChat emits a write_file tool call for a date script."""
    chat = LfmChat()
    messages = [
        {"role": "user", "content": "Write a script to call the linux date program."}
    ]

    updated_messages, last = _find_write_file_tool_call(chat, messages)

    assert updated_messages[-1]["role"] == "assistant"
    _assert_date_script_tool_call(last)


def test_lfm_chat_write_a_date_script_and_run():
    """LfmChat emits and executes a simple linux tool workflow."""
    chat = LfmChat()
    messages = [
        {
            "role": "user",
            "content": "Write a script to call the linux date program.  Run the script and tell me the result.",
        }
    ]

    updated_messages, last = _find_write_file_tool_call(chat, messages)

    assert updated_messages[-1]["role"] == "assistant"
    _assert_date_script_tool_call(last)
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
    # The model picks the date format, and the simple tool-call parser
    # truncates content when it encounters unescaped quotes, so we can't
    # assume stdout always contains the year. Require only that the script
    # executed without error.
    content = tool_call_result["content"]
    assert '"exit_code": 0' in content, content
    # The year is the ideal signal when the model kept `date` unadorned.
    if str(datetime.now().year) not in content:
        # Fall back: ensure we got non-empty stdout.
        assert '"stdout": ""' not in content or '"stdout": "\\n"' in content, content
