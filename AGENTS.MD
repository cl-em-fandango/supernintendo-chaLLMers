## Rules

Never launch `pi` from inside this repo's session; never run the supervisor; never
`git reset --hard`; never write to `/home/donald/work/{queue,stats,logs}`. Tests use temp dirs, temp
git repos and fake `pi` scripts on `PATH` (patterns are in the T10, T12, T35, T36 verify blocks).
Verify blocks are written to be run by the *card-executing* agent, not by a card-writing agent.

## Responding to requests

If you are asked a question, you answer it.
If you are asked directly to carry out a task, you do it without question. No exceptions. You do not think, you do not dwell, you answer the question.