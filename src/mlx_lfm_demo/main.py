import argparse
import sys
import json
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
        # After any output we flush so the terminal shows it even if
        # stdout is redirected or piped.
        print()  # newline
        # Some terminals need an explicit flush to break buffering.
        import sys

        sys.stdout.flush()


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
    args = parser.parse_args()

    if args.no_sandbox and args.sandbox:
        parser.error("--sandbox and --no-sandbox are mutually exclusive")

    tools.set_sandbox_enabled(not args.no_sandbox)

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

    # Initialize chat instance
    chat = LfmChat()
    # Conversation history
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
                max_turns = 8
                for _ in range(max_turns):
                    previous_conversation = list(conversation)
                    # Surface a clear "model running" marker before each
                    # model invocation so the user knows why the CLI is
                    # unresponsive (mlx_lm's stream_generate can take a
                    # noticeable amount of time to start producing output).
                    print("model", flush=True)
                    conversation = chat.chat(conversation)
                    _print_new_messages(previous_conversation, conversation)

                    previous_conversation = list(conversation)
                    conversation = chat.execute_tool_calls(conversation)
                    _print_new_messages(previous_conversation, conversation)

                    if len(previous_conversation) == len(conversation):
                        break
            elif line == "/clear":
                # Clear the conversation history
                conversation = []
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
                break
            else:
                # Add user message to conversation
                conversation.append({"role": "user", "content": line})
    except KeyboardInterrupt:
        print("\nExiting...")

    print("Chat session ended.")


if __name__ == "__main__":
    main()
