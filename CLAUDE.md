## Rules

- Track the model, tokens and cost of every LLM call.
- Keep the READMEs up to date before every commit. A commit that changes behaviour, config, commands, tests or code line numbers must also update `README.md`, `docs/README.md` and `docs/README.html`. The two files in `docs/` must stay equal in content, headings and diagrams. Check the code-table line numbers and the test count after code edits. Keep the READMEs free of conflicts with each other and with the code, and write them so that a person can read them.
- Use subagents for work that has no dependencies between its parts. Send the independent subagent calls in one message so they run in parallel. Do the work yourself when one part needs the result of another, or when it takes only a few tool calls.
- The most important rule is the second one: keep the READMEs current before every commit.
