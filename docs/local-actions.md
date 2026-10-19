# Local actions: open a folder, open a brief

Chief can put things on the stage machine's screen: "open my demo folder", "what's in my downloads",
"show me the brief". Three tools do it, all in `chief/tools/local.py`, shared by mock and live mode.

| Tool | Class | What it does |
|---|---|---|
| `list_folder(path)` | read | names, kinds and sizes inside a folder. Never file contents. Hidden (dot) files are skipped, at most 200 entries. |
| `open_folder(path)` | safe write (UI only) | shows the folder in the file manager |
| `open_file(path)` | safe write (UI only) | opens one document in its default viewer |

They only ever **open**. Nothing deletes, moves, renames, writes or runs anything, and the opener is never
given text from the request: only the already-resolved absolute path. On Windows that is `os.startfile`; on
macOS `open` and on Linux `xdg-open`, called with an argument list and no shell.

In **mock** mode the same checks run, but nothing is launched: the call is recorded in `MockTools.calls` and the
resolved path in `MockTools.opened`. In **live** and **demo** mode the file manager really opens.

## The allow-list

A path is accepted only if, after resolving symlinks, junctions and `..`, it sits inside an allowed root.

Default roots (nothing to configure):

- the project's `demo/` folder. **Only this one.** Desktop, Documents and Downloads are deliberately NOT allowed by
  default: listing them would put your real file names on the projector. To allow your Desktop, create a dedicated
  folder (for example `Desktop\ChiefDemo`) and list that in `CHIEF_LOCAL_ROOTS` below instead of the whole Desktop.
- `<state dir>/briefs` (`.chief/briefs`), where Chief saves its own one-page briefs. It is always allowed, even if
  you replace the other roots, so "show me the brief" keeps working. The rest of `.chief` (memory, logs) is not.

Change it in `.env` (see `.env.example`). Separate roots with `;`:

```
CHIEF_LOCAL_ROOTS=C:\Users\me\Desktop\stage;D:\Slides
```

When it is set it **replaces** the defaults (apart from `.chief/briefs`). Each root is then also a spoken name for the
files skill: "open stage", "open slides". Two roots with the same folder name are ambiguous and Chief will ask
instead of guessing. A drive root (`C:\`, `/`) or your whole home folder is silently ignored as a root: add the
specific folders instead.

Note that the project folder itself sits inside Desktop on the demo machine, so its non-hidden file *names* are
listable. `open_file` still only opens the safe types below, and hidden files such as `.env` are never listed.

## What is refused

Everything is refused with a short message, and nothing is opened:

- a path outside every root, including `..\..` traversal, a sibling that shares a prefix (`C:\Users\me\Desktop-evil`),
  other drives, and symlinks or junctions inside a root that point outside it
- relative paths, `~`, empty or control-character paths
- UNC and device paths (`\\server\share`, `\\?\C:\...`, `\\.\...`), NTFS alternate data streams (`file.txt:evil.exe`),
  and names ending in a dot or space (Windows silently strips them)
- `open_file` on anything but: `pdf png jpg jpeg gif txt md csv html docx xlsx pptx`. Programs, scripts, shortcuts,
  installers, libraries and macro-enabled Office files (`exe bat cmd ps1 msi lnk js vbs py sh dll jar docm xlsm ...`)
  are on an explicit block-list as well, for a clearer message and as a second line. Both the name asked for and the
  file it really points to are checked, so `ok.pdf` that is a link to `real.exe` is refused.
- `open_file` on a folder or a missing file, `open_folder` on a file or a missing folder

Tests: `tests/test_local_tools.py` (allow-list, traversal, links, extensions, mock vs live, scoping),
`tests/test_files_skill.py`, `tests/test_briefs.py`.

## The skills

**files** ("open my desktop", "open the demo folder", "show me my documents", "what's in my downloads"). Name
resolution is plain string matching, no model: the spoken name must equal an allow-listed folder name
(`desktop`, `documents`, `downloads`, `demo`, `briefs`, or your own roots), optionally "`acme in my documents`" to
pick one sub-folder by exact name. Unknown, empty or ambiguous names are refused with the list of what can be opened.
"Open ..." opens the folder and answers with a one-line summary; "what's in ..." only lists. It declares just
`list_folder` and `open_folder`, proposes nothing, and cannot open a file.

**open_brief** ("show me the brief", "open the brief"). `meeting_prep` now also saves a one-page, print-ready HTML
brief (title, who, past threads, background, sources, date) to `.chief/briefs/brief-<meeting>.html` (`chief/briefs.py`),
and says "show me the brief". `open_brief` opens that page with `open_file`, building it first if prep has not run.
Email and web text is untrusted: everything is HTML-escaped, only `http(s)` URLs become links, and the page carries a
Content-Security-Policy that forbids scripts and any network request.

## Not verified on a real desktop

The launcher itself (`os.startfile` raising a real Explorer window, the default viewer for each type, window focus
on the stage monitor) is covered only with fakes. Try it once on the stage machine: `python -m chief web`, then say
"open my desktop" and "show me the brief" with `CHIEF_MODE=live` (mock mode records but opens nothing).
