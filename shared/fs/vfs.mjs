/* The filesystem. Inodes, path resolution, symlinks, permissions, and nothing else.
 *
 * The model is POSIX's, not a tree of plain objects: a name in a directory is a number, and
 * the number names an inode that holds the data. That is what makes a hard link and a
 * symlink two different things rather than two words for the same copy, and it is why
 * `realpath` can disagree with the path you typed.
 *
 * ## What this is not
 *
 * Files hold text. A JS string is the contents, there is no binary mode, and `size` is a
 * character count. Everything above this layer is a text terminal and a text editor, so a
 * byte array would be a conversion at every boundary and a fidelity nobody reads.
 *
 * ## Errors
 *
 * Every refusal is an `FsError` carrying a POSIX `code`. The message is `path: reason`, so a
 * caller prints `` `${command}: ${error.message}` `` and gets the line coreutils would print.
 * A caller that wants to branch reads `code`; a caller that wants to report reads `message`.
 *
 * The shape of the tree that ships is `seed.mjs`. The single mounted instance, and its
 * persistence, are `fs.mjs`. This module imports nothing.
 */

//: Node types. The strings are what `stat().type` returns, so they are part of the API.
export const DIR = 'dir';
export const FILE = 'file';
export const LINK = 'link';

//: The root's inode number. Fixed, so a serialized filesystem does not have to say which
//: inode is the root.
const ROOT_INO = 1;

//: Linux's own ceiling on symlink traversals for one resolution (`MAXSYMLINKS`). A cycle is
//: indistinguishable from a very long chain, so the only honest answer is a limit. Exported
//: because every path that resolves one — `#lookup` and `realpath` — answers to this number,
//: and a check that asserts the ceiling has to name it rather than copy it.
export const MAX_LINKS = 40;

//: Permission bits, as they are tested rather than as they are written. Exported because a
//: caller that wants to ask before acting — `cd` checking it may enter a directory — has to
//: name the same bits this module checks.
export const PERM = Object.freeze({ R: 4, W: 2, X: 1 });
const R = PERM.R;
const W = PERM.W;
const X = PERM.X;

//: Ceilings, so a filesystem that lives in `localStorage` cannot grow past what the quota
//: will hold and fail at save time, by which point the write has already been accepted.
//: `df` prints both.
export const MAX_FILE_SIZE = 256 * 1024;
export const MAX_TOTAL_SIZE = 1024 * 1024;

//: Bumped when the serialized shape changes. A mismatch is a reset, not a migration: this
//: filesystem's contents are a demo tree, and a loader that guesses at an older shape is
//: more code than the data is worth.
export const FORMAT_VERSION = 1;

const MESSAGES = {
  ENOENT: 'No such file or directory',
  EEXIST: 'File exists',
  ENOTDIR: 'Not a directory',
  EISDIR: 'Is a directory',
  ENOTEMPTY: 'Directory not empty',
  ELOOP: 'Too many levels of symbolic links',
  EINVAL: 'Invalid argument',
  EACCES: 'Permission denied',
  EPERM: 'Operation not permitted',
  ENOSPC: 'No space left on device',
  ENAMETOOLONG: 'File name too long',
  EBUSY: 'Device or resource busy',
};

/** A refusal from the filesystem, carrying the code a caller can branch on. */
export class FsError extends Error {
  constructor(code, path = '') {
    super(path ? `${path}: ${MESSAGES[code] || code}` : (MESSAGES[code] || code));
    this.name = 'FsError';
    this.code = code;
    this.path = path;
  }
}

/* ---------------------------------------------------------------------------
   Paths. Lexical only — nothing here touches the filesystem, so these are safe
   to use on a path whose components do not exist yet.
   --------------------------------------------------------------------------- */

export function isAbsolute(path) {
  return path.startsWith('/');
}

/** The components of a path, with empties and `.` dropped. `..` survives. */
export function splitPath(path) {
  return path.split('/').filter((part) => part !== '' && part !== '.');
}

/** Join, keeping absoluteness from the first segment that has it. */
export function joinPath(...parts) {
  let out = '';
  for (const part of parts) {
    if (part === undefined || part === null || part === '') continue;
    if (isAbsolute(part)) out = part;
    else out = out ? `${out.replace(/\/+$/, '')}/${part}` : part;
  }
  return out;
}

/**
 * Collapse `.`, `..` and repeated slashes without consulting the filesystem.
 *
 * This is the `-L` rule a shell uses for its own working directory, and it is deliberately
 * not what the kernel does: `..` here cancels the previous name in the string, where
 * resolution follows the real parent of whatever that name pointed at. The two disagree
 * exactly when a symlink is involved, which is why `pwd` and `pwd -P` are different
 * commands.
 */
export function normalize(path) {
  const absolute = isAbsolute(path);
  const out = [];
  for (const part of splitPath(path)) {
    if (part === '..') {
      if (out.length && out[out.length - 1] !== '..') out.pop();
      else if (!absolute) out.push('..');
    } else {
      out.push(part);
    }
  }
  const joined = out.join('/');
  if (absolute) return `/${joined}`;
  return joined || '.';
}

/**
 * The last component of a path.
 *
 * `.` for a relative path with no components, never `/`: `splitPath` drops `.`, so a
 * basename that fell back to `/` answered `/` for `.` — and `joinPath(dest, '/')` is the
 * root, which is where `cp -r . dest` wrote.
 */
export function basename(path) {
  const parts = splitPath(path);
  if (parts.length) return parts[parts.length - 1];
  return isAbsolute(path) ? '/' : '.';
}

export function dirname(path) {
  const normalized = normalize(path);
  const cut = normalized.lastIndexOf('/');
  if (cut < 0) return '.';
  if (cut === 0) return '/';
  return normalized.slice(0, cut);
}

/** `rwxr-xr-x`, the way `ls -l` writes a mode. */
export function modeString(type, mode) {
  const kind = type === DIR ? 'd' : type === LINK ? 'l' : '-';
  let out = kind;
  for (const shift of [6, 3, 0]) {
    const bits = (mode >> shift) & 7;
    out += bits & R ? 'r' : '-';
    out += bits & W ? 'w' : '-';
    out += bits & X ? 'x' : '-';
  }
  return out;
}

/* ---------------------------------------------------------------------------
   The filesystem.
   --------------------------------------------------------------------------- */

export class VFS {
  /**
   * @param {object} [options]
   * @param {number} [options.uid] The identity every call is made as. One process, because
   *        there is one user at the keyboard; `chmod 000` still refuses, which is the part
   *        worth having.
   * @param {number} [options.gid]
   */
  constructor({ uid = 1000, gid = 1000 } = {}) {
    this.uid = uid;
    this.gid = gid;
    this.inodes = new Map();
    this.nextIno = ROOT_INO;
    this.watchers = new Set();
    //: Depth of nested `batch` calls. Above zero, watchers are not notified.
    this.batching = 0;
    const root = this.#create(DIR, { mode: 0o755, uid, gid });
    root.parent = root.ino;
    this.rootIno = root.ino;
  }

  /* --------------------------- inode plumbing --------------------------- */

  #create(type, { mode, uid = this.uid, gid = this.gid }) {
    const now = Date.now();
    const inode = {
      ino: this.nextIno,
      type,
      mode,
      uid,
      gid,
      // A new directory already has two names: its own, and the `..` inside it.
      nlink: type === DIR ? 2 : 1,
      atime: now,
      mtime: now,
      ctime: now,
    };
    this.nextIno += 1;
    if (type === DIR) {
      inode.entries = new Map();
      inode.parent = null;
    } else if (type === LINK) {
      inode.target = '';
    } else {
      inode.data = '';
    }
    this.inodes.set(inode.ino, inode);
    return inode;
  }

  #inode(ino) {
    const inode = this.inodes.get(ino);
    // A missing inode is a corrupt superblock, not a missing file: the only way to hold an
    // inode number is to have read it out of a directory.
    if (!inode) throw new FsError('EINVAL', `inode ${ino}`);
    return inode;
  }

  /** Whether this user has *every* requested bit on an inode. */
  #may(inode, bits) {
    if (this.uid === 0) {
      // Root ignores read and write bits, and still needs one execute bit to run a file —
      // which is what stops `chmod 000 file && ./file` from working as root.
      return (bits & X) === 0 || inode.type === DIR || (inode.mode & 0o111) !== 0;
    }
    const shift = inode.uid === this.uid ? 6 : inode.gid === this.gid ? 3 : 0;
    const granted = (inode.mode >> shift) & 7;
    return (granted & bits) === bits;
  }

  #need(inode, bits, path) {
    if (!this.#may(inode, bits)) throw new FsError('EACCES', path);
  }

  /* --------------------------- resolution --------------------------- */

  /**
   * Walk a path and report the directory it lands in, the final name, and what that name
   * holds — `null` when the name is free, which is what the create operations need.
   *
   * @returns {{dirIno: number, name: string, ino: number|null}}
   */
  #lookup(parts, startIno, { depth = 0, followFinal = true, shown = '' } = {}) {
    if (depth > MAX_LINKS) throw new FsError('ELOOP', shown);

    let dirIno = startIno;
    if (!parts.length) return { dirIno: this.#inode(startIno).parent, name: '', ino: startIno };

    for (let i = 0; i < parts.length; i += 1) {
      const part = parts[i];
      const last = i === parts.length - 1;
      const dir = this.#inode(dirIno);
      if (dir.type !== DIR) throw new FsError('ENOTDIR', shown);
      // The execute bit on a directory is the right to look inside it. Checked at every
      // level, because that is the only thing that makes `chmod 000 dir` mean anything.
      this.#need(dir, X, shown);

      let childIno;
      if (part === '..') childIno = dir.parent;
      else if (part === '.') childIno = dirIno;
      else childIno = dir.entries.get(part);

      if (childIno === undefined) {
        if (last) return { dirIno, name: part, ino: null };
        throw new FsError('ENOENT', shown);
      }

      const child = this.#inode(childIno);
      if (child.type === LINK && (!last || followFinal)) {
        const target = child.target;
        const base = isAbsolute(target) ? this.rootIno : dirIno;
        const hit = this.#lookup(splitPath(target), base, {
          depth: depth + 1,
          followFinal: true,
          shown: shown || target,
        });
        if (hit.ino === null) {
          // A dangling symlink. As the final component that is still a usable answer for a
          // create: writing through a broken link creates its target, which is POSIX's
          // behaviour and the reason this returns the target's parent rather than the link's.
          if (last) return hit;
          throw new FsError('ENOENT', shown);
        }
        if (last) return { dirIno: hit.dirIno, name: hit.name, ino: hit.ino };
        dirIno = hit.ino;
        continue;
      }

      if (last) return { dirIno, name: part, ino: childIno };
      dirIno = childIno;
    }
    // Unreachable: the loop returns on its last iteration.
    throw new FsError('EINVAL', shown);
  }

  /** Resolve one path against a working directory. */
  #resolve(path, { cwd = '/', followFinal = true } = {}) {
    if (typeof path !== 'string' || path === '') throw new FsError('ENOENT', String(path));
    const absolute = isAbsolute(path) ? path : joinPath(cwd, path);
    const hit = this.#lookup(splitPath(absolute), this.rootIno, { followFinal, shown: path });
    return { ...hit, shown: path };
  }

  /** Resolve a path that has to exist. */
  #existing(path, options = {}) {
    const hit = this.#resolve(path, options);
    if (hit.ino === null) throw new FsError('ENOENT', path);
    return { ...hit, inode: this.#inode(hit.ino) };
  }

  /** The directory a create writes into, with the name it writes. */
  #slot(path, options = {}) {
    const hit = this.#resolve(path, { ...options, followFinal: false });
    if (hit.dirIno === null || hit.name === '') throw new FsError('EEXIST', path);
    const dir = this.#inode(hit.dirIno);
    if (dir.type !== DIR) throw new FsError('ENOTDIR', path);
    return { ...hit, dir };
  }

  /* --------------------------- reading --------------------------- */

  /** One name's metadata, following a final symlink. */
  stat(path, options = {}) {
    return this.#info(this.#existing(path, options).inode);
  }

  /** One name's metadata, describing the symlink itself when it is one. */
  lstat(path, options = {}) {
    return this.#info(this.#existing(path, { ...options, followFinal: false }).inode);
  }

  /**
   * Whether a name resolves. A broken symlink does not.
   *
   * Only absence answers `false`. A name inside a directory this user may not look into is
   * not absent, and reporting it as absent is how `mkdir -p` ends up reporting `EEXIST` for
   * a path it was refused.
   */
  exists(path, options = {}) {
    try {
      this.stat(path, options);
      return true;
    } catch (error) {
      if (error instanceof FsError && (error.code === 'ENOENT' || error.code === 'ENOTDIR')) {
        return false;
      }
      throw error;
    }
  }

  /**
   * Refuse unless this user may do these things to a name.
   *
   * Exists because the right to enter a directory and the right to list it are different
   * bits, and `cd` wants to ask about the first one. Probing with a read would refuse a
   * directory that is `--x`, which is a real mode and a legal place to stand.
   *
   * @param {string} path
   * @param {number} bits Any of `PERM.R`, `PERM.W`, `PERM.X`, combined.
   */
  access(path, bits, options = {}) {
    const { inode } = this.#existing(path, options);
    this.#need(inode, bits, path);
  }

  #info(inode) {
    const info = {
      ino: inode.ino,
      type: inode.type,
      mode: inode.mode,
      modeString: modeString(inode.type, inode.mode),
      nlink: inode.nlink,
      uid: inode.uid,
      gid: inode.gid,
      mtime: inode.mtime,
      ctime: inode.ctime,
      atime: inode.atime,
      size: inode.type === DIR
        ? inode.entries.size
        : inode.type === LINK ? inode.target.length : inode.data.length,
    };
    if (inode.type === LINK) info.target = inode.target;
    return info;
  }

  /** The names in a directory, in insertion order. Sorting is the caller's business. */
  readdir(path, options = {}) {
    const { inode } = this.#existing(path, options);
    if (inode.type !== DIR) throw new FsError('ENOTDIR', path);
    this.#need(inode, R, path);
    return [...inode.entries.keys()];
  }

  /**
   * A directory's children with their own metadata, by `lstat` — so a symlink in a listing
   * reads as a symlink rather than as whatever it points at.
   */
  list(path, options = {}) {
    const { inode } = this.#existing(path, options);
    if (inode.type !== DIR) throw new FsError('ENOTDIR', path);
    this.#need(inode, R, path);
    return [...inode.entries.entries()].map(([name, ino]) => ({
      name,
      ...this.#info(this.#inode(ino)),
    }));
  }

  readFile(path, options = {}) {
    const { inode } = this.#existing(path, options);
    if (inode.type === DIR) throw new FsError('EISDIR', path);
    this.#need(inode, R, path);
    inode.atime = Date.now();
    return inode.data;
  }

  readlink(path, options = {}) {
    const { inode } = this.#existing(path, { ...options, followFinal: false });
    if (inode.type !== LINK) throw new FsError('EINVAL', path);
    return inode.target;
  }

  /**
   * The path with every symlink resolved away.
   *
   * Component by component, splicing a link's target back into the queue, which is the only
   * way to get this right: a link in the middle of a path changes what `..` after it means.
   */
  realpath(path, { cwd = '/' } = {}) {
    let pending = splitPath(isAbsolute(path) ? path : joinPath(cwd, path));
    const out = [];
    let hops = 0;
    while (pending.length) {
      if (hops > MAX_LINKS) throw new FsError('ELOOP', path);
      const part = pending.shift();
      if (part === '..') {
        out.pop();
        continue;
      }
      const here = `/${[...out, part].join('/')}`;
      const { inode } = this.#existing(here, { followFinal: false });
      if (inode.type === LINK) {
        hops += 1;
        const target = splitPath(inode.target);
        if (isAbsolute(inode.target)) out.length = 0;
        pending = [...target, ...pending];
        continue;
      }
      out.push(part);
    }
    return out.length ? `/${out.join('/')}` : '/';
  }

  /**
   * Every path under a directory, depth first, parents before children.
   *
   * Symlinks are reported, never descended: a link pointing at its own ancestor would make
   * the walk infinite, and `find` on a real filesystem does not follow links either unless
   * it is asked to.
   *
   * @returns {Generator<{path: string, name: string, depth: number, stat: object}>}
   */
  *walk(path, { cwd = '/', maxDepth = Infinity } = {}) {
    const start = isAbsolute(path) ? normalize(path) : normalize(joinPath(cwd, path));
    const stat = this.lstat(start);
    yield { path: start, name: basename(start), depth: 0, stat };
    if (stat.type !== DIR) return;
    yield* this.#descend(start, 1, maxDepth);
  }

  *#descend(dirPath, depth, maxDepth) {
    if (depth > maxDepth) return;
    let children;
    try {
      children = this.list(dirPath);
    } catch (error) {
      // An unreadable directory inside a walk is a fact about that directory, not the end
      // of the walk. Reported as an entry so a caller can print it the way `find` does.
      if (error instanceof FsError) {
        yield { path: dirPath, name: basename(dirPath), depth: depth - 1, error };
        return;
      }
      throw error;
    }
    for (const child of children) {
      const path = joinPath(dirPath, child.name);
      yield { path, name: child.name, depth, stat: child };
      if (child.type === DIR) yield* this.#descend(path, depth + 1, maxDepth);
    }
  }

  /* --------------------------- writing --------------------------- */

  mkdir(path, { cwd = '/', mode = 0o755, recursive = false } = {}) {
    if (recursive) {
      const parts = splitPath(isAbsolute(path) ? path : joinPath(cwd, path));
      let walked = '';
      for (const part of parts) {
        walked = `${walked}/${part}`;
        if (this.exists(walked)) {
          if (this.stat(walked).type !== DIR) throw new FsError('ENOTDIR', walked);
          continue;
        }
        this.#makeDir(walked, { cwd, mode });
      }
      return;
    }
    this.#makeDir(path, { cwd, mode });
  }

  #makeDir(path, { cwd, mode }) {
    const slot = this.#slot(path, { cwd });
    if (slot.ino !== null) throw new FsError('EEXIST', path);
    this.#need(slot.dir, W, path);
    const inode = this.#create(DIR, { mode });
    inode.parent = slot.dir.ino;
    slot.dir.entries.set(slot.name, inode.ino);
    // The child's `..` is a reference to its parent, which is what the link count counts.
    slot.dir.nlink += 1;
    this.#stamp(slot.dir);
    this.#changed('mkdir', path);
  }

  rmdir(path, { cwd = '/' } = {}) {
    const hit = this.#existing(path, { cwd, followFinal: false });
    if (hit.inode.type !== DIR) throw new FsError('ENOTDIR', path);
    if (hit.inode.ino === this.rootIno) throw new FsError('EBUSY', path);
    if (hit.inode.entries.size) throw new FsError('ENOTEMPTY', path);
    const dir = this.#inode(hit.dirIno);
    this.#need(dir, W, path);
    dir.entries.delete(hit.name);
    dir.nlink -= 1;
    this.inodes.delete(hit.inode.ino);
    this.#stamp(dir);
    this.#changed('rmdir', path);
  }

  /**
   * Remove one name. The inode survives while another name still holds it, which is the
   * whole of what a hard link is.
   */
  unlink(path, { cwd = '/' } = {}) {
    const hit = this.#existing(path, { cwd, followFinal: false });
    if (hit.inode.type === DIR) throw new FsError('EISDIR', path);
    const dir = this.#inode(hit.dirIno);
    this.#need(dir, W, path);
    dir.entries.delete(hit.name);
    hit.inode.nlink -= 1;
    if (hit.inode.nlink <= 0) this.inodes.delete(hit.inode.ino);
    this.#stamp(dir);
    this.#changed('unlink', path);
  }

  writeFile(path, text, { cwd = '/', mode = 0o644, append = false } = {}) {
    const body = String(text);
    const hit = this.#resolve(path, { cwd });
    let inode = hit.ino === null ? null : this.#inode(hit.ino);
    let dir = null;
    if (inode === null) {
      dir = this.#inode(hit.dirIno);
      if (dir.type !== DIR) throw new FsError('ENOTDIR', path);
      this.#need(dir, W, path);
    } else {
      if (inode.type === DIR) throw new FsError('EISDIR', path);
      this.#need(inode, W, path);
    }

    // Measured before anything is created. A refusal that had already made the inode and
    // the name left an empty file behind — and left it unsaved, because the throw skipped
    // the change event the debounced save listens to.
    const next = append && inode ? inode.data + body : body;
    if (next.length > MAX_FILE_SIZE) throw new FsError('ENOSPC', path);
    const delta = next.length - (inode ? inode.data.length : 0);
    if (this.usedBytes() + delta > MAX_TOTAL_SIZE) throw new FsError('ENOSPC', path);

    if (inode === null) {
      inode = this.#create(FILE, { mode });
      dir.entries.set(hit.name, inode.ino);
      this.#stamp(dir);
    }
    inode.data = next;
    this.#stamp(inode);
    this.#changed('write', path);
    return next.length;
  }

  appendFile(path, text, options = {}) {
    return this.writeFile(path, text, { ...options, append: true });
  }

  /**
   * Create an empty file, or move an existing one's timestamps forward.
   *
   * The write bit is checked here, on the inode being stamped, and not left to `writeFile`:
   * only the create branch goes through it, so a permission enforced there is enforced on
   * half of this operation. Unchecked, `chmod 444 f` followed by `touch f` moved the mtime
   * and announced a change on a file the caller may not write — which also told the saver in
   * `fs.mjs` to write a read-only file's new timestamp out.
   *
   * A directory is stamped the same way, needing write on the directory itself, because that
   * is what `touch dir` does.
   */
  touch(path, { cwd = '/', mode = 0o644 } = {}) {
    const hit = this.#resolve(path, { cwd });
    if (hit.ino === null) {
      this.writeFile(path, '', { cwd, mode });
      return;
    }
    const inode = this.#inode(hit.ino);
    this.#need(inode, W, path);
    this.#stamp(inode);
    this.#changed('touch', path);
  }

  symlink(target, path, { cwd = '/' } = {}) {
    if (typeof target !== 'string' || target === '') throw new FsError('EINVAL', path);
    const slot = this.#slot(path, { cwd });
    if (slot.ino !== null) throw new FsError('EEXIST', path);
    this.#need(slot.dir, W, path);
    // Stored verbatim, never resolved here. A symlink to a path that does not exist yet is
    // legal and useful, and resolving at creation time would quietly turn a relative link
    // into an absolute one.
    const inode = this.#create(LINK, { mode: 0o777 });
    inode.target = target;
    slot.dir.entries.set(slot.name, inode.ino);
    this.#stamp(slot.dir);
    this.#changed('symlink', path);
  }

  /** A second name for one inode. Directories are refused, as they are on Linux. */
  link(existing, path, { cwd = '/' } = {}) {
    const source = this.#existing(existing, { cwd });
    if (source.inode.type === DIR) throw new FsError('EPERM', existing);
    const slot = this.#slot(path, { cwd });
    if (slot.ino !== null) throw new FsError('EEXIST', path);
    this.#need(slot.dir, W, path);
    slot.dir.entries.set(slot.name, source.inode.ino);
    source.inode.nlink += 1;
    source.inode.ctime = Date.now();
    this.#stamp(slot.dir);
    this.#changed('link', path);
  }

  rename(from, to, { cwd = '/' } = {}) {
    const source = this.#existing(from, { cwd, followFinal: false });
    const slot = this.#slot(to, { cwd });
    const fromDir = this.#inode(source.dirIno);
    this.#need(fromDir, W, from);
    this.#need(slot.dir, W, to);

    if (source.inode.type === DIR) {
      // Moving a directory into its own subtree would detach it from the root and leave a
      // ring: reachable from itself, reachable from nothing else.
      for (let walk = slot.dir; ; walk = this.#inode(walk.parent)) {
        if (walk.ino === source.inode.ino) throw new FsError('EINVAL', to);
        if (walk.ino === this.rootIno) break;
      }
    }

    if (slot.ino !== null) {
      const target = this.#inode(slot.ino);
      if (target.ino === source.inode.ino) return;
      if (target.type === DIR) {
        if (source.inode.type !== DIR) throw new FsError('EISDIR', to);
        if (target.entries.size) throw new FsError('ENOTEMPTY', to);
        slot.dir.entries.delete(slot.name);
        slot.dir.nlink -= 1;
        this.inodes.delete(target.ino);
      } else {
        if (source.inode.type === DIR) throw new FsError('ENOTDIR', to);
        slot.dir.entries.delete(slot.name);
        target.nlink -= 1;
        if (target.nlink <= 0) this.inodes.delete(target.ino);
      }
    }

    fromDir.entries.delete(source.name);
    slot.dir.entries.set(slot.name, source.inode.ino);
    if (source.inode.type === DIR) {
      source.inode.parent = slot.dir.ino;
      if (fromDir.ino !== slot.dir.ino) {
        fromDir.nlink -= 1;
        slot.dir.nlink += 1;
      }
    }
    source.inode.ctime = Date.now();
    this.#stamp(fromDir);
    if (slot.dir !== fromDir) this.#stamp(slot.dir);
    this.#changed('rename', to);
  }

  /** Copy one file's contents and mode into a new name. */
  copyFile(from, to, { cwd = '/' } = {}) {
    const source = this.#existing(from, { cwd });
    if (source.inode.type === DIR) throw new FsError('EISDIR', from);
    this.#need(source.inode, R, from);
    this.writeFile(to, source.inode.data, { cwd, mode: source.inode.mode });
  }

  chmod(path, mode, { cwd = '/' } = {}) {
    const { inode } = this.#existing(path, { cwd });
    // Only the owner, as on Linux: a mode anyone may change is not a permission.
    if (this.uid !== 0 && inode.uid !== this.uid) throw new FsError('EPERM', path);
    inode.mode = mode & 0o7777;
    inode.ctime = Date.now();
    this.#changed('chmod', path);
  }

  #stamp(inode) {
    const now = Date.now();
    inode.mtime = now;
    inode.ctime = now;
  }

  /**
   * Empty the filesystem back to a bare root, in place.
   *
   * In place, and not by constructing a new `VFS`, because the instance is shared: two
   * terminals and a Notepad hold references to it, and handing the caller a replacement
   * would leave every other window writing into the filesystem nobody can see any more.
   */
  reset() {
    this.inodes.clear();
    this.nextIno = ROOT_INO;
    const root = this.#create(DIR, { mode: 0o755, uid: this.uid, gid: this.gid });
    root.parent = root.ino;
    this.rootIno = root.ino;
    this.#changed('reset', '/');
  }

  /* --------------------------- observation --------------------------- */

  /**
   * Watch every mutation. Returns the function that stops watching.
   *
   * A throwing watcher is reported and skipped rather than allowed to abort the write that
   * notified it: the write has already happened, and the filesystem is not the right place
   * for a listener's bug to surface.
   */
  watch(listener) {
    this.watchers.add(listener);
    return () => this.watchers.delete(listener);
  }

  /**
   * Run a sequence of mutations as one, notifying watchers once at the end.
   *
   * Rebuilding the tree is the case this exists for. Every step of a reseed is a mutation,
   * and the first of them fires while the tree is a bare root — so a watcher that asks "does
   * my working directory still exist" is asked halfway through, gets "no", and acts on an
   * answer that stops being true two calls later. A terminal was left stranded at `/` by
   * exactly that.
   *
   * @param {string} op   What the batch is, for the one event watchers do get.
   * @param {() => void} work
   */
  batch(op, work) {
    this.batching += 1;
    try {
      work();
    } finally {
      this.batching -= 1;
    }
    this.#changed(op, '/');
  }

  #changed(op, path) {
    // Inside a batch the tree is mid-rebuild, and a watcher reading it would read a tree
    // that never existed.
    if (this.batching > 0) return;
    for (const listener of this.watchers) {
      try {
        listener({ op, path });
      } catch (error) {
        console.error('vfs: a filesystem watcher threw', error);
      }
    }
  }

  /* --------------------------- accounting --------------------------- */

  usedBytes() {
    let total = 0;
    for (const inode of this.inodes.values()) {
      if (inode.type === FILE) total += inode.data.length;
      else if (inode.type === LINK) total += inode.target.length;
    }
    return total;
  }

  /** What `df` prints, and what a status bar counts. */
  stats() {
    let files = 0;
    let dirs = 0;
    let links = 0;
    for (const inode of this.inodes.values()) {
      if (inode.type === DIR) dirs += 1;
      else if (inode.type === LINK) links += 1;
      else files += 1;
    }
    return {
      files,
      dirs,
      links,
      inodes: this.inodes.size,
      used: this.usedBytes(),
      total: MAX_TOTAL_SIZE,
    };
  }

  /* --------------------------- persistence --------------------------- */

  /** A plain object `JSON.stringify` can take. Inode numbers are the whole structure. */
  serialize() {
    return {
      version: FORMAT_VERSION,
      nextIno: this.nextIno,
      inodes: [...this.inodes.values()].map((inode) => {
        const row = {
          ino: inode.ino,
          type: inode.type,
          mode: inode.mode,
          uid: inode.uid,
          gid: inode.gid,
          nlink: inode.nlink,
          mtime: inode.mtime,
          ctime: inode.ctime,
          atime: inode.atime,
        };
        if (inode.type === DIR) {
          row.parent = inode.parent;
          row.entries = [...inode.entries.entries()];
        } else if (inode.type === LINK) {
          row.target = inode.target;
        } else {
          row.data = inode.data;
        }
        return row;
      }),
    };
  }

  /**
   * Rebuild from `serialize`'s output, refusing anything that would not hold together.
   *
   * Validated rather than trusted because the input is whatever is in `localStorage`, which
   * is reachable from the devtools console and survives a version of the code that wrote a
   * different shape. An inode number pointing at nothing would otherwise surface much later
   * as a directory that cannot be listed.
   */
  static parse(data, options = {}) {
    if (!data || typeof data !== 'object') throw new FsError('EINVAL', 'filesystem image');
    if (data.version !== FORMAT_VERSION) {
      throw new FsError('EINVAL', `filesystem image version ${data.version}`);
    }
    if (!Array.isArray(data.inodes) || !data.inodes.length) {
      throw new FsError('EINVAL', 'filesystem image has no inodes');
    }

    const fs = new VFS(options);
    fs.inodes.clear();
    for (const row of data.inodes) {
      if (!Number.isInteger(row.ino) || ![DIR, FILE, LINK].includes(row.type)) {
        throw new FsError('EINVAL', `filesystem image inode ${JSON.stringify(row.ino)}`);
      }
      const inode = {
        ino: row.ino,
        type: row.type,
        mode: Number.isInteger(row.mode) ? row.mode : 0o644,
        uid: Number.isInteger(row.uid) ? row.uid : fs.uid,
        gid: Number.isInteger(row.gid) ? row.gid : fs.gid,
        nlink: Number.isInteger(row.nlink) ? row.nlink : 1,
        mtime: row.mtime || Date.now(),
        ctime: row.ctime || Date.now(),
        atime: row.atime || Date.now(),
      };
      if (row.type === DIR) {
        // Every entry is checked to be a `[name, ino]` pair before the Map is built. `new
        // Map([1, 2])` throws a TypeError, which is not an `FsError`, which means the mount
        // in `fs.mjs` rethrows instead of reseeding — one malformed byte in `localStorage`
        // and both apps fail to start on every launch, with no way back from inside the
        // desktop. Refusing it as a bad image is the recoverable answer.
        if (!Array.isArray(row.entries)) {
          // Not defaulted to empty. A directory whose entry list is the wrong shape would
          // come back as an empty directory, which restores a tree with every file silently
          // missing — the one outcome worse than reseeding and saying so.
          throw new FsError('EINVAL', `filesystem image: inode ${row.ino} has no entry list`);
        }
        inode.entries = new Map();
        for (const entry of row.entries) {
          if (!Array.isArray(entry) || entry.length !== 2
              || typeof entry[0] !== 'string' || !Number.isInteger(entry[1])) {
            throw new FsError(
              'EINVAL',
              `filesystem image: inode ${row.ino} has an entry that is not [name, ino]`,
            );
          }
          inode.entries.set(entry[0], entry[1]);
        }
        inode.parent = Number.isInteger(row.parent) ? row.parent : ROOT_INO;
      } else if (row.type === LINK) {
        // Strict for the same reason: an empty target is a link to nothing, and a file
        // defaulted to '' is a file whose contents were quietly dropped.
        if (typeof row.target !== 'string' || row.target === '') {
          throw new FsError('EINVAL', `filesystem image: inode ${row.ino} has no target`);
        }
        inode.target = row.target;
      } else {
        if (typeof row.data !== 'string') {
          throw new FsError('EINVAL', `filesystem image: inode ${row.ino} has no contents`);
        }
        inode.data = row.data;
      }
      fs.inodes.set(inode.ino, inode);
    }

    const root = fs.inodes.get(ROOT_INO);
    if (!root || root.type !== DIR) throw new FsError('EINVAL', 'filesystem image has no root');
    fs.rootIno = ROOT_INO;
    for (const inode of fs.inodes.values()) {
      if (inode.type !== DIR) continue;
      for (const [name, ino] of inode.entries) {
        if (!fs.inodes.has(ino)) {
          throw new FsError('EINVAL', `filesystem image: ${name} points at missing inode ${ino}`);
        }
      }
    }
    // Walked rather than spread into `Math.max`: an image with more inodes than the
    // argument limit would throw a RangeError here, which is a stack message instead of a
    // filesystem.
    let highest = ROOT_INO;
    for (const ino of fs.inodes.keys()) if (ino > highest) highest = ino;
    fs.nextIno = Math.max(Number.isInteger(data.nextIno) ? data.nextIno : 0, highest + 1);
    return fs;
  }
}
