# The filesystem

A POSIX-shaped filesystem in memory, saved to `localStorage`. Nested directories, symlinks,
hard links, permission bits, inodes, and the errors a real one returns.

```js
const { openFileSystem } = await import('../../shared/fs/fs.mjs');   // inside mount()
const fs = await openFileSystem();

fs.writeFile('/home/user/hello.txt', 'hi\n');
fs.readFile('~/hello.txt');          // no — tilde is the shell's job, not the kernel's
fs.list('/home/user');               // [{name, type, mode, size, mtime, nlink, target?}]
```

| Path | Subject |
| --- | --- |
| `vfs.mjs` | The filesystem: inodes, resolution, the operations, serialisation. Imports nothing. |
| `seed.mjs` | The tree a fresh filesystem starts with, as calls against the API. |
| `fs.mjs` | The one mount, and the only place that writes to `localStorage`. |

## Why there is one mount

`openFileSystem()` memoises a promise at module scope. Two apps that open "the filesystem"
have to get the same object, or a save in Notepad lands in a tree the terminal cannot see.
ES modules dedupe by resolved URL, so every importer of `fs.mjs` shares one mount however
many windows ask for it.

That is the whole mechanism behind `open readme.txt` in the terminal: no message passing, no
refresh, no event. Both windows already hold the same filesystem.

**Import it inside `mount`, never at module scope.** Every `app.mjs` is imported at startup
to build the Start menu, and a module-scope import would make every reader pay for the
engine, the seed tree and a `localStorage` read whether or not they open a file.
`docs/apps.md` has the general rule.

## The model

A directory holds names. A name holds an inode number. The inode holds the data.

That indirection is the only reason the three link-ish things are different:

- **A hard link** is a second name for one inode. `ln a b` then `echo x >> a` changes `b`,
  and `rm a` leaves `b` holding the file. `nlink` counts the names.
- **A symlink** is an inode whose data is a path. It is resolved every time it is used, and
  it can point at nothing — `links/broken` in the seed tree does.
- **A copy** is a different inode with the same contents, and nothing joins them afterwards.

`/home/user/links/` in the seed tree has one of each, including the two that fail: a dangling
link and a link to itself. `ls -l links/` is the evidence.

### Files hold text

A JS string is the contents, `size` is a character count, and there is no binary mode.
Everything above this layer is a text terminal and a text editor, so bytes would be a
conversion at every boundary and a fidelity nobody reads.

### Permissions are enforced, not displayed

There is one identity — `uid 1000`, `gid 1000` — and the mode bits are checked on every
call: read to read a file, write to write one, **execute on a directory to look inside it**.
`/etc/secret.conf` ships at mode `000`, and `cat` on it says `Permission denied` rather than
printing nothing.

Execute on a directory is the bit worth knowing: it is the right to *enter*, where read is
the right to *list*. A `--x` directory is a place you can stand and cannot enumerate, which
is why `fs.access(path, PERM.X)` exists — probing with a read would refuse it wrongly.

### Logical and physical paths disagree, on purpose

`normalize()` collapses `..` lexically, against the string. Resolution follows `..` to the
real parent of whatever the previous name pointed at. They differ exactly when a symlink is
involved, and that difference is `pwd` versus `pwd -P`:

```
cd links/relative-dir     # a symlink to /home/user/notes
pwd                       # /home/user/links/relative-dir   — the path you walked
pwd -P                    # /home/user/notes                — where you are
cd ..                     # /home/user/links                — lexical, like bash -L
```

The shell keeps the logical path, as `bash` does by default. `realpath()` is the physical
answer, resolved component by component, because a link in the middle of a path changes what
every `..` after it means.

## Errors

Every refusal is an `FsError` with a POSIX `code` and a `path: reason` message, so a command
prints `` `${name}: ${error.message}` `` and gets the line coreutils would print.

| Code | When |
| --- | --- |
| `ENOENT` | The name is not there. A dangling symlink resolves to this. |
| `ENOTDIR` | A path component is a file. |
| `EISDIR` | A file operation on a directory. |
| `EEXIST` | A create over something that exists. |
| `ENOTEMPTY` | `rmdir` on a directory with contents. |
| `ELOOP` | More than 40 symlink hops in one resolution. A cycle is indistinguishable from a long chain, so the only honest answer is a limit. |
| `EACCES` | The mode bits say no. |
| `EPERM` | `chmod` by a non-owner, or a hard link to a directory. |
| `ENOSPC` | Past `MAX_FILE_SIZE` (256 KB) or `MAX_TOTAL_SIZE` (1 MB). |
| `EINVAL` | `readlink` on something that is not a link, a rename of a directory into itself, or a stored image that will not hold together. |

The ceilings exist because the tree lives in `localStorage`: without them a write would be
accepted and then fail at save time, by which point the data is already in the editor.

## What persists, and how it fails

`fs.mjs` watches the filesystem's own change events and writes the serialised tree to
`localStorage` under `webxp.fs.v1`, 300 ms after the last write. One keystroke in Notepad is
one write, so saving per write would serialise the whole filesystem per character.

Three failures, each handled where it happens rather than swallowed:

- **A stored tree that will not parse** is reported to the console with the reason and
  replaced by a fresh seed. `VFS.parse` validates rather than trusts — the input is
  reachable from the devtools console and survives a version of the code that wrote a
  different shape, and an inode number pointing at nothing would otherwise surface much later
  as a directory that cannot be listed.
- **Quota, or storage denied mid-session**, stops saving and records why. `df` prints it. A
  filesystem that silently stopped persisting is worse than one that never did.
- **A version mismatch** is a reset, not a migration. The contents are a demo tree, and a
  loader that guesses at an older shape is more code than the data is worth.

`format --yes` in the terminal restores the seed tree. It empties the live instance in place
rather than making a new one, because other windows hold the old one.

## Watching it

```js
const unwatch = fs.watch(({ op, path }) => { /* 'write', 'unlink', 'reset', … */ });
win.onClose = unwatch;   // if you start it, stop it
```

The terminal uses this for two things: its status bar, and noticing that its own working
directory has been removed by another window. A watcher that throws is reported to the
console and skipped — the write it is reacting to has already happened, and the filesystem is
not the place for a listener's bug to surface.

## Checks

`scripts/check.py`:

| Check | Asserts |
| --- | --- |
| `filesystem` | The model itself, in the browser: resolution through nested symlinks, `..` after a link, `ELOOP`, dangling links, hard-link counts and shared contents, `rename` into its own subtree, the mode bits in both directions, and a `serialize` → `parse` round trip. |
| `terminal` | The commands, driven by real keystrokes. |
| `open across apps` | A file written in the terminal, opened in Notepad, saved, and read back — which is the single-mount claim, asserted. |
| `filesystem persists` | Write, reload the page, `cat`. |
