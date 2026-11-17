# What HCWMA can do

One section per agent. Updated whenever an agent is added or changed (`add-agent` skill).

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
