import sys
from .chat import Chat

def main():
    # Initialize chat instance
    chat = Chat()
    # Conversation history
    conversation = []
    
    print("Enter your messages. Type '/go' to process the accumulated messages.")
    print("Type '/clear' to clear conversation history.")
    print("Type '/quit' to exit.")
    print("Type Ctrl+D (Unix) or Ctrl+Z (Windows) to exit.\n")
    
    try:
        while True:
            # Read a line from stdin
            line = sys.stdin.readline()
            if not line:  # EOF
                break
            line = line.rstrip('\n')
            
            if line == "/go":
                # Process the accumulated messages
                conversation = chat.run_chat(conversation)
                # After processing, the conversation already includes the assistant's response
                # Continue to next input
            elif line == "/clear":
                # Clear the conversation history
                conversation = []
                print("Conversation history cleared.")
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