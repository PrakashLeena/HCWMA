# What HCWMA can do

One section per agent. Updated whenever an agent is added or changed (`add-agent` skill).

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
- Restore, move, rename or delete files. Restoring from the Recycle Bin arrives with the
  approval workflow in Step 9.
- Search folders you have not approved, other users' Recycle Bins, or cloud storage that
  is not synced into an approved folder.
- Read the Recycle Bin on Linux or macOS.

## Echo test agent (`echo`) — since Step 7

A test agent. Repeats text back and tells the current date and time on this PC.
