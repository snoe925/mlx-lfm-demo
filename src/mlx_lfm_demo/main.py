import argparse
import os
import re
import sys
import json
import datetime
from .lfm_chat import LfmChat
from . import tools


def _print_new_messages(previous, current):
    for message in current[len(previous) :]:
        role = message.get("role")
        if role == "assistant":
            print(message.get("content", ""))
        elif role == "tool":
            print(f"[tool] {message.get('content', '')}")
        elif role == "user":
            print("user")


def _append_new_messages_jsonl(jsonl_path, previous_conversation, current_conversation):
    new_messages = current_conversation[len(previous_conversation):]
    if not new_messages:
        return
    with open(jsonl_path, "a") as f:
        for msg in new_messages:
            enriched = {**msg, "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat()}
            f.write(json.dumps(enriched, ensure_ascii=False) + "\n")


def _log_event(jsonl_path, event_type, **extra):
    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    event = {"role": "event", "event": event_type, "timestamp": timestamp, **extra}
    with open(jsonl_path, "a") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _rotate_jsonl_log(jsonl_path):
    """Rotate an existing JSONL log to ``<jsonl_path>.<N>``.

    The first rotation produces ``<jsonl_path>.0``, the second ``.1``, and so
    on. The next suffix is chosen by scanning the parent directory for files
    matching ``<basename>.<int>`` and taking ``max + 1``. Returns the new path
    on success, or ``None`` if there was nothing to rotate.
    """
    if not jsonl_path or not os.path.exists(jsonl_path):
        return None

    parent = os.path.dirname(jsonl_path) or "."
    base = os.path.basename(jsonl_path)
    pattern = re.compile(r"^" + re.escape(base) + r"\.(\d+)$")

    max_n = -1
    try:
        entries = os.listdir(parent)
    except OSError:
        entries = []
    for name in entries:
        match = pattern.match(name)
        if match is None:
            continue
        try:
            n = int(match.group(1))
        except ValueError:
            continue
        if n > max_n:
            max_n = n

    rotated = f"{jsonl_path}.{max_n + 1}"
    os.rename(jsonl_path, rotated)
    return rotated


def main():
    # The interactive CLI defaults to sandbox mode because the `linux` tool
    # can execute arbitrary shell commands; running inside QEMU is the
    # safer default. Unit tests and other library callers keep the
    # module-level default of OFF. Users can opt out at launch with
    # --no-sandbox (or toggle live with the /sandbox command).
    parser = argparse.ArgumentParser(
        prog="mlx-lfm-demo",
        description=(
            "Interactive LFM chat CLI. The `linux` tool runs scripts inside "
            "the QEMU sandbox by default; use /sandbox to toggle at runtime."
        ),
    )
    parser.add_argument(
        "--no-sandbox",
        action="store_true",
        help=(
            "Start with sandbox mode OFF so the linux tool runs on the host. "
            "By default the CLI starts with sandbox mode ON."
        ),
    )
    parser.add_argument(
        "--sandbox",
        action="store_true",
        help="Explicitly start with sandbox mode ON (the default).",
    )
    parser.add_argument(
        "--prompt",
        type=str,
        help="File containing the prompt to send. If '-', read from stdin.",
    )
    parser.add_argument(
        "--no-jsonl",
        action="store_true",
        help="Disable automatic JSONL chat logging (default: logs to chat_log.jsonl).",
    )
    parser.add_argument(
        "--jsonl-path",
        type=str,
        default="chat_log.jsonl",
        help="Path for the JSONL chat log (default: chat_log.jsonl).",
    )
    args = parser.parse_args()

    if args.no_sandbox and args.sandbox:
        parser.error("--sandbox and --no-sandbox are mutually exclusive")

    tools.set_sandbox_enabled(not args.no_sandbox)

    # Resolve JSONL logging path
    jsonl_path = None if args.no_jsonl else args.jsonl_path

    # If we are starting in sandbox mode, verify the environment before we
    # spend time loading the model. Missing qemu / missing kernel / missing
    # disk image would only be discovered on the first `linux` tool call,
    # which is a frustrating failure mode.
    if tools.is_sandbox_enabled():
        from sandbox import check_sandbox_preflight

        preflight = check_sandbox_preflight()
        if preflight["ok"]:
            version_line = preflight["qemu_version"] or "version unknown"
            print(f"Sandbox preflight OK: {preflight['qemu_path']} ({version_line})")
        else:
            print("Sandbox mode is ON but the environment is not ready:")
            for err in preflight["errors"]:
                print(f"  - {err}")
            print(
                "Re-run with --no-sandbox to skip QEMU, or fix the issues above.",
                file=sys.stderr,
            )
            sys.exit(1)

    # Rotate any existing JSONL log to chat_log.jsonl.N before opening a fresh
    # one for this session. Done after preflight so we don't churn the log
    # directory when the CLI is about to exit with a sandbox error.
    if jsonl_path:
        rotated = _rotate_jsonl_log(jsonl_path)
        if rotated:
            print(f"Rotated previous chat log to {rotated}")
        _log_event(jsonl_path, "session_start")

    # Initialize the chat instance once and reuse it across prompt-mode and
    # interactive mode. LfmChat() loads model weights which is the slowest
    # step in startup; constructing it twice (once just to grab system_content
    # for logging) would double that cost.
    chat = LfmChat()

    if jsonl_path:
        _log_event(jsonl_path, "system_prompt", content=chat.system_content)

    # Handle prompt argument if provided
    if args.prompt is not None:
        if args.prompt == "-":
            # Read from stdin
            prompt_content = sys.stdin.read()
        else:
            # Read from file
            try:
                with open(args.prompt, "r") as f:
                    prompt_content = f.read()
            except FileNotFoundError:
                print(f"Error: Prompt file '{args.prompt}' not found.", file=sys.stderr)
                sys.exit(1)
            except Exception as e:
                print(f"Error reading prompt file '{args.prompt}': {e}", file=sys.stderr)
                sys.exit(1)

        # Conversation history with the prompt as the first user message
        conversation = [{"role": "user", "content": prompt_content}]

        # Process the chat loop
        max_turns = 20
        for _ in range(max_turns):
            previous_conversation = list(conversation)
            # Surface a clear "model running" marker before each
            # model invocation so the user knows why the CLI is
            # unresponsive (mlx_lm's stream_generate can take a
            # noticeable amount of time to start producing output).
            print("model", flush=True)
            conversation = chat.chat(conversation)
            _print_new_messages(previous_conversation, conversation)
            if jsonl_path:
                _append_new_messages_jsonl(jsonl_path, previous_conversation, conversation)

            previous_conversation = list(conversation)
            conversation = chat.execute_tool_calls(conversation)
            _print_new_messages(previous_conversation, conversation)
            if jsonl_path:
                _append_new_messages_jsonl(jsonl_path, previous_conversation, conversation)

            if len(previous_conversation) == len(conversation):
                print("user", flush=True)
                break

        # Exit after processing
        sys.exit(0)

    # Initialize conversation history for interactive mode
    conversation = []

    print(
        "Enter your messages. Type '/go' or press Enter twice to process the accumulated messages."
    )
    print("Type '/clear' to clear conversation history.")
    print("Type '/context' to dump conversation context as JSON.")
    print(
        "Type '/sandbox' to toggle running the linux tool inside the QEMU sandbox "
        "(currently " + ("ON" if tools.is_sandbox_enabled() else "OFF") + ")."
    )
    print("Type '/quit' to exit.")
    print("Type Ctrl+D (Unix) or Ctrl+Z (Windows) to exit.\n")

    previous_blank = False
    try:
        while True:
            # Read a line from stdin
            line = sys.stdin.readline()
            if not line:  # EOF
                break
            line = line.rstrip("\n")

            # Two consecutive blank lines act as /go
            if line == "":
                if previous_blank:
                    line = "/go"
                    previous_blank = False
                else:
                    previous_blank = True
                    continue
            else:
                previous_blank = False

            if line == "/go":
                max_turns = 20
                for _ in range(max_turns):
                    previous_conversation = list(conversation)
                    # Surface a clear "model running" marker before each
                    # model invocation so the user knows why the CLI is
                    # unresponsive (mlx_lm's stream_generate can take a
                    # noticeable amount of time to start producing output).
                    print("model", flush=True)
                    conversation = chat.chat(conversation)
                    _print_new_messages(previous_conversation, conversation)
                    if jsonl_path:
                        _append_new_messages_jsonl(jsonl_path, previous_conversation, conversation)

                    previous_conversation = list(conversation)
                    conversation = chat.execute_tool_calls(conversation)
                    _print_new_messages(previous_conversation, conversation)
                    if jsonl_path:
                        _append_new_messages_jsonl(jsonl_path, previous_conversation, conversation)

                    if len(previous_conversation) == len(conversation):
                        print("user", flush=True)
                        break
            elif line == "/clear":
                # Clear the conversation history
                conversation = []
                if jsonl_path:
                    _log_event(jsonl_path, "clear")
                print("Conversation history cleared.")
            elif line == "/context":
                # Dump conversation context as JSON
                print(json.dumps(conversation, indent=2))
            elif line == "/sandbox" or line.startswith("/sandbox "):
                # Toggle, or explicitly set, whether the `linux` tool runs
                # scripts inside the QEMU sandbox. By default the linux tool
                # runs scripts directly on the host; turning on /sandbox
                # routes execution through QEMU for isolation at the cost of
                # substantial latency.
                arg = line[len("/sandbox") :].strip().lower()
                if arg in ("", "toggle"):
                    new_state = not tools.is_sandbox_enabled()
                elif arg in ("on", "enable", "true", "1", "yes"):
                    new_state = True
                elif arg in ("off", "disable", "false", "0", "no"):
                    new_state = False
                elif arg in ("status",):
                    new_state = tools.is_sandbox_enabled()
                else:
                    print(
                        f"Unknown /sandbox argument: {arg!r}. Use on/off/toggle/status."
                    )
                    continue
                tools.set_sandbox_enabled(new_state)
                print(
                    "Sandbox mode: "
                    + (
                        "ON (linux tool runs via QEMU)"
                        if new_state
                        else "OFF (linux tool runs on host)"
                    )
                )
            elif line == "/quit":
                # Exit the program
                if jsonl_path:
                    _log_event(jsonl_path, "session_end")
                break
            else:
                # Add user message to conversation
                conversation.append({"role": "user", "content": line})
                if jsonl_path:
                    _append_new_messages_jsonl(jsonl_path, conversation[:-1], conversation)

    except KeyboardInterrupt:
        print("\nExiting...")

    print("Chat session ended.")


if __name__ == "__main__":
    main()
