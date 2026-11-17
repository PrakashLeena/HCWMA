# What HCWMA can do

One section per agent. Updated whenever an agent is added or changed (`add-agent` skill).

## Coding (`coding`) and Debugging (`debugging`) agents — since Step 11

Small Python jobs inside one sandbox folder, `workspace/code/` (inside the HCWMA repository,
not committed). Paths you give are relative to it. **Be realistic:** a 4-billion-parameter
model on a CPU is far weaker at coding than Claude. Expect small scripts, simple tests and
one-line bug fixes; expect it to be slow (minutes) and sometimes wrong.

**Coding can do**

- Write a small Python program or function, with a pytest file, and run it.
  *"Write a program in wordstats that counts words, with a test, and run it."*
- Make a small edit to a file in the workspace; explain a piece of code.

**Debugging can do**

- Run the tests (or the program) to reproduce a failure, read the traceback and logs,
  make the smallest fix with an exact-text patch and run the tests again.
  *"The tests in shop fail. Find the bug in cart.py and fix it."*

**How it is kept safe**

- Files can be read and written only inside `workspace/code/`. A path that climbs out of it
  (`..`, absolute paths, symlinks, junctions) is refused. Executable file types (`.exe`,
  `.bat`, `.ps1` ...) cannot be written. Nothing is ever deleted.
- Code runs only as `python script.py` or `python -m pytest`, never through a shell, in a
  fresh process with no API keys or user profile, a time limit (default 30 s), a memory cap
  and an output cap. Every run is logged in `workspace/code/.hcwma/logs/`.
- While it runs, a guard blocks writing outside `workspace/code/`, reading your other files,
  the network, and starting other programs.
- **Honest limit:** this guard runs inside Python. It stops careless or wrong code, which is
  what a small model writes, but it is not a container: deliberately hostile code could get
  round it. The agents never run anything you did not ask for, and only these two agents can.
- Every tool call is in the audit log. These agents never ask for approval because nothing
  they can do leaves the coding workspace.

**Cannot do (yet)**

- Work on code outside `workspace/code/` (your own projects elsewhere), install packages,
  use the internet or git, or run languages other than Python.
- Run programs that wait for keyboard input.
- Move finished code out of the workspace: copy it yourself, or ask File & System (it asks
  you first).

**Each agent reads only its own memory scopes:** Coding sees your profile; Debugging none.

## Memory Librarian (`memory_librarian`) — since Step 10

Remembers what you ask it to, shows what HCWMA remembers, and forgets on request. Memory
lives on this PC only (`%LOCALAPPDATA%\HCWMA`), never in the cloud.

**Can do**

- Remember a fact or preference you state. *"Remember that my sister's birthday is on 12 March."*
  Any HCWMA chat later — also after a restart — can answer from it.
- Show what it remembers. *"What do you remember about me?"*
- Forget an entry. *"Forget my sister's birthday."* It shows you the entries and asks
  before deleting; deletion is real (also from summaries made from it and from memory backups).
- Forget everything, after a one-time approval (T4).
- Refresh the PC file index.
- Without asking: HCWMA keeps a one-line note of each finished task (1 year) and a
  one-sentence summary of each chat (90 days), made from your messages only.

**Never**

- Stores passwords, keys, tokens, card, bank account or ID numbers (they are cut out).
- Stores text from files, web pages or tool results as a fact about you.
- Stores a line that tries to give HCWMA orders ("always approve ..."). Memory can never
  change a permission or an approval.

**You can also, at the keyboard:** `python -m memory show | export <folder> | backup |
restore <file> | forget <id> | cleanup`.

**Each agent reads only its own memory scopes:** the Router sees your profile and chat
summaries; File & System sees your profile and past task outcomes; the echo test agent sees none.

## File & System agent (`file_system`) — since Step 8

Looks for files and reports on disk space. It only looks; it changes nothing.

**Can do**

- Find a file by words in its name or folder, by type (extension) or by date.
  *"I'm missing one of my project files, it was called annek-proposal."*
- List the files you changed recently. *"Show me the files I changed today."*
- Check whether a deleted file is still in the Recycle Bin (Windows).
- Report free and used space per drive, and the largest files and sub-folders in the
  folders it may search.
- Say honestly when it cannot find something, and suggest what to try next.

**Where it looks**

- Only in the folders listed under `approved_folders` in `configs/permissions.yaml`, plus
  the HCWMA `workspace/` folder. No folder is approved yet (owner decision 2026-11-16), so
  today it only sees `workspace/`.
- Never in protected paths (credential stores, `~/.ssh`, browser profiles, OS system
  folders, HCWMA's own `security/` folder), even inside an approved folder.
- The Recycle Bin check only shows files that were deleted from those same folders.

**Cannot do (yet)**

- Read, summarize or search the text inside files. The index holds names, dates and sizes
  only (owner decision 2026-11-17).
- Search folders you have not approved, other users' Recycle Bins, or cloud storage that
  is not synced into an approved folder.
- Read the Recycle Bin on Linux or macOS.

## Echo test agent (`echo`) — since Step 7

A test agent. Repeats text back and tells the current date and time on this PC.
