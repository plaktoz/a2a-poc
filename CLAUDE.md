## Rules

- Change the tracking to model, token, and cost, with the default budget-exceeded action set to allow.
- Keep the READMEs up to date before every commit. A commit that changes behaviour, config, commands, tests or code line numbers must also update `README.md`, `docs/README.md` and `docs/README.html`. The two files in `docs/` must stay equal in content, headings and diagrams. Check the code-table line numbers and the test count after code edits.
- Use subagents for work that has no dependencies between its parts. Send the independent subagent calls in one message so they run in parallel. Do the work yourself when one part needs the result of another, or when it takes only a few tool calls.
- Two lasting rules here; the most important is keeping the README current before committing.
