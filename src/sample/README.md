# Live gateway samples

Each script sends `Reply with exactly: OK` to one worker, using the endpoint, key and model from `llm.yaml`. A pass prints `exit=0` and `OK`.

## Prerequisites

- `uv sync`
- The environment variables `llm.yaml` names are set: `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_MODEL`, `OPENAI_BASE_URL`, `OPENAI_API_KEY`
- `codex` and `claude` are on your `PATH`

Run from the repo root.

## Run

```bash
# Codex worker, GPT
uv run python src/sample/codex_live.py

# Claude worker
uv run python src/sample/claude_live.py
```

The Claude run prints a warning that Claude Code does not recognize `claude-advantage-assist`. It is expected, and the call still works.

## Gemini

1. Set `enabled: true` on `codex-gemini` in `llm.yaml`.
2. Set `GEMINI_API_KEY` and `GOOGLE_GEMINI_BASE_URL`.
3. Run `uv run python src/sample/codex_live.py codex-gemini`.

Codex warns that it has no metadata for `gemini-3.6-flash` and uses fallback metadata.

## Notes

- `codex exec` hangs on an open stdin, so `codex_live.py` closes it.
- `claude_live.py` passes a clean environment. Inheriting a session's `CLAUDE_CODE_*` variables made the nested call hang.
- A non-zero exit prints the tail of stderr.
