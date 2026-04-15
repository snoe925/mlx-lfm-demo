from typing import List, Dict, Any, Optional

from mlx_lm import stream_generate

from .chat import Chat
from .tools import tool_call

TOOL_START_TOKEN = "<|tool_call_start|>"
TOOL_END_TOKEN = "<|tool_call_end|>"


class LfmChat(Chat):
    def __init__(self, model_name: Optional[str] = None):
        resolved_model_name = model_name or "LiquidAI/LFM2.5-1.2B-Instruct"
        super().__init__(model_name=resolved_model_name)
        self.tool_call_count = 0

    def chat(
        self, messages: List[Dict[str, Any]], canned_response: Optional[str] = None
    ) -> List[Dict[str, Any]]:
        if canned_response is not None:
            return [dict(m) for m in messages] + [
                {"role": "assistant", "content": canned_response}
            ]

        chat_messages = []
        if not messages or messages[0].get("role") != "system":
            chat_messages.append({"role": "system", "content": self.system_content})

        for m in messages:
            chat_messages.append(dict(m))

        prompt = self.tokenizer.apply_chat_template(
            chat_messages, tokenize=False, add_generation_prompt=True
        )

        assistant_response = ""

        for response in stream_generate(
            self.model, self.tokenizer, prompt, max_tokens=2048
        ):
            if not response.text:
                break

            assistant_response += response.text

        chat_messages.append({"role": "assistant", "content": assistant_response})

        return chat_messages

    def execute_tool_calls(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        updated_messages = [dict(m) for m in messages]
        i = 0
        while i < len(updated_messages):
            msg = updated_messages[i]
            if (
                msg["role"] == "assistant"
                and "(" in msg["content"]
                and ")" in msg["content"]
                and not msg.get("tool_executed")
            ):
                tool_call_str = msg["content"]
                result = tool_call(tool_call_str)
                msg["tool_executed"] = True
                updated_messages.append({"role": "tool", "content": result})
                return updated_messages
            i += 1
        return updated_messages

    def execute_all_tool_calls(
        self, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        current_messages = list(messages)
        while True:
            new_messages = self.execute_tool_calls(current_messages)
            if len(new_messages) == len(current_messages):
                break
            current_messages = new_messages
        return current_messages

    def execute_tool_call(self, content: str) -> Dict[str, Any]:
        """
        <|tool_call_start|>[write_file(file_path="./tmp/date_script.sh", content="#!/bin/bash\\ndate")]<|tool_call_end|>
        """
        return {"role": "tool", "content": tool_call(content)}
