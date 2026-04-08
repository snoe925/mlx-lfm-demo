# mlx-lfm-demo
LiquidAI LFM in MLX

# Unsafe tool calls
This program can make unsafe tool calls.  You probably want to run it in a container.

https://github.com/apple/container

Install with homebrew.
```
brew install container
```
And then run as follows. Note that the commands with "#" are in the container.
```
container run -it --rm --entrypoint=sh python
# curl -LsSf https://astral.sh/uv/install.sh | sh
# bash
bash# /root/.local/bin/uvx https://github.com/snoe925/mlx-lfm-demo.git
```

# Tools
The tools are:
read file, write file, list files and bash execution.

# Chat interactions
The chat reads stdin for multiline inputs.
Send the messages to the model with the go command.
```
/go
/clear
/quit
```

# LFMAGENT.md
Setup the chat context with this file.
This helps tell the model about the tools.
Customize this file to make the model achieve a specific goal.

