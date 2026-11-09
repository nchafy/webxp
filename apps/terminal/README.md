# Terminal

A POSIX shell over the filesystem in `shared/fs/`. XP's chrome, Linux's commands — a
deliberate mismatch, because the window is the desktop's and the command set is the one
people have in their hands.

```
ls -l links/                 one of each kind of symlink
find / -name '*.log'
grep -rn error /var
cat notes/todo.txt | wc -l
ls /etc > ~/listing.txt
open notes/todo.txt          Notepad, on the same file; Ctrl+S writes it back
help                         every command, with its flags
```

| Path | Subject |
| --- | --- |
| `app.mjs` | The pane, the prompt line, history keys, Tab completion, the status bar. |
| `shell.mjs` | The language: words, quotes, globs, pipelines, redirection. One session's state. |
| `commands.mjs` | Every command, one table. `help` is generated from it. |
| `glob.mjs` | `*`, `?`, `[...]`. Shared by word expansion and `find -name`. |

**`help` is the command reference**, and it is generated from the table rather than written
twice: a command added to `commands.mjs` is documented by existing. This file is about the
language, the shape of the code, and what is deliberately missing.

## The language

```
cmd arg 'quoted arg' "also quoted" | cmd2 -n 3 > file
cmd < file >> other              # comments run to the end of the line
```

- **Quotes and `\`** suppress globbing, which is what makes a file literally called `*`
  reachable: `ls '*'`.
- **Globs** are `*`, `?` and `[abc]`, per path component — `*.txt` never matches across a
  `/`. A leading dot has to be asked for by name, or `rm *` would take the dotfiles. An
  unmatched pattern comes back as itself, so `ls *.none` reports "no such file" naming the
  pattern instead of listing everything.
- **Pipes** pass one string. A stage that fails produces no output and the rest of the
  pipeline still runs, so `cat missing | wc -l` prints `0`.
- **`stderr` is never piped and never redirected.** A failing command inside a pipeline still
  says so, in the pane, in red.
- **`~`** expands at the start of a word. That is the only expansion: a `$` is a dollar sign.

### What `ls` does differently into a pipe

`ls` lays out columns to fit the pane, measured from a probe span in the pane's own font, and
switches to one name per line when its output is not going to the pane. Without that,
`ls | wc -l` would count rows rather than files.

## Flags are declared, not sniffed

`options()` is given the letters a command takes, so `ls -Z` is an error and `ls --colour` is
an error. A flag that does nothing is indistinguishable at the prompt from a flag that is not
implemented, and the second one is a lie.

## Two windows, one filesystem

Each Terminal window is its own session: its own working directory, history and scrollback.
They share the filesystem, because there is one mount — `shared/fs/README.md` has the
mechanism. `open <dir>` opens a second terminal already in that directory; `open <file>`
opens Notepad on it.

A window also watches for its own working directory being removed by another one, and says
so rather than failing the next command with `ENOENT` for a directory the reader never left.

## Deliberately absent

| Not here | Why |
| --- | --- |
| Variables, `$VAR`, `export` | Expansion is where a toy shell turns into a language. The floor is globs and `~`. |
| `&&`, `;`, `&` | Exit statuses exist and are returned; sequencing is the next thing to add rather than something half-present. Typing one is an error, not a filename — read as a word, `mkdir d && echo x` made four directories and reported success. |
| ANSI escapes | The pane is one text renderer, not a terminal emulator. `ls` has no colour and `less` has no screen to take over. |
| Symbolic `chmod u+x` | Octal only. A parser that took `u+x` and ignored `g-w` would be worse than one that takes neither. |
| Binary files, `dd`, `xxd` | The filesystem holds text. See `shared/fs/README.md`. |
| `/dev/null` | There are no device files — a node is a directory, a file or a symlink. `> /dev/null` says "no such file" rather than pretending to discard. |

## What to add next, in order

1. **`&&`, `||` and `;`.** The statuses are already there and returned; this is parsing only,
   and it is what makes everything else composable.
2. **A pager, and `less`/`more`.** `cat` on a long file floods the scrollback. The pane
   already measures its own height for this.
3. **`$VAR`, `export`, `$?`, `$PWD`.** Worth it once there is sequencing to use it with, and
   not before — a variable with nothing to branch on is decoration.
4. **`sed` and `cut`, or `tr`.** The gap in the pipeline set: there is no way to transform a
   line, only to select, count and sort it.
5. **`man`, as real pages in `/usr/share/man`.** `help` is the table; a page is prose, and
   the filesystem is the natural place to keep it.

Lower down, and cheap: `basename`, `dirname`, `tac`, `nl`, `yes`, `seq`, `diff`, `ln -f`,
`cp -i`, `tee`, `which`. Each is a table entry and a check.
