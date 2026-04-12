from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional
import json
from mlx_lm import load, stream_generate
from .tools import tool_call, TOOLS

TOOL_START_TOKEN = "<|tool_call_start|>"
TOOL_END_TOKEN = "<|tool_call_end|>"


class BaseChat(ABC):
    @abstractmethod
    def chat(
        self, messages: List[Dict[str, Any]], canned_response: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        """
        Sends a list of messages to the chat and returns the updated list of messages.
        """
        pass


class Chat(BaseChat):
    def __init__(self, model_name: Optional[str] = None):
        self.model_name = model_name or "LiquidAI/LFM2.5-1.2B-Instruct"
        self.model = None
        self.tokenizer = None
        self.tools = TOOLS
        self.system_content = ""

        if model_name is not None:
            res = load(self.model_name)
            self.model = res[0]
            self.tokenizer = res[1]
            self.system_content = self._build_system_content()

    def _build_system_content(self) -> str:
        system_content = "List of tools: " + json.dumps(self.tools)
        try:
            with open("LFMAGENT.md", "r") as f:
                system_md_content = f.read().strip()
                system_content = f"{system_md_content}\n\n{system_content}"
        except FileNotFoundError:
            pass
        return system_content

    def chat(
        self, messages: List[Dict[str, Any]], canned_response: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if canned_response is not None:
            return messages + [{"role": "assistant", "content": canned_response}]

        if not messages or messages[0].get("role") != "system":
            chat_messages = [{"role": "system", "content": self.system_content}]
            chat_messages.extend(messages)
        else:
            chat_messages = messages

        continue_chat = True
        while continue_chat:
            prompt = self.tokenizer.apply_chat_template(
                chat_messages, tokenize=False, add_generation_prompt=True
            )
            is_tool = False
            tool_call_str = ""
            assistant_response = ""

            for response in stream_generate(
                self.model, self.tokenizer, prompt, max_tokens=2048
            ):
                if not response.text:
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

            if is_tool:
                continue_chat = False
            elif tool_call_str:
                if assistant_response:
                    chat_messages.append(
                        {"role": "assistant", "content": assistant_response}
                    )

                result = tool_call(tool_call_str)
                chat_messages.append({"role": "tool", "content": result})
                continue_chat = True
            else:
                if assistant_response:
                    chat_messages.append(
                        {"role": "assistant", "content": assistant_response}
                    )
                continue_chat = False

        return chat_messages
