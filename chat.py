import json
from mlx_lm import load, generate, stream_generate
from tools import tool_call, TOOLS

TOOL_START_TOKEN = "<|tool_call_start|>"
TOOL_END_TOKEN = "<|tool_call_end|>"

class Chat:
    """
    A class to handle chat interactions with the LFM2.5 model and tools.
    
    This class encapsulates all the setup and logic needed for chatting
    with the model, including model loading, tool handling, and message processing.
    """
    
    def __init__(self, model_name="LiquidAI/LFM2.5-1.2B-Instruct"):
        """
        Initialize the Chat instance with model loading.
        
        Args:
            model_name (str): The name/path of the model to load
        """
        self.model_name = model_name
        self.model, self.tokenizer = load(model_name)
        self.tools = TOOLS
        self.system_content = self._build_system_content()
        
    def _build_system_content(self):
        """
        Build the system content by combining LFMAGENT.md (if exists) with tool list.
        
        Returns:
            str: The system content for the chat
        """
        system_content = "List of tools: " + json.dumps(self.tools)
        try:
            with open("LFMAGENT.md", "r") as f:
                system_md_content = f.read().strip()
                system_content = f"{system_md_content}\n\n{system_content}"
        except FileNotFoundError:
            pass  # Use default system prompt if AGENT.md doesn't exist
        return system_content
    
    def run_chat(self, messages):
        """
        Run a chat session with the given messages.
        
        Args:
            messages (list[dict]): List of message dictionaries with 'role' and 'content'
            
        Returns:
            list[dict]: Updated messages list including assistant responses
        """
        # Initialize messages with system message if not present
        if not messages or messages[0].get("role") != "system":
            chat_messages = [{"role": "system", "content": self.system_content}]
            chat_messages.extend(messages)
        else:
            chat_messages = messages
            
        continue_chat = True
        while continue_chat:
            prompt = self.tokenizer.apply_chat_template(
                chat_messages, 
                tokenize=False, 
                add_generation_prompt=True
            )
            is_tool = False
            tool_call_str = ''
            assistant_response = ''
            
            for response in stream_generate(
                self.model, 
                self.tokenizer, 
                prompt, 
                max_tokens=2048
            ):
                if not response.text:
                    continue_chat = False
                    break
                    
                if is_tool:
                    if response.text == TOOL_END_TOKEN:
                        is_tool = False
                    else:
                        tool_call_str += response.text
                else:
                    if response.text == TOOL_START_TOKEN:
                        is_tool = True
                    else:
                        assistant_response += response.text
                        
            if not is_tool and tool_call_str:
                print('*** Make tool call ***', tool_call_str, flush=True)
                result = tool_call(tool_call_str)
                chat_messages.append({"role": "tool", "content": result})
                tool_call_str = ''
                # If we call a tool, continue the chat with the results
                continue_chat = True
                
            print(assistant_response, flush=True)
            chat_messages.append({"role": "assistant", "content": assistant_response})
            
        return chat_messages
