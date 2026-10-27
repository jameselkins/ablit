# Claude Code coding smoke test

The [search fixture](fixtures/claude-code-search/) checks whether an endpoint can complete a small coding task through Claude Code, including file edits and test commands. It is a compatibility check, and it does not measure performance on a large codebase.

The fixture starts with six tests, of which four fail. The implementation mishandles case, punctuation, repeated words and ties. Keep a fresh copy for each model so one model never starts with another model's changes.

## Procedure

First, rent the selected catalog entry with an approved spending cap. Keep `ablit up` attached until the coding test ends, and use its printed Claude Code exports in a separate terminal. The tests on 2026-10-27 used Claude Code 2.1.311 and a separate `CLAUDE_CONFIG_DIR` for each model. All model aliases pointed to the rented endpoint, and `ANTHROPIC_API_KEY` was unset.

Use a fresh config directory and clear the hosted API key before starting Claude Code:

```sh
export CLAUDE_CONFIG_DIR="$(mktemp -d)"
unset ANTHROPIC_API_KEY
```

Second, copy the fixture to a temporary directory outside the ablit checkout and start Claude Code there. Use the following options for each prompt:

```sh
claude -p --bare --setting-sources '' --disable-slash-commands --no-chrome \
  --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
  --permission-mode dontAsk --restricted \
  --tools Read,Edit,Write,Bash,Glob,Grep \
  --allowedTools 'Read,Edit,Write,Glob,Grep,Bash(python3 -m unittest*)' \
  --max-turns 12 --verbose --output-format stream-json \
  --session-id <uuid> '<first prompt>'
```

The allowed commands permit the unit tests and refuse unrelated shell commands. A model may request a command that the test refuses. Record the refusal separately from endpoint errors.

Use this first prompt:

```text
Fix the search behavior described in README.md. Read the implementation and tests, make the code changes, add any missing regression coverage, and run python3 -m unittest -v. Keep the public API and keep existing test assertions intact.
```

Third, run the tests independently and check the cases below. Send the follow-up with `--resume <uuid>` in place of `--session-id <uuid>`:

```text
Now add Index.add(document) and Index.remove(document_id). Adding an existing ID replaces its text without duplicate results. remove returns True for an existing document and False for an unknown ID. Add tests for replacement, removal and search after each operation, and run the complete test suite. Keep the search behavior from the first task.
```

The automated runner allowed up to 15 minutes for each prompt. After each prompt, it ran `python3 -m unittest -v` independently and checked the cases below in a new Python process. A pass requires a successful Claude Code result, passing unit tests and passing independent checks.

Finally, run `ablit down <run>` until it exits 0 and confirms that billing has stopped. Keep the transcript, independent test output and run report locally. Publish only a summary without keys, IP addresses or host IDs.

## Independent checks

Run the first block after both prompts, from the directory containing the model's edited `text_index.py`:

```python
from text_index import Document, Index

x = Index([
    Document("z", "BLUE, blue fox!"),
    Document("a", "blue"),
    Document("b", "fox"),
])
assert x.search("BLUE blue fox?") == ["z", "a", "b"]
assert x.search("!!!") == []
assert x.search("missing") == []
```

After the second prompt, append the following checks to the first block and run both in the same Python process:

```python
x.add(Document("z", "owl"))
assert x.search("fox") == ["b"]
assert x.search("owl") == ["z"]
assert x.remove("z") is True
assert x.remove("z") is False
assert x.remove("unknown") is False
assert x.search("owl") == []
```
