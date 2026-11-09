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

//: Where the tree is kept between reloads. The version is in the key rather than only in the
//: payload, so a future format change cannot be handed a tree it would have to guess at.
export const STORAGE_KEY = 'webxp.fs.v1';

//: How long after the last write the tree is saved. One keystroke in Notepad is one write,
//: so saving per write would serialize the whole filesystem per character.
const SAVE_DELAY_MS = 300;

//: Live state for the one mount, which is what `df` prints. A module-level object because
//: there is a module-level mount; it has no meaning before `openFileSystem` resolves.
export const persistence = {
  key: STORAGE_KEY,
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

/** Whether a mount has already happened, for a caller that must not cause one. */
export function isMounted() {
  return pending !== null;
}

async function mount() {
  const [{ VFS, FsError }, { buildSeed }] = await Promise.all([
    import('./vfs.mjs'),
    import('./seed.mjs'),
  ]);

  let fs = null;
  const saved = readStorage();
  if (saved) {
    try {
      fs = VFS.parse(saved);
      persistence.source = 'restored';
    } catch (error) {
      // A stored tree that will not parse is reported and replaced, never silently merged
      // into a fresh one. The console line is the only record of why yesterday's files are
      // gone, so it carries the reason rather than the fact.
      console.error(`webxp: stored filesystem unusable, seeding a fresh one — ${error.message}`);
      if (!(error instanceof FsError) && !(error instanceof SyntaxError)) throw error;
    }
  }
  if (!fs) {
    fs = new VFS();
    buildSeed(fs);
    persistence.source = 'seed';
  }

  attachSaving(fs);
  return fs;
}

function readStorage() {
  let raw;
  try {
    raw = window.localStorage.getItem(STORAGE_KEY);
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
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(fs.serialize()));
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
 */
export async function resetFileSystem() {
  const fs = await openFileSystem();
  const { buildSeed } = await import('./seed.mjs');
  fs.reset();
  buildSeed(fs);
  persistence.source = 'seed';
  persistence.error = null;
  save(fs);
  return fs;
}
