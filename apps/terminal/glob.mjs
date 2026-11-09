/* Name patterns: `*`, `?`, `[...]`.
 *
 * Its own module because two callers need it and they must agree. The shell expands a word
 * into filenames; `find -name` and `grep --include` test one name against a pattern. A
 * second implementation of the bracket rules is a second set of bracket bugs.
 *
 * Imports nothing.
 */

const MAGIC = /[*?[]/;

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
      const body = pattern.slice(i + 1, close);
      out += `[${body.startsWith('!') ? `^${body.slice(1)}` : body}]`;
      i = close;
    } else out += char.replace(/[.+^${}()|\\\]]/g, '\\$&');
  }
  try {
    return new RegExp(`${out}$`, ignoreCase ? 'i' : '');
  } catch {
    // A bracket body can still be unparseable as a character class — `[\]` is the short
    // one — and the pattern reaches here straight from `find -name`, outside any try. The
    // whole pattern degrades to a literal rather than becoming a stack trace, which is the
    // same answer the unclosed-bracket case above already gives.
    return new RegExp(`^${pattern.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}$`, ignoreCase ? 'i' : '');
  }
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
  } catch {
    return false;
  }
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
        } catch {
          // A component that is not a readable directory contributes no matches, which is
          // also what a shell does.
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

  const matches = prefixes.filter((path) => present(fs, path, cwd));
  return matches.length ? matches : [pattern];
}
