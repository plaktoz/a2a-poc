# Model prices

The factory reads this file to price the LLM calls whose provider reports no cost. Codex is the one today (FR-30).
Claude reports its own cost. Its rows here are for reference and for a future fallback.

Prices are USD per 1,000,000 tokens. The source is https://www.llm-prices.com/ (feed `current-v1.json`).
Each row uses the lower tier of the feed (the prompt up to 272k tokens for GPT). A larger prompt costs more than this table says.

Refresh the table once a week with the skill `update-model-prices`, or run `uv run python -m factory.prices`.
Do not edit the table by hand. Edit the aliases and the feed ids in the JSON block, then refresh.

## Table

<!-- prices:start -->
| Model | Input | Cached input | Output | Feed id or alias |
|---|---:|---:|---:|---|
| `gpt-6.1-sol` | 2 | 0.1 | 10 | `gpt-6.1-sol` |
| `gpt-6-luna` | 0.1 | 0.01 | 0.5 | `gpt-6-luna` |
| `gemini-3.7-flash` | 0.75 | 0.075 | 3.75 | `gemini-3.7-flash` |
| `claude-opus-5-5` | 4 | 0.2 | 20 | `claude-opus-5-5` |
| `gpt-advantage-assist` | | | | alias of `gpt-6.1-sol` |
| `claude-advantage-assist` | | | | alias of `claude-opus-5-5` |
<!-- prices:end -->

## Aliases

An alias is a model name that the gateway serves and the feed does not list. The owner chose these.

| Alias | Priced as | Decided |
|---|---|---|
| `gpt-advantage-assist` | `gpt-6.1-sol` | 2026-10-31 |
| `claude-advantage-assist` | `claude-opus-5-5` | 2026-10-31 |

## Data

The engine reads this block. `feed_id` names the feed row when it differs from the model name. `alias_of` points to another row.

```json prices
{
  "models": {
    "gpt-6.1-sol": {
      "input": 2.0,
      "cached_input": 0.1,
      "output": 10.0
    },
    "gpt-6-luna": {
      "input": 0.1,
      "cached_input": 0.01,
      "output": 0.5
    },
    "gemini-3.7-flash": {
      "input": 0.75,
      "cached_input": 0.075,
      "output": 3.75
    },
    "claude-opus-5-5": {
      "input": 4,
      "cached_input": 0.2,
      "output": 20
    },
    "gpt-advantage-assist": {
      "alias_of": "gpt-6.1-sol"
    },
    "claude-advantage-assist": {
      "alias_of": "claude-opus-5-5"
    }
  },
  "source": "https://www.llm-prices.com/current-v1.json",
  "feed_updated_at": "2026-10-26",
  "fetched_on": "2026-10-31"
}
```
