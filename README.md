# mlx-lfm-demo
LiquidAI LFM in MLX

# Unsafe tool calls
This program can make unsafe tool calls.  You probably want to run it in a container.

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

