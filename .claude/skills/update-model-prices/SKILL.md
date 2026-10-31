---
name: update-model-prices
description: Refresh the model price table in docs/model-prices.md from the public feed at llm-prices.com, check that every Codex model in llm.yaml is priced, and report what changed. Use weekly, or when a model is added to llm.yaml, or when factory check or a run says a call has no price.
---

# Update model prices

The factory prices the Codex calls from `docs/model-prices.md` (FR-30). Claude reports its own cost and does not use this table. The table is refreshed from the public feed https://www.llm-prices.com/current-v1.json. The numbers come from the feed's JSON, never from a summary of a web page.

Run this skill once a week. Run it sooner when `llm.yaml` gets a new model.

## Steps

1. Run the refresh in check mode. Read the output. It writes nothing.

   ```bash
   .venv/bin/python -m factory.prices --check
   ```

   It prints each price that would change, and `no change` or `the file would change`. If it prints `error:`, stop and report the error. Do not edit the file by hand to get past it.

2. Run the refresh for real.

   ```bash
   .venv/bin/python -m factory.prices
   ```

   It rewrites the table between `<!-- prices:start -->` and `<!-- prices:end -->` and the JSON block in `docs/model-prices.md`. The prose stays as it is. It stamps the feed date (`feed_updated_at`) and today's date (`fetched_on`).

3. Check that every Codex model is priced. Read the models of the enabled Codex workers in `llm.yaml`. The test `test_the_shipped_models_in_llm_yaml_that_codex_runs_all_have_a_price` does this check. Run it:

   ```bash
   .venv/bin/pytest -q tests/test_prices.py
   ```

   If it fails, the test names the models to add. Go to step 4.

4. For a model that the feed does not list, add an entry by hand to the JSON block. Use `"alias_of"` when the gateway serves a model that the feed does not list, and the owner says which model it equals. Use `"feed_id"` when the gateway name differs from the feed name. Record the alias in the Aliases table in the file, with the date and the owner's decision. Never invent a price. If the owner has not said which model it equals, ask. Do not guess.

5. Verify.

   ```bash
   .venv/bin/pytest -q
   .venv/bin/factory check --worker codex-gpt
   ```

   The suite must pass. The check output must show a cost greater than $0 for the Codex models and must not say `calls have no price`.

6. Report to the owner in this form:
   - The feed date (`feed_updated_at`) and today's date.
   - Each model whose price changed, with the old and new input and output price.
   - Any model with no price, and what you did about it.
   - The real output of the suite run.

7. Commit only when the owner asks. Update `docs/tasks.md` if the refresh changes a task's state. The commit message names the feed date.

## Rules

- The feed gives USD per 1,000,000 tokens. The table uses the same unit. Do not convert.
- The table lists the lower price tier only (GPT prompts up to 272k tokens). A larger prompt costs more than the table says. Say so in the report when a run used a large prompt.
- A failed refresh writes nothing. The module refuses a feed that lacks a priced model. Do not force it through.
- Do not change the routing in `llm.yaml` or `agents.yaml` to fit the prices. The skill changes prices only.
- The owner decided the aliases. `gpt-advantage-assist` is `gpt-6.1-sol`. `claude-advantage-assist` is `claude-opus-5-5`. Do not change them without the owner.

## If something fails

| Symptom | Cause | Action |
|---|---|---|
| `error: cannot read ... (URL)` or a timeout | The feed is down or there is no network. | Report it. The table keeps its old prices, which still price calls. Try again later. |
| `not in the feed, so the table was not changed: ...` | A model in the table has left the feed. | Add a `feed_id` for it, or an `alias_of`, after the owner decides. |
| `the prices block is not valid JSON` | Someone edited the JSON block by hand. | Restore the block from git (`git show HEAD:docs/model-prices.md`) and rerun. |
| `has no ```json prices block` or `needs the markers` | The file lost its markers. | Restore the file from git. Do not rebuild it from memory. |
