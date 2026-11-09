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

  attachSaving(fs);
  return fs;
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
  persistence.source = 'seed';
  persistence.error = null;
  save(fs);
  return fs;
}
