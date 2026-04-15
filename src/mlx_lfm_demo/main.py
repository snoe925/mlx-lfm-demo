import sys
import json
from .lfm_chat import LfmChat


def _print_new_messages(previous, current):
    for message in current[len(previous) :]:
        role = message.get("role")
        if role == "assistant":
            print(message.get("content", ""))
        elif role == "tool":
            print(f"[tool] {message.get('content', '')}")


def main():
    # Initialize chat instance
    chat = LfmChat()
    # Conversation history
    conversation = []

    print("Enter your messages. Type '/go' to process the accumulated messages.")
    print("Type '/clear' to clear conversation history.")
    print("Type '/context' to dump conversation context as JSON.")
    print("Type '/quit' to exit.")
    print("Type Ctrl+D (Unix) or Ctrl+Z (Windows) to exit.\n")

    try:
        while True:
            # Read a line from stdin
            line = sys.stdin.readline()
            if not line:  # EOF
                break
            line = line.rstrip("\n")

            if line == "/go":
                max_turns = 8
                for _ in range(max_turns):
                    previous_conversation = list(conversation)
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
