/* The one mounted filesystem, and the only place that writes it to disk.
 *
 * Two apps that open "the filesystem" have to get the same object or the Notepad window
 * saves into a tree the terminal cannot see. A module-level promise is what guarantees that:
 * ES modules dedupe by resolved URL, so every importer of this path shares one mount
 * regardless of how many windows ask.
 *
 * Nothing is imported at module scope. Every `app.mjs` is imported at startup to build the
 * Start menu, so an app that imports this module eagerly must not pull the engine, the seed
 * tree and a `localStorage` read in with it. See `docs/apps.md`, "Sharing a dependency
 * between apps".
 */

//: Where the tree is kept between reloads, as a function of the format version: a bumped
//: version is a new key, so the new code is never handed a payload written by the old one.
//:
//: Derived rather than written out, because `webxp.fs.v1` and `FORMAT_VERSION = 1` are one
//: fact. Hand-synced, the version could be bumped in `vfs.mjs` while old payloads kept
//: answering under the same key — which is the single thing the key's version exists to
//: prevent. The live value is `persistence.key`.
function storageKey(version) {
  return `webxp.fs.v${version}`;
}

//: How long after the last write the tree is saved. One keystroke in Notepad is one write,
//: so saving per write would serialize the whole filesystem per character.
const SAVE_DELAY_MS = 300;

//: Live state for the one mount, which is what `df` prints. A module-level object because
//: there is a module-level mount; it has no meaning before `openFileSystem` resolves.
export const persistence = {
  /** Set when the mount resolves the format version. Null before that. */
  key: null,
  /** 'seed' when the tree was built fresh, 'restored' when it came out of storage. */
  source: 'unmounted',
  /** Number of completed saves this session. */
  saves: 0,
  /** Milliseconds of the last save, or null. */
  lastSaved: null,
  /** Why saving is off, or null. Set once and not retried. */
  error: null,
};

let pending = null;

/**
 * The filesystem, mounted once.
 *
 * @returns {Promise<import('./vfs.mjs').VFS>}
 */
export function openFileSystem() {
  if (!pending) {
    // A rejection is not cached. A single failed dynamic import would otherwise be replayed
    // to every window for the rest of the session, with the module answering 200 the whole
    // time — the same trap `src/apps.mjs` documents for a stylesheet that blipped.
    pending = mount().catch((error) => {
      pending = null;
      throw error;
    });
  }
  return pending;
}

//: Where the repository's own files are grafted, and where the index that describes them
//: lives. Both are resolved against this module rather than against the page, so the mount
//: survives the site being served from a subdirectory.
const INDEX_URL = new URL('./mount.json', import.meta.url);
const REPO_URL = new URL('../../', import.meta.url);

/** How many files the mount grafted, and what went wrong if it did not. */
export const mounted = { root: null, files: 0, error: null };

async function mount() {
  const [{ VFS, FORMAT_VERSION }, { buildSeed }] = await Promise.all([
    import('./vfs.mjs'),
    import('./seed.mjs'),
  ]);
  persistence.key = storageKey(FORMAT_VERSION);

  let fs = null;
  const saved = readStorage();
  if (saved) {
    try {
      fs = VFS.parse(saved);
      persistence.source = 'restored';
    } catch (error) {
      // Any failure, not only the ones `VFS.parse` names.
      //
      // The input is whatever is in `localStorage`: a payload from an older build, something
      // a devtools console wrote, a half-finished write. There is no shape of it that should
      // leave the desktop unusable, and a rethrow does exactly that — the mount rejects,
      // every launch of the terminal and Notepad fails, and `format` cannot run because it
      // needs a mount. So an unparseable image is a bad image, reported with its reason and
      // replaced. A defect in `parse` itself shows up as this line in the console rather
      // than as a dead desktop.
      console.error(`webxp: stored filesystem unusable, seeding a fresh one — ${error.message}`);
    }
  }
  if (!fs) {
    fs = new VFS();
    fs.batch('seed', () => buildSeed(fs));
    persistence.source = 'seed';
  }

  // After the tree is in place, never before: the mount is re-grafted on every boot and is
  // deliberately absent from what was saved, so grafting first would mean restoring over it.
  await graftRepository(fs);
  attachSaving(fs);
  return fs;
}

/**
 * Graft the repository's own files into the tree, read-only.
 *
 * A failure here is reported and survived. The desktop is usable without `/usr/src` — the
 * terminal, the home directory and everything the user wrote are all still there — so a
 * missing or unparseable index must not be the difference between a desktop and no desktop.
 * `df` says when it happened.
 */
async function graftRepository(fs) {
  let index;
  try {
    const response = await fetch(INDEX_URL);
    if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
    index = await response.json();
  } catch (error) {
    mounted.error = `index unreadable: ${error.message}`;
    console.error(
      `webxp: could not read ${INDEX_URL.pathname} — /usr/src will be missing. `
      + 'Run scripts/mount.py to write it.',
      error,
    );
    return;
  }
  try {
    mounted.files = fs.mountIndex(
      index.root,
      index,
      (relative) => new URL(relative, REPO_URL).href,
    );
    mounted.root = index.root;
  } catch (error) {
    mounted.error = `index unusable: ${error.message}`;
    console.error('webxp: the mount index did not graft', error);
  }
}

/**
 * Fetch the bodies of any mounted files among these paths.
 *
 * Called by whatever is about to read them — the shell before it runs a command that reads
 * contents, Notepad before it shows a file. The filesystem decides which paths still need
 * fetching; this does the fetching, because `vfs.mjs` does no I/O.
 *
 * A fetch that fails is reported and the file is left unhydrated, so the read that follows
 * fails with the filesystem's own message rather than with an empty file. Silence here would
 * turn a 404 into a file that looks empty.
 *
 * @returns {Promise<number>} How many were fetched.
 */
export async function hydrate(fs, paths, { cwd = '/' } = {}) {
  const need = fs.pending([].concat(paths), { cwd });
  if (!need.length) return 0;
  let filled = 0;
  await Promise.all(need.map(async ({ path, url }) => {
    try {
      const response = await fetch(url);
      if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
      fs.fill(path, await response.text(), { cwd });
      filled += 1;
    } catch (error) {
      // Recorded on the file, not only in the console. Left unrecorded, the read that
      // followed reported "the caller must await hydrate() first" — which blames the
      // program for a 404 and sends the reader to the wrong fix, while the real reason sat
      // in a console line nobody had open.
      fs.failed(path, error.message, { cwd });
      console.error(`webxp: could not fetch ${url} for ${path}`, error);
    }
  }));
  return filled;
}

function readStorage() {
  let raw;
  try {
    raw = window.localStorage.getItem(persistence.key);
  } catch (error) {
    // A browser with storage denied is a browser with no persistence, not a broken desktop.
    persistence.error = `storage unreadable: ${error.name}`;
    console.error('webxp: localStorage is unavailable, the filesystem will not persist', error);
    return null;
  }
  if (!raw) return null;
  try {
    return JSON.parse(raw);
  } catch (error) {
    console.error(`webxp: stored filesystem is not JSON — ${error.message}`);
    return null;
  }
}

/** Debounced write-through, driven by the filesystem's own change events. */
function attachSaving(fs) {
  let timer = null;
  fs.watch(() => {
    if (persistence.error) return;
    if (timer !== null) clearTimeout(timer);
    timer = setTimeout(() => {
      timer = null;
      save(fs);
    }, SAVE_DELAY_MS);
  });
}

/** Write the tree out now. Returns the failure reason, or null. */
export function save(fs) {
  try {
    window.localStorage.setItem(persistence.key, JSON.stringify(fs.serialize()));
    persistence.saves += 1;
    persistence.lastSaved = Date.now();
    return null;
  } catch (error) {
    // Quota, or storage denied mid-session. Saving stops rather than retrying on every
    // keystroke, and `df` says so: a filesystem that silently stopped persisting is worse
    // than one that never did.
    persistence.error = `${error.name}: saving is off`;
    console.error('webxp: could not save the filesystem', error);
    return persistence.error;
  }
}

/**
 * Put the seed tree back, in the live filesystem.
 *
 * The instance is kept and emptied rather than replaced, because other windows hold it.
 * Their working directories may no longer exist afterwards, which the terminal notices
 * through the change event it is already watching.
 *
 * Emptying and reseeding are one batch, so that event arrives once, with the tree whole. Un-
 * batched, the first mutation of the rebuild notified every watcher while the filesystem was
 * a bare root, and a terminal sitting in `/home/user` was moved to `/` on the grounds that
 * its directory did not exist — two calls before it did again.
 */
export async function resetFileSystem() {
  const fs = await openFileSystem();
  const { buildSeed } = await import('./seed.mjs');
  fs.batch('reset', () => {
    fs.reset();
    buildSeed(fs);
  });
  // The repository is not the user's to discard, and `format` wiped it along with everything
  // else. Re-grafted here rather than left missing until the next reload.
  await graftRepository(fs);
  persistence.source = 'seed';
  persistence.error = null;
  save(fs);
  return fs;
}
