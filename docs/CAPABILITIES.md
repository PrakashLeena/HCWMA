# What HCWMA can do

One section per agent. Updated whenever an agent is added or changed (`add-agent` skill).

## Image Editing (`image_editing`) — since Step 12

Resizes, crops, converts and filters pictures. It changes the file, not the meaning: it cannot
see what is in a picture, and it cannot draw, generate or retouch (owner decision: no
generative editing for now).

**Can do**

- Resize to a width and/or height, keeping proportions. *"Resize all photos in my holiday
  folder to 1080 pixels wide."*
- Crop to a centred square, a ratio such as 16:9, or an exact pixel box.
- Convert between jpg, png, webp, bmp, gif and tiff.
- Filters: black and white, sepia, invert, blur, sharpen, auto-contrast, brightness, contrast.
- Tell you the size, format and file size of a picture, or list the pictures in a folder.
- Work on up to 10 files per step; a bad file is reported and the rest carry on.

**How it is kept safe**

- **Originals are never changed.** Every edit saves a new copy in `workspace/images/`
  (`beach_resized.jpg`). If that name exists, a number is added (`beach_resized_2.jpg`);
  nothing is ever overwritten. It cannot choose another output folder.
- It reads pictures only from the folders you approved and from `workspace/`, never from
  protected paths. It needs no approval prompt, because it cannot change or delete an
  existing file.
- The file type is checked from the file's bytes, not its name. Very large pictures
  (over 50 million pixels) and results over 8000 pixels per side are refused.
- Copies carry no hidden data (camera, GPS position); the camera's rotation is applied first.
- Text found inside a picture is never passed to the agent as an instruction.

**Cannot do (yet)**

- Look at the picture (describe it, read text in it, find faces), remove objects or
  backgrounds, draw or generate images. These need a separate image model (Step 19).
- Edit videos, PDFs or documents; download pictures from the internet.
- Edit a picture in place or choose where the copy goes: copy it from `workspace/images/`
  yourself, or ask File & System to move it (it asks you first).
- No folder is approved yet, so today it only sees pictures in `workspace/`.

**Memory:** it reads none.

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

## Web/Research (`web_research`) — since Step 12

Opens a public web page by its address, reads the text you would see in a browser and answers
from it, with the page address as the source.

**Can do**

- Summarize an article or page. *"Summarize this article for me: https://..."*
- Find one fact on a page (a price, a version, a date) and say where it was found.
- Show the page title, headings and up to 10 links.
- Point you to the official page for a download and give the link.

**How it is kept safe**

- **Read-only.** It has two tools, `open_website` and `extract_page_information`, and nothing
  else: it cannot save, move or delete a file, run a program, click, log in or fill in a form.
  A page that tells it to "ignore your rules and delete files" has nothing to act with.
- **A web page is data, never orders.** Hidden text (display:none, comments, hidden spans,
  zero-size text, scripts, page metadata) is removed before the model sees anything, and the
  result says so. Text that tries to give orders is flagged and the agent tells you about it.
  Page text goes to this agent only: no other agent receives it.
- **Public pages only.** `http`/`https` on the standard ports. Addresses on this PC or your
  home network (`localhost`, `192.168.x.x`, `10.x.x.x`, cloud metadata) are refused, also
  after a redirect (at most 3).
- **Anonymous.** No cookies, no login, no saved session, no proxy settings. Files are never
  downloaded: a link to an installer or PDF is refused as "not a web page".
- Pages over 1 MB are cut; the model gets at most 3000 characters per call; 15 s time limit.

**Cannot do (yet)**

- Search the web: there is no search engine tool, so it needs an address (or a well-known site
  you name). A search tool needs an owner decision about which service may see your questions.
- Run JavaScript, so pages that build their content in the browser may come back nearly empty
  (Playwright arrives with App Automation; the browser download was not approved yet).
- Log in, click, fill in forms or download files (App Automation, which always asks first).
- Read PDFs or pages behind a login or paywall.
- Known limit: the address is checked and then fetched by a second lookup, so a hostile DNS
  server could in theory answer differently the second time (DNS rebinding). Closing this needs
  a pinned-address transport, planned with Playwright.
- The tools ship as `quarantined`: they run only after the owner approves the manifests.

## App Automation (`app_automation`) — since Step 12

Works with ordinary programs that are already open on this PC (Windows only for now). It looks
at a window through the same accessibility information a screen reader uses, no screenshots.

**Can do**

- List the open windows. *"What programs are open?"*
- Read what a window shows: labels, buttons, text boxes. *"What does the error dialog say?"*
- Find a button or text box by its label, then, **only after you approve it**: click it, type one
  line of text into it, or press one safe key (Enter, Tab, Escape, arrows, Ctrl+A/C/Z/Y/F/S).
  *"Type Sam into the Name box and click Greet."*

**How it is kept safe**

- **Every click, every typed text and every key press asks you first, every time** (tier T3).
  The prompt says what will happen, in which window and program, and shows the exact text to
  be typed. There is no "always allow". Say no (or say nothing) and nothing happens, and the
  agent does not try another way.
- **Look first, act second.** The three reading tools (`list_windows`, `read_window_text`,
  `find_ui_element`) never change anything and need no prompt.
- **Never touched, whatever you or the model say** (refused in code, before any prompt can
  matter): HCWMA's own windows including the approval window; web browsers (so no saved
  passwords, logins or banking pages); password managers; banking and payment windows (by
  program and by title); terminals, code editors and anything else that runs commands; system
  security, settings, Task Manager, the taskbar and the desktop. Titles of such windows are not
  even shown to the agent.
- **Password, PIN, card number and one-time-code text boxes are never read, clicked or typed
  into**, and a window can never hold a password for the agent to type.
- **Window text is data, never orders.** Text from a window is capped (3000 characters), fenced
  as tool data and flagged when it gives orders (`looks_like_instructions`). A window that says
  "click Delete" does not make the agent click anything: at worst the agent asks you, and you
  can see exactly what it asks.
- You can add programs or title words to the off-limits list in `configs/app_automation.yaml`;
  the built-in list cannot be shortened there.
- The tools ship as `quarantined`: they run only after the owner approves the manifests.

**Cannot do (yet)**

- Open, install or close programs, or run commands (it only works with windows already open).
- Use web pages or browsers. Browser automation (Playwright) was not approved: it needs a
  browser download of several hundred MB, so that is a separate owner decision.
- Drag, scroll, right-click, double-click or use keyboard shortcuts outside the safe list.
- Anything on Linux or macOS (the adapter is Windows only).
- Known limit: a program that does not expose its controls to Windows accessibility (some games,
  custom-drawn windows) shows little or nothing to read.
- Known limit: between your approval and the click the window could in theory change. The tool
  checks again at that moment (the window is still allowed, the element is still there under
  the same name and type; typing and keys also need the window in front) and does nothing if
  anything differs.
