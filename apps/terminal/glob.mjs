/* Name patterns: `*`, `?`, `[...]`.
 *
 * Its own module because two callers need it and they must agree. The shell expands a word
 * into filenames; `find -name` and `find -iname` test one name against a pattern. A second
 * implementation of the bracket rules is a second set of bracket bugs.
 *
 * Imports one thing: the filesystem's node types, because a pattern ending in a separator
 * means directories only, and `'dir'` spelled here would be a second copy of a value
 * `vfs.mjs` exports.
 */

import { DIR } from '../../shared/fs/vfs.mjs';

const MAGIC = /[*?[]/;

//: The refusals that mean "this name contributes no matches". Everything else is a defect in
//: the filesystem or in this module, and is logged rather than read as absence.
const UNMATCHABLE = new Set(['ENOENT', 'ENOTDIR', 'EACCES', 'ELOOP']);

export function hasMagic(word) {
  return MAGIC.test(word);
}

/**
 * One path component's pattern, as a regular expression anchored at both ends.
 *
 * `[^/]` rather than `.` for `?` and `*`, because a pattern component never matches across a
 * separator: `*.txt` must not match `notes/a.txt`.
 */
export function componentRegex(pattern, { ignoreCase = false } = {}) {
  let out = '^';
  for (let i = 0; i < pattern.length; i += 1) {
    const char = pattern[i];
    if (char === '*') out += '[^/]*';
    else if (char === '?') out += '[^/]';
    else if (char === '[') {
      const close = pattern.indexOf(']', i + 2);
      if (close < 0) {
        // An unclosed bracket is a literal bracket. A shell does not refuse the line for it,
        // and refusing here would make `ls [` an error instead of a lookup.
        out += '\\[';
        continue;
      }
      out += charClass(pattern.slice(i + 1, close));
      i = close;
    } else out += char.replace(/[.+^${}()|\\\]]/g, '\\$&');
  }
  try {
    return new RegExp(`${out}$`, ignoreCase ? 'i' : '');
  } catch (error) {
    // A bracket body can still be unparseable as a character class — `[a-\]` is the short
    // one — and the pattern reaches here straight from `find -name`, outside any try. The
    // whole pattern degrades to a literal rather than becoming a stack trace, which is the
    // same answer the unclosed-bracket case above already gives. Only a pattern that will
    // not compile: anything else thrown here is not this function's to absorb.
    if (!(error instanceof SyntaxError)) throw error;
    return new RegExp(`^${pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`, ignoreCase ? 'i' : '');
  }
}

/**
 * One `[...]` body as a JS character class.
 *
 * The members are escaped rather than passed through. A `]` inside the body is a literal in
 * a glob — `[]]` matches `]`, which is why the scan for the closing bracket starts one
 * character late — but an unescaped `]` closes a JS class, so `[]]` compiled to the empty
 * class `[]`: it matched nothing, threw nothing, and left a file called `]` unfindable.
 */
function charClass(body) {
  const negated = body.startsWith('!');
  const members = negated ? body.slice(1) : body;
  return `[${negated ? '^' : ''}${members.replace(/[\]\\^]/g, '\\$&')}]`;
}

function joinPrefix(prefix, part) {
  if (prefix === '') return part;
  if (prefix.endsWith('/')) return prefix + part;
  return `${prefix}/${part}`;
}

/** Whether a name is there at all, counting a symlink that points at nothing. */
function present(fs, path, cwd) {
  try {
    fs.lstat(path, { cwd });
    return true;
  } catch (error) {
    report(path, error);
    return false;
  }
}

/** Whether a name names a directory, following a final symlink as a shell does. */
function isDirectory(fs, path, cwd) {
  try {
    return fs.stat(path, { cwd }).type === DIR;
  } catch (error) {
    report(path, error);
    return false;
  }
}

/**
 * Name a refusal this module cannot read as "no matches here".
 *
 * Swallowed, a defect in resolution would be indistinguishable from a name that is not
 * there: the pattern would come back as itself and the reader would be told the file does
 * not exist.
 */
function report(path, error) {
  if (UNMATCHABLE.has(error.code)) return;
  console.error(`glob: ${path} could not be examined`, error);
}

/**
 * Expand one pattern against the filesystem, keeping the shape the user typed: a relative
 * pattern yields relative paths.
 *
 * An unmatched pattern comes back as itself, which is the default a shell has. `ls *.none`
 * then reports "no such file" naming the pattern, instead of silently listing everything.
 */
export function expandGlob(fs, cwd, pattern) {
  if (!hasMagic(pattern)) return [pattern];
  let prefixes = [pattern.startsWith('/') ? '/' : ''];
  // A trailing separator is the directories-only constraint, and it is carried here rather
  // than left in the component list: `'*/'.split('/')` ends in an empty component, which is
  // skipped, which dropped the constraint and let `echo */` answer with regular files.
  const directoriesOnly = pattern.endsWith('/');

  for (const part of pattern.split('/')) {
    if (part === '') continue;
    const next = [];
    if (!hasMagic(part)) {
      for (const prefix of prefixes) next.push(joinPrefix(prefix, part));
    } else {
      const regex = componentRegex(part);
      for (const prefix of prefixes) {
        let names;
        try {
          names = fs.readdir(prefix || '.', { cwd });
        } catch (error) {
          // A component that is not a readable directory contributes no matches, which is
          // also what a shell does.
          report(prefix || '.', error);
          continue;
        }
        for (const name of names.sort()) {
          // A leading dot has to be asked for by name, or `rm *` would take the dotfiles.
          if (name.startsWith('.') && !part.startsWith('.')) continue;
          if (regex.test(name)) next.push(joinPrefix(prefix, name));
        }
      }
    }
    prefixes = next;
  }

  const matches = directoriesOnly
    ? prefixes.filter((path) => isDirectory(fs, path, cwd)).map((path) => `${path}/`)
    : prefixes.filter((path) => present(fs, path, cwd));
  return matches.length ? matches : [pattern];
}
