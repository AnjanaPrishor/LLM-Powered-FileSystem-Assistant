"""
llm_file_assistant.py
---------------------
Part B: LLM Integration.

Wires the 4 tools defined in `fs_tools.py` into an LLM (OpenAI) so a user
can ask natural-language questions like:

    "Read all resumes in the sample_data folder"
    "Find resumes mentioning Python experience"
    "Create a summary file for resume.txt"

The LLM decides which tool(s) to call, we execute them locally, feed the
results back, and let the model produce a final answer.

Requirements
------------
    py -m pip install -r requirements.txt
    setx OPENAI_API_KEY "sk-..."   (Windows, new terminal after this)

Run
---
    py llm_file_assistant.py                          # interactive REPL
    py llm_file_assistant.py "list all pdf files in sample_data"
    py llm_file_assistant.py --dry-run                # no API calls, prints schemas
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

from fs_tools import read_file, list_files, write_file, search_in_file


# ---------------------------------------------------------------------------
# 1. Tool schemas (OpenAI "tools" format)
# ---------------------------------------------------------------------------

TOOLS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read the text content of a file. Supports .txt, .md, .pdf, "
                "and .docx. Returns the extracted text plus metadata "
                "(size, modified date, char/line counts)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Path to the file to read.",
                    },
                },
                "required": ["filepath"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": (
                "List files in a directory (non-recursive), optionally "
                "filtered by extension. Returns file name, path, size, and "
                "modified date for each file."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Directory to scan.",
                    },
                    "extension": {
                        "type": "string",
                        "description": (
                            "Optional extension filter, e.g. 'pdf' or '.pdf'. "
                            "Case-insensitive."
                        ),
                    },
                },
                "required": ["directory"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write text content to a file (UTF-8). Creates any missing "
                "parent directories automatically. Overwrites existing files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "Destination file path.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Text content to write.",
                    },
                },
                "required": ["filepath", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_in_file",
            "description": (
                "Case-insensitive substring search inside a file. Works on "
                ".txt, .md, .pdf, and .docx. Returns each match's line "
                "number, position, and surrounding context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "filepath": {
                        "type": "string",
                        "description": "File to search.",
                    },
                    "keyword": {
                        "type": "string",
                        "description": "Substring to search for.",
                    },
                },
                "required": ["filepath", "keyword"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# 2. Dispatcher: map tool name -> Python function
# ---------------------------------------------------------------------------

TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "read_file": read_file,
    "list_files": list_files,
    "write_file": write_file,
    "search_in_file": search_in_file,
}


def run_tool(name: str, arguments: dict) -> Any:
    """Execute one tool call locally and return its result."""
    fn = TOOL_FUNCTIONS.get(name)
    if fn is None:
        return {"success": False, "error": f"Unknown tool: {name}"}
    try:
        return fn(**arguments)
    except TypeError as exc:
        return {"success": False, "error": f"Bad arguments for {name}: {exc}"}
    except Exception as exc:
        return {"success": False, "error": f"{name} raised: {exc}"}


# ---------------------------------------------------------------------------
# 3. LLM agent loop
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """\
You are a helpful file-system assistant. You can read, list, write, and
search files by calling the provided tools. Rules:

- Always use tools to inspect the filesystem — never guess file contents.
- When the user asks about a folder, call list_files first to see what
  exists, then read the relevant files.
- When writing summaries, put them in a new file next to the source
  (e.g. "summary_<original>.txt") using write_file.
- Keep responses concise. When you cite text from a file, quote briefly.
- If a tool returns success=false, explain the error to the user rather
  than retrying blindly.
"""

MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
MAX_TOOL_ITERATIONS = 8


def _get_client():
    """Import + construct the OpenAI client lazily so --dry-run works
    without the package or API key installed."""
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "openai package is not installed. Run: py -m pip install openai"
        ) from exc

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY environment variable is not set.\n"
            "  Windows (persistent):  setx OPENAI_API_KEY \"sk-...\"\n"
            "  Windows (this shell):  $env:OPENAI_API_KEY = \"sk-...\""
        )
    return OpenAI(api_key=api_key)


def chat(user_message: str, history: list[dict] | None = None,
         verbose: bool = True) -> tuple[str, list[dict]]:
    """
    Send `user_message` to the LLM, handle any tool calls it makes, and
    return (final_assistant_text, updated_history).
    """
    client = _get_client()

    messages: list[dict] = list(history) if history else [
        {"role": "system", "content": SYSTEM_PROMPT}
    ]
    messages.append({"role": "user", "content": user_message})

    for step in range(MAX_TOOL_ITERATIONS):
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
        )
        msg = response.choices[0].message

        # Append the assistant message *as-is* — it may contain tool_calls
        # that the API needs to see referenced in following tool messages.
        messages.append({
            "role": "assistant",
            "content": msg.content,
            "tool_calls": [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in (msg.tool_calls or [])
            ] or None,
        })

        # No tool calls => we have our final answer.
        if not msg.tool_calls:
            return msg.content or "", messages

        # Execute each tool call and feed the result back.
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                result = {"success": False,
                          "error": f"Invalid JSON arguments: {exc}"}
            else:
                if verbose:
                    print(f"  -> tool: {name}({args})")
                result = run_tool(name, args)
                if verbose:
                    preview = json.dumps(result, default=str)[:160]
                    print(f"     result: {preview}"
                          + ("..." if len(preview) == 160 else ""))

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": name,
                "content": json.dumps(result, default=str),
            })

    return ("[stopped: reached MAX_TOOL_ITERATIONS "
            f"({MAX_TOOL_ITERATIONS}) without a final answer]"), messages


# ---------------------------------------------------------------------------
# 4. CLI / REPL
# ---------------------------------------------------------------------------

EXAMPLE_QUERIES = [
    "List all files in the sample_data folder",
    "Read all resumes in sample_data and tell me who has Python experience",
    "Find resumes mentioning Python",
    "Create a summary file for sample_data/resume.txt at output/summary_resume.txt",
]


def print_banner() -> None:
    print("=" * 60)
    print(" LLM-Powered FileSystem Assistant")
    print(f" model: {MODEL}")
    print("=" * 60)
    print("Example queries:")
    for q in EXAMPLE_QUERIES:
        print(f"  - {q}")
    print("Type 'exit' or Ctrl-C to quit.\n")


def dry_run() -> None:
    """Print tool schemas without contacting the LLM. Useful for
    verifying the setup on a machine without an API key."""
    print("=== TOOLS EXPOSED TO THE LLM ===")
    print(json.dumps(TOOLS, indent=2))
    print("\n=== DISPATCHER ===")
    for name, fn in TOOL_FUNCTIONS.items():
        print(f"  {name:15s} -> {fn.__module__}.{fn.__name__}")
    print("\nNo API calls made. Set OPENAI_API_KEY and re-run without "
          "--dry-run to chat.")


def main(argv: list[str]) -> int:
    if "--dry-run" in argv:
        dry_run()
        return 0

    # Single-shot mode: one query passed on the command line.
    if len(argv) > 1:
        query = " ".join(argv[1:])
        try:
            answer, _ = chat(query)
        except RuntimeError as exc:
            print(f"error: {exc}")
            return 1
        print("\n" + answer)
        return 0

    # Interactive REPL
    print_banner()
    history: list[dict] | None = None
    while True:
        try:
            user = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not user:
            continue
        if user.lower() in {"exit", "quit"}:
            return 0
        try:
            answer, history = chat(user, history=history)
        except RuntimeError as exc:
            print(f"error: {exc}")
            return 1
        print(f"\nassistant> {answer}\n")


if __name__ == "__main__":
    sys.exit(main(sys.argv))
