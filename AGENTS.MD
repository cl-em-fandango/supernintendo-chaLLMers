## Rules

IF YOU ARE ASKED TO EXECUTE A SINGLE TASK AND RETURN, DO IT AND RETURN. DO NOT USE YOUR INIATIVE, DO NOT GUESS, DO NOT INFER, EXECUTE THE SINGLE CARD ONLY AND RETURN

UNLESS EXPLICITLY STATED:
Never launch `pi` from inside this repo's session; never run the supervisor; never
`git reset --hard`; never write to `/home/donald/work/{queue,stats,logs}`. Tests use temp dirs, temp
git repos and fake `pi` scripts on `PATH` (patterns are in the T10, T12, T35, T36 verify blocks).
Verify blocks are written to be run by the *card-executing* agent, not by a card-writing agent.
Never start the sandbox container from inside a card session — not `scripts/run-sandbox.sh`,
`scripts/test-drive-sandbox.sh`, `scripts/rebuild-container.sh`, `integration_test.sh`, nor a direct
`podman`/`docker run`. The local model server hosts one model at a time, so a container's request
evicts the model serving the session that started it and both die. Verify in-process instead
(unit tests, temp dirs, fake executables on `PATH`); container/e2e runs are a human action, never
part of a card.

## Responding to requests

If you are asked a question, you answer it.
If you are asked directly to carry out a task, you do it without question. No exceptions. You do not think, you do not dwell, you answer the question.