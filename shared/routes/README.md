# Links

One URL names one app and the state that app knows how to restore.

```
#/terminal                     the shell, at home
#/terminal/usr/src/webxp       the shell, already there
#/explorer/home/user/links     a folder window on that folder
#/notepad/etc/motd             a file, open
```

Before this, nothing in the desktop was linkable: `grep -rn 'location.hash\|pushState\|
popstate'` over `src/`, `apps/`, `demo/` and `shared/` returned nothing at all. You could
open day 02, drill into a street, and there was no way to send anybody what you were looking
at.

| Path | Subject |
| --- | --- |
| `routes.mjs` | Parse and format a hash, publish one, restore one, describe a page. Imports nothing. |

## The router knows no app

`parse` splits a hash into three things — an id, a path, and query parameters — and hands the
last two to whoever owns the id. `street/1234` means nothing here and something to day 02.
That is what keeps a per-app parser out of the router, and why the state is a path rather
than a scheme this module would have to understand.

The id **is** the program id: `#/day-02-lines`, not `#/day-02`. A prettier id needs a
registry mapping one to the other, which is a second name for every app and a thing to
drift. When an app can declare its own route name in its spec, that declaration has to
*replace* the program id as the answer rather than sit beside it — two keys that both answer
"what is this app called in a URL" is the same table again, in a shape that looks like a
feature.

## What a link carries, and what it does not

One app and its state. No window geometry, no z-order, no second window — opening a link
gives you a fresh desktop with that one window on it. That is what a reader wants from a link
they were sent, and it keeps the URL short enough to paste.

An app opts in by reading `args.path` in its own `mount`. One that reads nothing still gets
`#/<id>` and opens bare, which is the honest reading of a bare id.

### A route does not suppress `~/Startup`

It lands on top of it. `boot.mjs` opens the startup set first and restores the route last, so
the linked window is the focused one and whatever the reader asked to open is still open
behind it.

The reverse looks tidier — a link opening exactly one window and nothing else — and it is
wrong for a reason that only shows up when you ask whose folder it is. `~/Startup` is the
*reader's* standing instruction about the *reader's* desktop; the filesystem lives in their
browser, not the sender's. A link that suppressed it would let a stranger's URL quietly
cancel your configuration, which is a worse surprise than a link opening alongside the two
windows you asked for every morning.

It is also the consistent reading of what a link is here: one app and its state, not a
desktop. The sender is contributing a window, not replacing a machine — their `/home` is not
the reader's either, so "what I see is what you see" was never on offer.

A route naming an app the startup set already opened gives two windows of it, as two of
anything non-singleton always does, and their state differs — which is the case where you
want both.

`args.path` is absolute whoever sends it — `open <dir>` passes a `realpath`, a link carries
one with the leading slash implied, and `asAbsolute` puts it back. There is no second key for
the same idea: `args.cwd` is gone.

## Publishing

A window publishes a link to itself as it moves — every `cd`, every folder, every file
opened. **`replaceState`, not `location.hash =`**: filling the back button with steps nobody
took deliberately would stop the back button meaning "the page I came from". The address bar
still updates, so the link is always there to copy. `check_routes` asserts `history.length`
does not move.

Two terminals means the one you last typed in owns the URL. A link names one app and its
state, so the window being acted in is the one it describes.

## `/pages`

```
$ ls /pages
about  basemap  explorer  notepad  terminal  …
$ cat /pages/explorer
app:   explorer
name:  Explorer
link:  #/explorer
state: a path, as #/explorer/some/path
```

A **view** of the route table, not the route table itself. The filesystem shows what is
routable; the router decides what routing means.

That split is the whole design, and it is why `cat` can print something true. A tree that
*was* the router would have to answer what `chmod 000 /pages/day-02-lines` does — break the
site, which couples the shell to routing, or nothing, which makes the permissions decoration.
This project removed four decorative permissions in one afternoon; adding one back by design
would be the wrong lesson learned.

Grafted read-only by `VFS.mountText` — `444` files in a `555` directory, dropped from what is
saved, never counted against the quota. `mountText` rather than `mountIndex` because the
contents are generated rather than fetched, and recording a URL nothing will ever request
would be a lie in the data structure.

The host page decides what is in it and `shared/fs/fs.mjs` decides when: `onMount(graft)`
runs when something first needs a filesystem, so a reader who opens no window pays for
neither. Decision in the host page, timing in the filesystem — the same split as `~/Startup`.

## Checks

`scripts/check.py`, `routes`:

- Parsing and formatting, including a bare `#` (which browsers produce for "top of page" and
  which must not be read as a route), an encoded separator, a space, and a round trip of what
  each window publishes.
- Three links opened from cold, each asserting the window landed on the state the URL names,
  and that **only** that window opened.
- A link to a program that does not exist reports a dead link rather than opening nothing in
  silence.
- A `cd` publishes a link and adds no history entry.
- `/pages` lists what is routable, `cat` prints a link, the link it advertises is the link
  that works — read out of the file and opened, rather than compared to a string written
  twice — and every write to it is refused.
