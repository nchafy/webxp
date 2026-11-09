/* The commands. One table, one entry per command, each entry a `run(context)`.
 *
 * A command reads `context.stdin`, writes with `context.write`, reports with `context.warn`
 * and returns an exit status. It never touches the DOM and never knows whether its output is
 * going to the pane, into a pipe or into a file — which is what makes `ls -l > listing.txt`
 * work without `ls` knowing that redirection exists.
 *
 * ## The two rules the table keeps
 *
 * 1. **Every command is in `COMMANDS`, and `help` is generated from it.** A command added
 *    here is documented by existing; there is no second list to keep in step.
 * 2. **Flags are declared, not sniffed.** `options()` is given the letters a command takes,
 *    so `ls -Z` is an error rather than a silently ignored word. A flag that does nothing is
 *    indistinguishable from a flag that is not implemented, and the second one is a lie.
 *
 * The filesystem is `shared/fs/vfs.mjs`; its path helpers are imported rather than
 * re-derived, so `cd ../x` and `find ../x` cannot disagree about what that means.
 */

import {
  DIR, FILE, FORMAT_VERSION, FsError, LINK, PERM, basename, dirname, joinPath, normalize,
} from '../../shared/fs/vfs.mjs';
import { componentRegex } from './glob.mjs';
import { HOME, userName } from '../../shared/fs/seed.mjs';

/**
 * A command used wrongly, or a line that does not parse. Printed beside the command, never
 * thrown onward.
 *
 * It lives here rather than in `shell.mjs` so the imports run one way: the shell needs the
 * command table, and a table that needed the shell back would be a cycle for one class.
 */
export class UsageError extends Error {
  constructor(message) {
    super(message);
    this.name = 'UsageError';
  }
}

/**
 * The error a command may print beside itself and carry on past.
 *
 * Returns it, or rethrows anything that is not one — and the rethrow is the point:
 * `shell.mjs` prints it as `internal error` and puts the stack in the console. Absorbed into
 * a per-operand warn instead, a defect read exactly like a refusal from the filesystem.
 * `du: Cannot read properties of undefined` and `du: Permission denied` are the same line to
 * a reader, and only one of them is a fact about their files.
 *
 * @param {Error} error
 * @returns {UsageError|FsError}
 */
function refusal(error) {
  if (error instanceof FsError || error instanceof UsageError) return error;
  throw error;
}

/* ---------------------------------------------------------------------------
   Argument handling.
   --------------------------------------------------------------------------- */

/**
 * Parse flags off `context.argv`.
 *
 * @param {object} ctx
 * @param {object} spec
 * @param {string} [spec.bool]    Letters that are switches: `'laR'`.
 * @param {string} [spec.value]   Letters that take a value: `-n 5`, `-n5`.
 * @param {string[]} [spec.long]  Long options, without the dashes.
 * @param {string} [spec.numeric] The letter a bare `-5` means, as `head -5` does.
 * @param {number} [spec.max]     How many operands the command takes. Unlimited when
 *        omitted; past it, an error. A command that accepted `pwd /tmp` and printed the
 *        working directory anyway reported success for a line it did not carry out.
 * @returns {{flags: Set<string>, values: Record<string, string>, operands: string[]}}
 */
export function options(ctx, spec = {}) {
  const bool = new Set([...(spec.bool || '')]);
  const takesValue = new Set([...(spec.value || '')]);
  const long = spec.long || [];
  const flags = new Set();
  const values = {};
  const operands = [];
  const argv = ctx.argv;
  let i = 1;

  for (; i < argv.length; i += 1) {
    const word = argv[i];
    if (word === '--') {
      i += 1;
      break;
    }
    if (word.startsWith('--')) {
      const name = word.slice(2);
      if (!long.includes(name)) throw new UsageError(`unrecognised option '--${name}'`);
      flags.add(`--${name}`);
      continue;
    }
    // A lone `-` is an operand — the conventional name for standard input — not a flag.
    if (word.length > 1 && word.startsWith('-')) {
      const letters = word.slice(1);
      if (spec.numeric && /^\d+$/.test(letters)) {
        values[spec.numeric] = letters;
        continue;
      }
      for (let k = 0; k < letters.length; k += 1) {
        const letter = letters[k];
        if (takesValue.has(letter)) {
          const rest = letters.slice(k + 1);
          if (rest) {
            values[letter] = rest;
          } else {
            i += 1;
            if (i >= argv.length) {
              throw new UsageError(`option requires an argument -- '${letter}'`);
            }
            values[letter] = argv[i];
          }
          break;
        }
        if (!bool.has(letter)) throw new UsageError(`invalid option -- '${letter}'`);
        flags.add(letter);
      }
      continue;
    }
    operands.push(word);
  }
  operands.push(...argv.slice(i));
  if (spec.max !== undefined && operands.length > spec.max) {
    throw new UsageError(`extra operand '${operands[spec.max]}'`);
  }
  return { flags, values, operands };
}

function count(values, letter, fallback) {
  if (values[letter] === undefined) return fallback;
  const parsed = Number(values[letter]);
  if (!Number.isInteger(parsed) || parsed < 0) {
    throw new UsageError(`invalid number '${values[letter]}'`);
  }
  return parsed;
}

/** The lines of a text, without the empty one a trailing newline implies. */
function lines(text) {
  if (!text) return [];
  return text.replace(/\n$/, '').split('\n');
}

/**
 * The inputs a filter reads: its operands, or standard input when it has none.
 *
 * A file that cannot be read is reported and skipped rather than ending the command, which
 * is what `cat a missing b` does — `b` still prints.
 */
function readInputs(ctx, operands) {
  if (!operands.length) return { files: [{ name: '-', text: ctx.stdin }], failed: false };
  const files = [];
  let failed = false;
  for (const path of operands) {
    if (path === '-') {
      files.push({ name: '-', text: ctx.stdin });
      continue;
    }
    try {
      files.push({ name: path, text: ctx.fs.readFile(path, { cwd: ctx.shell.cwd }) });
    } catch (error) {
      ctx.warn(`${ctx.argv[0]}: ${refusal(error).message}`);
      failed = true;
    }
  }
  return { files, failed };
}

/* ---------------------------------------------------------------------------
   Rendering.
   --------------------------------------------------------------------------- */

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

/** `Nov  8 21:04`, the way `ls -l` writes a timestamp. */
function timestamp(ms) {
  const date = new Date(ms);
  const day = String(date.getDate()).padStart(2, ' ');
  const hh = String(date.getHours()).padStart(2, '0');
  const mm = String(date.getMinutes()).padStart(2, '0');
  return `${MONTHS[date.getMonth()]} ${day} ${hh}:${mm}`;
}

/** `1.2K`. Sizes are character counts; the filesystem holds text. */
function human(size) {
  if (size < 1024) return String(size);
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)}K`;
  return `${(size / (1024 * 1024)).toFixed(1)}M`;
}

/**
 * Lay names out in columns that fit the pane, filling down as `ls` does.
 *
 * Filling down rather than across is not cosmetic: a column-major layout keeps a sorted list
 * readable top-to-bottom, which is the order the names were sorted into.
 */
function columnize(names, columns) {
  if (!names.length) return '';
  const width = Math.max(...names.map((name) => name.length)) + 2;
  const perRow = Math.max(1, Math.floor(columns / width));
  if (perRow === 1) return `${names.join('\n')}\n`;
  const rows = Math.ceil(names.length / perRow);
  let out = '';
  for (let row = 0; row < rows; row += 1) {
    let line = '';
    for (let column = 0; column < perRow; column += 1) {
      const index = column * rows + row;
      if (index >= names.length) continue;
      line += names[index].padEnd(width);
    }
    out += `${line.trimEnd()}\n`;
  }
  return out;
}

/* ---------------------------------------------------------------------------
   Paths, as the commands need them.
   --------------------------------------------------------------------------- */

/** Resolve a path for display: absolute, lexically normalised, no filesystem access. */
function abs(ctx, path) {
  return normalize(path.startsWith('/') ? path : joinPath(ctx.shell.cwd, path));
}

//: The refusals that mean "where this symlink points cannot be examined". A caller that has
//: an answer without the target treats these as "stay with the link"; anything else is a
//: defect and must not be absorbed as one of them.
const UNREACHABLE = new Set(['ENOENT', 'ENOTDIR', 'EACCES', 'ELOOP']);

/**
 * `lstat`, with the one exception `ls` needs: an operand that is a symlink **to a
 * directory** is treated as that directory, so `ls links/relative-dir` lists what is in it.
 *
 * A symlink to a file stays the symlink. Following that one as well made `ls -l
 * links/absolute` print a plain file where the whole point of `links/` is to show that it is
 * not one — the listing denied the thing the directory exists to demonstrate.
 */
function operandStat(ctx, path) {
  const cwd = ctx.shell.cwd;
  const link = ctx.fs.lstat(path, { cwd });
  if (link.type !== LINK) return link;
  try {
    const target = ctx.fs.stat(path, { cwd });
    return target.type === DIR ? target : link;
  } catch (error) {
    // A broken link is still a name worth printing, which is why this is not an error.
    if (!UNREACHABLE.has(error.code)) throw error;
    return link;
  }
}

/* ---------------------------------------------------------------------------
   The table.
   --------------------------------------------------------------------------- */

export const COMMANDS = {
  /* ------------------------------ where am I ------------------------------ */

  pwd: {
    group: 'navigate',
    usage: 'pwd [-P]',
    summary: 'print the working directory; -P resolves every symlink',
    run(ctx) {
      const { flags } = options(ctx, { bool: 'P', max: 0 });
      // Two answers, and they differ inside a symlinked directory. The shell keeps the path
      // you walked; -P asks the filesystem where you actually are.
      ctx.line(flags.has('P') ? ctx.fs.realpath(ctx.shell.cwd) : ctx.shell.cwd);
      return 0;
    },
  },

  cd: {
    group: 'navigate',
    usage: 'cd [dir|-]',
    summary: 'change directory; no argument goes home, - goes back',
    run(ctx) {
      const { operands } = options(ctx, { max: 1 });
      let target = operands[0] || HOME;
      if (target === '-') {
        if (!ctx.shell.oldcwd) throw new UsageError('OLDPWD not set');
        target = ctx.shell.oldcwd;
        ctx.line(target);
      }
      // Checked on the path as typed, and only then stored normalised. Normalising first
      // cancelled `..` against the name before it lexically, so no intermediate component
      // was ever resolved: `cd /nope/..` and `cd /etc/motd/..` both succeeded while `ls` on
      // the same string refused, which is the disagreement this module's header rules out.
      const cwd = ctx.shell.cwd;
      const stat = ctx.fs.stat(target, { cwd });
      if (stat.type !== DIR) throw new UsageError(`${target}: Not a directory`);
      // Entering a directory is the execute bit, not the read bit: `--x` is a directory you
      // can stand in and cannot list. Asked here rather than discovered by the first `ls`.
      ctx.fs.access(target, PERM.X, { cwd });
      ctx.shell.oldcwd = cwd;
      // Logical, as `bash -L` keeps it: the path walked, not the path resolved. That is what
      // makes `pwd` and `pwd -P` two commands.
      ctx.shell.cwd = abs(ctx, target);
      return 0;
    },
  },

  ls: {
    group: 'navigate',
    usage: 'ls [-l] [-a] [-R] [-1] [-F] [-h] [-t] [-r] [path...]',
    summary: 'list a directory',
    run(ctx) {
      const { flags, operands } = options(ctx, { bool: 'laR1Fhtr' });
      const paths = operands.length ? operands : ['.'];
      let status = 0;

      const files = [];
      const dirs = [];
      for (const path of paths) {
        try {
          const stat = operandStat(ctx, path);
          if (stat.type === DIR) dirs.push(path);
          else files.push({ name: path, stat });
        } catch (error) {
          ctx.warn(`ls: ${error.message}`);
          status = 1;
        }
      }

      const header = files.length + dirs.length > 1 || flags.has('R');
      if (files.length) ctx.write(render(ctx, sortEntries(files, flags), flags));

      let first = !files.length;
      for (const dir of dirs) {
        const queue = [dir];
        while (queue.length) {
          const current = queue.shift();
          let entries;
          try {
            entries = ctx.fs.list(current, { cwd: ctx.shell.cwd }).map((entry) => ({
              name: entry.name,
              stat: entry,
            }));
          } catch (error) {
            ctx.warn(`ls: ${error.message}`);
            status = 1;
            continue;
          }
          if (!flags.has('a')) entries = entries.filter((entry) => !entry.name.startsWith('.'));
          else {
            // Resolved physically, because these two name the directory being listed and
            // its real parent. Lexically, `.` inside a symlinked directory reported the
            // symlink — a listing of a directory in which the directory itself appeared as
            // a link to somewhere else.
            const here = ctx.fs.realpath(current, { cwd: ctx.shell.cwd });
            entries.unshift(
              { name: '..', stat: ctx.fs.lstat(ctx.fs.realpath(`${here}/..`)) },
              { name: '.', stat: ctx.fs.lstat(here) },
            );
          }
          if (header) ctx.write(`${first ? '' : '\n'}${current}:\n`);
          first = false;
          ctx.write(render(ctx, sortEntries(entries, flags), flags));
          if (flags.has('R')) {
            // Symlinked directories are not descended: a link to an ancestor would make the
            // recursion endless, and `ls -R` on a real filesystem does not follow them.
            const children = entries
              .filter((entry) => entry.stat.type === DIR && entry.name !== '.' && entry.name !== '..')
              .map((entry) => (current === '/' ? `/${entry.name}` : `${current}/${entry.name}`));
            queue.unshift(...children);
          }
        }
      }
      return status;
    },
  },

  tree: {
    group: 'navigate',
    usage: 'tree [-a] [-L depth] [dir]',
    summary: 'the directory as a tree',
    run(ctx) {
      const { flags, values, operands } = options(ctx, { bool: 'a', value: 'L', max: 1 });
      const root = operands[0] || '.';
      const maxDepth = values.L === undefined ? Infinity : count(values, 'L', Infinity);
      const stat = ctx.fs.lstat(root, { cwd: ctx.shell.cwd });
      ctx.line(root);
      if (stat.type !== DIR) return 0;

      let dirs = 0;
      let files = 0;
      const walk = (path, prefix, depth) => {
        if (depth > maxDepth) return;
        let entries = ctx.fs.list(path, { cwd: ctx.shell.cwd });
        if (!flags.has('a')) entries = entries.filter((entry) => !entry.name.startsWith('.'));
        entries.sort((a, b) => (a.name < b.name ? -1 : a.name > b.name ? 1 : 0));
        entries.forEach((entry, index) => {
          const last = index === entries.length - 1;
          const label = entry.type === LINK ? `${entry.name} -> ${entry.target}` : entry.name;
          ctx.line(`${prefix}${last ? '└── ' : '├── '}${label}`);
          if (entry.type === DIR) {
            dirs += 1;
            walk(
              joinPath(path, entry.name),
              `${prefix}${last ? '    ' : '│   '}`,
              depth + 1,
            );
          } else {
            files += 1;
          }
        });
      };
      walk(root, '', 1);
      ctx.line('');
      ctx.line(`${dirs} ${dirs === 1 ? 'directory' : 'directories'}, ${files} ${files === 1 ? 'file' : 'files'}`);
      return 0;
    },
  },

  stat: {
    group: 'navigate',
    usage: 'stat [-L] path...',
    summary: 'everything the filesystem knows about a name; -L follows symlinks',
    run(ctx) {
      const { flags, operands } = options(ctx, { bool: 'L' });
      if (!operands.length) throw new UsageError('stat needs a path');
      let status = 0;
      for (const path of operands) {
        try {
          const cwd = ctx.shell.cwd;
          const stat = flags.has('L')
            ? ctx.fs.stat(path, { cwd })
            : ctx.fs.lstat(path, { cwd });
          ctx.line(`  File: ${path}${stat.type === LINK ? ` -> ${stat.target}` : ''}`);
          ctx.line(`  Size: ${stat.size}\tType: ${stat.type}\tInode: ${stat.ino}\tLinks: ${stat.nlink}`);
          ctx.line(`Access: (${(stat.mode & 0o7777).toString(8).padStart(4, '0')}/${stat.modeString})  Uid: (${stat.uid}/${userName(stat.uid)})  Gid: (${stat.gid}/${userName(stat.gid)})`);
          ctx.line(`Modify: ${new Date(stat.mtime).toISOString()}`);
        } catch (error) {
          ctx.warn(`stat: ${refusal(error).message}`);
          status = 1;
        }
      }
      return status;
    },
  },

  file: {
    group: 'navigate',
    usage: 'file path...',
    summary: 'what a name holds',
    run(ctx) {
      const { operands } = options(ctx, {});
      if (!operands.length) throw new UsageError('file needs a path');
      let status = 0;
      for (const path of operands) {
        try {
          const stat = ctx.fs.lstat(path, { cwd: ctx.shell.cwd });
          let kind;
          if (stat.type === DIR) kind = 'directory';
          else if (stat.type === LINK) kind = `symbolic link to ${stat.target}`;
          else if (stat.size === 0) kind = 'empty';
          else kind = `ASCII text, ${lines(ctx.fs.readFile(path, { cwd: ctx.shell.cwd })).length} lines`;
          ctx.line(`${path}: ${kind}`);
        } catch (error) {
          ctx.warn(`file: ${refusal(error).message}`);
          status = 1;
        }
      }
      return status;
    },
  },

  du: {
    group: 'navigate',
    usage: 'du [-s] [-h] [path...]',
    summary: 'size of a tree in characters',
    run(ctx) {
      const { flags, operands } = options(ctx, { bool: 'sh' });
      const paths = operands.length ? operands : ['.'];
      let status = 0;
      for (const path of paths) {
        try {
          const totals = new Map();
          let own = 0;
          for (const entry of ctx.fs.walk(path, { cwd: ctx.shell.cwd })) {
            if (entry.error) {
              ctx.warn(`du: ${entry.error.message}`);
              status = 1;
              continue;
            }
            if (entry.stat.type === DIR) {
              if (!totals.has(entry.path)) totals.set(entry.path, 0);
              continue;
            }
            own += entry.stat.size;
            // Charged to every ancestor inside the walk, which is what makes a directory's
            // figure the size of its subtree rather than of its own entries.
            for (const dir of totals.keys()) {
              if (entry.path.startsWith(dir === '/' ? '/' : `${dir}/`)) {
                totals.set(dir, totals.get(dir) + entry.stat.size);
              }
            }
          }
          const show = (size, name) => ctx.line(`${flags.has('h') ? human(size) : size}\t${name}`);
          if (totals.size === 0) {
            show(own, path);
          } else if (flags.has('s')) {
            show(totals.get(abs(ctx, path)) ?? own, path);
          } else {
            for (const [dir, size] of [...totals.entries()].sort()) show(size, dir);
          }
        } catch (error) {
          ctx.warn(`du: ${refusal(error).message}`);
          status = 1;
        }
      }
      return status;
    },
  },

  df: {
    group: 'navigate',
    usage: 'df [-h]',
    summary: 'how much of the filesystem is used, and whether it is being saved',
    async run(ctx) {
      const { flags } = options(ctx, { bool: 'h', max: 0 });
      const stats = ctx.fs.stats();
      const size = (value) => (flags.has('h') ? human(value) : String(value));
      const percent = Math.round((stats.used / stats.total) * 100);
      ctx.line(`${'Filesystem'.padEnd(12)}${'Size'.padStart(9)}${'Used'.padStart(10)}${'Avail'.padStart(10)}  Use%  Mounted on`);
      ctx.line(
        `${'webxp-vfs'.padEnd(12)}${size(stats.total).padStart(9)}${size(stats.used).padStart(10)}`
        + `${size(stats.total - stats.used).padStart(10)}  ${String(percent).padStart(3)}%  /`,
      );
      ctx.line(`${stats.dirs} directories, ${stats.files} files, ${stats.links} symlinks, ${stats.inodes} inodes`);
      // The persistence state belongs with the free space: both answer "will this still be
      // here tomorrow". Imported here rather than at the top so the command table stays
      // loadable without a mount.
      const { persistence } = await import('../../shared/fs/fs.mjs');
      if (persistence.error) ctx.line(`persistence: off — ${persistence.error}`);
      else {
        const last = persistence.lastSaved ? `, last saved ${timestamp(persistence.lastSaved)}` : '';
        ctx.line(`persistence: ${persistence.key} (${persistence.source}), ${persistence.saves} saves${last}`);
      }
      return 0;
    },
  },

  /* ------------------------------ reading ------------------------------ */

  cat: {
    group: 'read',
    usage: 'cat [-n] [file...]',
    summary: 'print a file; no file reads the pipe',
    run(ctx) {
      const { flags, operands } = options(ctx, { bool: 'n' });
      const { files, failed } = readInputs(ctx, operands);
      let number = 0;
      for (const file of files) {
        if (!flags.has('n')) {
          ctx.write(file.text);
          continue;
        }
        for (const line of lines(file.text)) {
          number += 1;
          ctx.line(`${String(number).padStart(6)}\t${line}`);
        }
      }
      return failed ? 1 : 0;
    },
  },

  head: {
    group: 'read',
    usage: 'head [-n lines] [file...]',
    summary: 'the first lines',
    run(ctx) {
      const { values, operands } = options(ctx, { value: 'n', numeric: 'n' });
      const limit = count(values, 'n', 10);
      const { files, failed } = readInputs(ctx, operands);
      files.forEach((file, index) => {
        if (files.length > 1) ctx.line(`${index ? '\n' : ''}==> ${file.name} <==`);
        for (const line of lines(file.text).slice(0, limit)) ctx.line(line);
      });
      return failed ? 1 : 0;
    },
  },

  tail: {
    group: 'read',
    usage: 'tail [-n lines] [file...]',
    summary: 'the last lines',
    run(ctx) {
      const { values, operands } = options(ctx, { value: 'n', numeric: 'n' });
      const limit = count(values, 'n', 10);
      const { files, failed } = readInputs(ctx, operands);
      files.forEach((file, index) => {
        if (files.length > 1) ctx.line(`${index ? '\n' : ''}==> ${file.name} <==`);
        const all = lines(file.text);
        for (const line of all.slice(Math.max(0, all.length - limit))) ctx.line(line);
      });
      return failed ? 1 : 0;
    },
  },

  wc: {
    group: 'read',
    usage: 'wc [-l] [-w] [-c] [file...]',
    summary: 'count lines, words, characters',
    run(ctx) {
      const { flags, operands } = options(ctx, { bool: 'lwc' });
      const { files, failed } = readInputs(ctx, operands);
      const want = flags.size ? flags : new Set(['l', 'w', 'c']);
      const totals = { l: 0, w: 0, c: 0 };
      const row = (counts, name) => {
        const parts = [];
        for (const letter of ['l', 'w', 'c']) {
          if (want.has(letter)) parts.push(String(counts[letter]).padStart(7));
        }
        ctx.line(`${parts.join('')}${name === '-' ? '' : ` ${name}`}`);
      };
      for (const file of files) {
        const counts = {
          l: lines(file.text).length,
          w: file.text.split(/\s+/).filter(Boolean).length,
          c: file.text.length,
        };
        for (const letter of ['l', 'w', 'c']) totals[letter] += counts[letter];
        row(counts, file.name);
      }
      if (files.length > 1) row(totals, 'total');
      return failed ? 1 : 0;
    },
  },

  grep: {
    group: 'read',
    usage: 'grep [-i] [-n] [-v] [-c] [-l] [-r] pattern [file...]',
    summary: 'lines matching a pattern; the pattern is a regular expression',
    run(ctx) {
      const { flags, operands } = options(ctx, { bool: 'invcrl' });
      if (!operands.length) throw new UsageError('grep needs a pattern');
      const [source, ...rest] = operands;
      let regex;
      try {
        regex = new RegExp(source, flags.has('i') ? 'i' : '');
      } catch (error) {
        // `new RegExp` throws a SyntaxError for a pattern that will not compile, which is
        // the reader's mistake. Anything else is not, and must not be reported as one.
        if (!(error instanceof SyntaxError)) throw error;
        throw new UsageError(`bad pattern: ${error.message}`);
      }

      const targets = [];
      let status = 1;
      let failed = false;
      if (!rest.length) {
        targets.push({ name: '-', text: ctx.stdin });
      } else {
        for (const path of rest) {
          try {
            const stat = ctx.fs.stat(path, { cwd: ctx.shell.cwd });
            if (stat.type === DIR) {
              if (!flags.has('r')) {
                ctx.warn(`grep: ${path}: Is a directory`);
                failed = true;
                continue;
              }
              for (const entry of ctx.fs.walk(path, { cwd: ctx.shell.cwd })) {
                if (entry.error || entry.stat.type !== FILE) continue;
                try {
                  targets.push({ name: entry.path, text: ctx.fs.readFile(entry.path) });
                } catch (error) {
                  ctx.warn(`grep: ${refusal(error).message}`);
                  failed = true;
                }
              }
              continue;
            }
            targets.push({ name: path, text: ctx.fs.readFile(path, { cwd: ctx.shell.cwd }) });
          } catch (error) {
            ctx.warn(`grep: ${refusal(error).message}`);
            failed = true;
          }
        }
      }

      const label = targets.length > 1 || flags.has('r');
      for (const target of targets) {
        const hits = [];
        lines(target.text).forEach((line, index) => {
          const matched = regex.test(line);
          if (matched === flags.has('v')) return;
          hits.push({ line, number: index + 1 });
        });
        if (hits.length) status = 0;
        if (flags.has('l')) {
          if (hits.length) ctx.line(target.name);
          continue;
        }
        if (flags.has('c')) {
          ctx.line(label ? `${target.name}:${hits.length}` : String(hits.length));
          continue;
        }
        for (const hit of hits) {
          const prefix = label ? `${target.name}:` : '';
          ctx.line(`${prefix}${flags.has('n') ? `${hit.number}:` : ''}${hit.line}`);
        }
      }
      return failed ? 2 : status;
    },
  },

  sort: {
    group: 'read',
    usage: 'sort [-r] [-n] [-u] [file...]',
    summary: 'sort lines',
    run(ctx) {
      const { flags, operands } = options(ctx, { bool: 'rnu' });
      const { files, failed } = readInputs(ctx, operands);
      let all = files.flatMap((file) => lines(file.text));
      all.sort((a, b) => {
        if (flags.has('n')) return (Number.parseFloat(a) || 0) - (Number.parseFloat(b) || 0);
        return a < b ? -1 : a > b ? 1 : 0;
      });
      if (flags.has('r')) all.reverse();
      if (flags.has('u')) all = all.filter((line, index) => index === 0 || line !== all[index - 1]);
      for (const line of all) ctx.line(line);
      return failed ? 1 : 0;
    },
  },

  uniq: {
    group: 'read',
    usage: 'uniq [-c] [file]',
    summary: 'collapse repeated adjacent lines',
    run(ctx) {
      const { flags, operands } = options(ctx, { bool: 'c', max: 1 });
      const { files, failed } = readInputs(ctx, operands);
      const all = files.flatMap((file) => lines(file.text));
      let index = 0;
      while (index < all.length) {
        let run = 1;
        while (all[index + run] === all[index]) run += 1;
        ctx.line(flags.has('c') ? `${String(run).padStart(7)} ${all[index]}` : all[index]);
        index += run;
      }
      return failed ? 1 : 0;
    },
  },

  find: {
    group: 'read',
    usage: 'find [path...] [-name pat] [-iname pat] [-type f|d|l] [-maxdepth n] [-mindepth n] [-size ±n] [-empty]',
    summary: 'walk a tree, printing what matches',
    run(ctx) {
      const argv = ctx.argv.slice(1);
      const paths = [];
      const tests = [];
      let maxDepth = Infinity;
      let minDepth = 0;
      let index = 0;

      while (index < argv.length && !argv[index].startsWith('-')) {
        paths.push(argv[index]);
        index += 1;
      }
      for (; index < argv.length; index += 1) {
        const predicate = argv[index];
        const argument = () => {
          index += 1;
          if (index >= argv.length) throw new UsageError(`missing argument to ${predicate}`);
          return argv[index];
        };
        switch (predicate) {
          case '-name': {
            const regex = componentRegex(argument());
            tests.push((entry) => regex.test(entry.name));
            break;
          }
          case '-iname': {
            const regex = componentRegex(argument(), { ignoreCase: true });
            tests.push((entry) => regex.test(entry.name));
            break;
          }
          case '-type': {
            const letter = argument();
            const want = { f: FILE, d: DIR, l: LINK }[letter];
            if (!want) throw new UsageError(`unknown type '${letter}' — use f, d or l`);
            tests.push((entry) => entry.stat.type === want);
            break;
          }
          case '-maxdepth':
            maxDepth = Number(argument());
            if (!Number.isInteger(maxDepth) || maxDepth < 0) throw new UsageError('-maxdepth needs a whole number');
            break;
          case '-mindepth':
            minDepth = Number(argument());
            if (!Number.isInteger(minDepth) || minDepth < 0) throw new UsageError('-mindepth needs a whole number');
            break;
          case '-size': {
            // Characters, not blocks: this filesystem stores text, and reporting 512-byte
            // blocks for a 40-character file would be a unit nobody here can check.
            const spec = argument();
            const bound = Number.parseInt(spec.replace(/^[+-]/, ''), 10);
            if (!Number.isInteger(bound)) throw new UsageError(`-size needs a number, got '${spec}'`);
            if (spec.startsWith('+')) tests.push((entry) => entry.stat.size > bound);
            else if (spec.startsWith('-')) tests.push((entry) => entry.stat.size < bound);
            else tests.push((entry) => entry.stat.size === bound);
            break;
          }
          case '-empty':
            tests.push((entry) => entry.stat.size === 0);
            break;
          case '-print':
            break;
          default:
            throw new UsageError(`unknown predicate '${predicate}'`);
        }
      }

      let status = 0;
      for (const path of paths.length ? paths : ['.']) {
        let start;
        try {
          start = abs(ctx, path);
          ctx.fs.lstat(start);
        } catch (error) {
          ctx.warn(`find: ${refusal(error).message}`);
          status = 1;
          continue;
        }
        for (const entry of ctx.fs.walk(start, { maxDepth })) {
          if (entry.error) {
            ctx.warn(`find: ${entry.error.message}`);
            status = 1;
            continue;
          }
          if (entry.depth < minDepth) continue;
          if (!tests.every((test) => test(entry))) continue;
          // Printed under the name the user typed, the way find does: `find .` prints
          // `./notes`, not `/home/user/notes`. The root is the special case — its own path
          // is one character long, so slicing by its length would eat the separator.
          const suffix = entry.path.slice(start === '/' ? 0 : start.length);
          const stem = start === '/' ? '' : path.replace(/\/$/, '');
          ctx.line(`${stem}${suffix}` || path);
        }
      }
      return status;
    },
  },

  /* ------------------------------ writing ------------------------------ */

  touch: {
    group: 'write',
    usage: 'touch file...',
    summary: 'create a file, or move its timestamp forward',
    run(ctx) {
      const { operands } = options(ctx, {});
      if (!operands.length) throw new UsageError('touch needs a file');
      let status = 0;
      for (const path of operands) {
        try {
          ctx.fs.touch(path, { cwd: ctx.shell.cwd });
        } catch (error) {
          ctx.warn(`touch: ${refusal(error).message}`);
          status = 1;
        }
      }
      return status;
    },
  },

  mkdir: {
    group: 'write',
    usage: 'mkdir [-p] dir...',
    summary: 'make a directory; -p makes the parents too',
    run(ctx) {
      const { flags, operands } = options(ctx, { bool: 'p' });
      if (!operands.length) throw new UsageError('mkdir needs a name');
      let status = 0;
      for (const path of operands) {
        try {
          ctx.fs.mkdir(path, { cwd: ctx.shell.cwd, recursive: flags.has('p') });
        } catch (error) {
          ctx.warn(`mkdir: ${refusal(error).message}`);
          status = 1;
        }
      }
      return status;
    },
  },

  rmdir: {
    group: 'write',
    usage: 'rmdir dir...',
    summary: 'remove an empty directory',
    run(ctx) {
      const { operands } = options(ctx, {});
      if (!operands.length) throw new UsageError('rmdir needs a name');
      let status = 0;
      for (const path of operands) {
        try {
          ctx.fs.rmdir(path, { cwd: ctx.shell.cwd });
        } catch (error) {
          ctx.warn(`rmdir: ${refusal(error).message}`);
          status = 1;
        }
      }
      return status;
    },
  },

  rm: {
    group: 'write',
    usage: 'rm [-r] [-f] path...',
    summary: 'remove a name; -r for a directory',
    run(ctx) {
      const { flags, operands } = options(ctx, { bool: 'rf' });
      if (!operands.length && !flags.has('f')) throw new UsageError('rm needs a path');
      let status = 0;
      const remove = (path) => {
        // The root is refused before anything is read, the way GNU `rm` does with
        // `--preserve-root`. Without it `rm -rf /` emptied the filesystem and then reported
        // a child it happened to fail on last, naming a path the reader never typed.
        if (abs(ctx, path) === '/') {
          const typed = path === '/' ? "'/'" : `'${path}', which is '/'`;
          throw new UsageError(`it is dangerous to operate recursively on ${typed}`);
        }
        const stat = ctx.fs.lstat(path, { cwd: ctx.shell.cwd });
        // A symlink is removed as itself, never followed. `rm links/relative-dir` takes the
        // link; without this it would take the directory it points at.
        if (stat.type !== DIR) {
          ctx.fs.unlink(path, { cwd: ctx.shell.cwd });
          return;
        }
        if (!flags.has('r')) throw new UsageError(`${path}: Is a directory`);
        for (const name of ctx.fs.readdir(path, { cwd: ctx.shell.cwd })) {
          remove(joinPath(path, name));
        }
        ctx.fs.rmdir(path, { cwd: ctx.shell.cwd });
      };
      for (const path of operands) {
        try {
          remove(path);
        } catch (error) {
          const reason = refusal(error);
          if (flags.has('f') && reason.code === 'ENOENT') continue;
          ctx.warn(`rm: ${reason.message}`);
          status = 1;
        }
      }
      return status;
    },
  },

  cp: {
    group: 'write',
    usage: 'cp [-r] source... dest',
    summary: 'copy',
    run(ctx) {
      const { flags, operands } = options(ctx, { bool: 'r' });
      if (operands.length < 2) throw new UsageError('cp needs a source and a destination');
      const dest = operands.pop();
      const cwd = ctx.shell.cwd;
      const destIsDir = ctx.fs.exists(dest, { cwd }) && ctx.fs.stat(dest, { cwd }).type === DIR;
      if (operands.length > 1 && !destIsDir) {
        throw new UsageError(`${dest}: Not a directory`);
      }
      let status = 0;
      const copy = (from, to) => {
        const stat = ctx.fs.stat(from, { cwd });
        if (stat.type !== DIR) {
          ctx.fs.copyFile(from, to, { cwd });
          return;
        }
        if (!flags.has('r')) throw new UsageError(`${from}: Is a directory`);
        // A directory copied into its own subtree never terminates: the copy creates a new
        // child, the walk reaches it, and it copies that too. Resolved on both sides first,
        // because `cp -r a /tmp/link-to-a/b` is the same mistake spelled differently.
        const source = ctx.fs.realpath(from, { cwd });
        const destination = joinPath(
          ctx.fs.realpath(dirname(abs(ctx, to)), { cwd }),
          basename(abs(ctx, to)),
        );
        if (destination === source || destination.startsWith(`${source}/`)) {
          throw new UsageError(`cannot copy '${from}' into itself, '${to}'`);
        }
        if (!ctx.fs.exists(to, { cwd })) ctx.fs.mkdir(to, { cwd, mode: stat.mode });
        for (const name of ctx.fs.readdir(from, { cwd })) {
          copy(`${from}/${name}`, `${to}/${name}`);
        }
      };
      for (const source of operands) {
        try {
          const target = destIsDir ? joinPath(dest, basename(source)) : dest;
          copy(source, target);
        } catch (error) {
          ctx.warn(`cp: ${refusal(error).message}`);
          status = 1;
        }
      }
      return status;
    },
  },

  mv: {
    group: 'write',
    usage: 'mv source... dest',
    summary: 'move or rename',
    run(ctx) {
      const { operands } = options(ctx, {});
      if (operands.length < 2) throw new UsageError('mv needs a source and a destination');
      const dest = operands.pop();
      const cwd = ctx.shell.cwd;
      const destIsDir = ctx.fs.exists(dest, { cwd }) && ctx.fs.stat(dest, { cwd }).type === DIR;
      if (operands.length > 1 && !destIsDir) throw new UsageError(`${dest}: Not a directory`);
      let status = 0;
      for (const source of operands) {
        try {
          // The basename of the resolved source: `basename('.')` is not a name, and
          // `joinPath(dest, '/')` is the root, so `mv . dest` aimed at `/`.
          const target = destIsDir ? joinPath(dest, basename(abs(ctx, source))) : dest;
          ctx.fs.rename(source, target, { cwd });
        } catch (error) {
          ctx.warn(`mv: ${refusal(error).message}`);
          status = 1;
        }
      }
      return status;
    },
  },

  ln: {
    group: 'write',
    usage: 'ln [-s] target name',
    summary: 'a second name for a file (-s: a file holding a path)',
    run(ctx) {
      const { flags, operands } = options(ctx, { bool: 's' });
      if (operands.length !== 2) throw new UsageError('ln needs a target and a name');
      const [target, name] = operands;
      if (flags.has('s')) ctx.fs.symlink(target, name, { cwd: ctx.shell.cwd });
      else ctx.fs.link(target, name, { cwd: ctx.shell.cwd });
      return 0;
    },
  },

  readlink: {
    group: 'write',
    usage: 'readlink path...',
    summary: 'the path a symlink holds',
    run(ctx) {
      const { operands } = options(ctx, {});
      if (!operands.length) throw new UsageError('readlink needs a path');
      let status = 0;
      for (const path of operands) {
        try {
          ctx.line(ctx.fs.readlink(path, { cwd: ctx.shell.cwd }));
        } catch (error) {
          ctx.warn(`readlink: ${refusal(error).message}`);
          status = 1;
        }
      }
      return status;
    },
  },

  realpath: {
    group: 'write',
    usage: 'realpath path...',
    summary: 'the path with every symlink resolved',
    run(ctx) {
      const { operands } = options(ctx, {});
      if (!operands.length) throw new UsageError('realpath needs a path');
      let status = 0;
      for (const path of operands) {
        try {
          ctx.line(ctx.fs.realpath(path, { cwd: ctx.shell.cwd }));
        } catch (error) {
          ctx.warn(`realpath: ${refusal(error).message}`);
          status = 1;
        }
      }
      return status;
    },
  },

  chmod: {
    group: 'write',
    usage: 'chmod octal path...',
    summary: 'set the permission bits, as an octal number',
    run(ctx) {
      const { operands } = options(ctx, {});
      if (operands.length < 2) throw new UsageError('chmod needs a mode and a path');
      const [spec, ...paths] = operands;
      if (!/^[0-7]{3,4}$/.test(spec)) {
        // Symbolic modes (`u+x`) are not accepted rather than half-accepted: a parser that
        // took `u+x` and ignored `g-w` would be worse than one that takes neither.
        throw new UsageError(`invalid mode '${spec}' — octal only, e.g. 644`);
      }
      const mode = Number.parseInt(spec, 8);
      let status = 0;
      for (const path of paths) {
        try {
          ctx.fs.chmod(path, mode, { cwd: ctx.shell.cwd });
        } catch (error) {
          ctx.warn(`chmod: ${refusal(error).message}`);
          status = 1;
        }
      }
      return status;
    },
  },

  echo: {
    group: 'write',
    usage: 'echo [-n] word...',
    summary: 'print the words',
    run(ctx) {
      // The one command that must not use `options()`. Everything after the command name is
      // a word, including a word that starts with a dash: through the general parser, `echo
      // -5` answered `invalid option -- '5'` where a shell prints `-5`.
      const words = ctx.argv.slice(1);
      const newline = words[0] !== '-n';
      if (!newline) words.shift();
      ctx.write(words.join(' ') + (newline ? '\n' : ''));
      return 0;
    },
  },

  /* ------------------------------ the session ------------------------------ */

  help: {
    group: 'session',
    usage: 'help [command]',
    summary: 'this list, or one command in detail',
    run(ctx) {
      const { operands } = options(ctx, { max: 1 });
      if (operands.length) {
        const name = operands[0];
        const command = COMMANDS[name];
        if (!command) throw new UsageError(`no help for '${name}'`);
        ctx.line(command.usage);
        ctx.line(`  ${command.summary}`);
        return 0;
      }
      const groups = { navigate: 'Where you are', read: 'Reading', write: 'Writing', session: 'The session' };
      const width = Math.max(...Object.keys(COMMANDS).map((name) => name.length));
      for (const [group, title] of Object.entries(groups)) {
        ctx.line(`${title}:`);
        for (const [name, command] of Object.entries(COMMANDS)) {
          if (command.group !== group) continue;
          ctx.line(`  ${name.padEnd(width)}  ${command.summary}`);
        }
        ctx.line('');
      }
      ctx.line("Pipes, < > >>, globs, quotes and Tab completion all work. 'help <command>' for usage.");
      return 0;
    },
  },

  history: {
    group: 'session',
    usage: 'history',
    summary: 'the lines this window has run',
    run(ctx) {
      options(ctx, { max: 0 });
      ctx.shell.history.forEach((line, index) => {
        ctx.line(`${String(index + 1).padStart(5)}  ${line}`);
      });
      return 0;
    },
  },

  clear: {
    group: 'session',
    usage: 'clear',
    summary: 'empty the pane',
    run(ctx) {
      options(ctx, { max: 0 });
      ctx.control('clear');
      return 0;
    },
  },

  exit: {
    group: 'session',
    usage: 'exit',
    summary: 'close this window',
    run(ctx) {
      options(ctx, { max: 0 });
      ctx.control('exit');
      return 0;
    },
  },

  whoami: {
    group: 'session',
    usage: 'whoami',
    summary: 'the user the filesystem checks permissions against',
    run(ctx) {
      options(ctx, { max: 0 });
      ctx.line(userName(ctx.fs.uid));
      return 0;
    },
  },

  date: {
    group: 'session',
    usage: 'date',
    summary: 'the time, from the machine running the browser',
    run(ctx) {
      options(ctx, { max: 0 });
      const now = new Date();
      ctx.line(`${now.toDateString()} ${now.toTimeString()}`);
      return 0;
    },
  },

  uname: {
    group: 'session',
    usage: 'uname [-a]',
    summary: 'what this is',
    run(ctx) {
      const { flags } = options(ctx, { bool: 'a', max: 0 });
      // The filesystem's format version, read from the filesystem rather than written out
      // here: `vfs-1` beside a `FORMAT_VERSION` of 2 is a version report that is wrong.
      ctx.line(flags.has('a')
        ? `webxp ${ctx.shell.host} vfs-${FORMAT_VERSION} shared/fs/vfs.mjs ${navigator.userAgent}`
        : 'webxp');
      return 0;
    },
  },

  ps: {
    group: 'session',
    usage: 'ps',
    summary: 'the open windows',
    run(ctx) {
      options(ctx, { max: 0 });
      if (!ctx.desktop) throw new UsageError('no desktop to ask');
      ctx.line('PID      PROGRAM         STATE  TITLE');
      for (const win of ctx.desktop.windows) {
        ctx.line(
          `${win.id.padEnd(8)} ${String(win.programId).padEnd(15)} ${win.state.padEnd(6)} ${win.titleText}`,
        );
      }
      return 0;
    },
  },

  kill: {
    group: 'session',
    usage: 'kill pid',
    summary: 'close a window by the PID ps prints',
    run(ctx) {
      const { operands } = options(ctx, {});
      if (!ctx.desktop) throw new UsageError('no desktop to ask');
      if (!operands.length) throw new UsageError('kill needs a PID');
      let status = 0;
      for (const pid of operands) {
        const win = ctx.desktop.windows.find(
          (each) => each.id === pid || each.id === `win-${pid}`,
        );
        if (!win) {
          ctx.warn(`kill: ${pid}: no such window`);
          status = 1;
          continue;
        }
        win.close();
      }
      return status;
    },
  },

  open: {
    group: 'session',
    usage: 'open path',
    summary: 'open a file in Notepad, or a directory in a new terminal',
    async run(ctx) {
      const { operands } = options(ctx, {});
      if (!ctx.desktop) throw new UsageError('no desktop to open anything on');
      if (operands.length !== 1) throw new UsageError('open takes one path');
      const path = operands[0];
      // Resolved to a physical absolute path before it is handed over: the other window has
      // its own working directory, and `open ../notes/todo.txt` has to mean this one's.
      const real = ctx.fs.realpath(path, { cwd: ctx.shell.cwd });
      const stat = ctx.fs.stat(real);
      if (stat.type === DIR) {
        await ctx.desktop.launch('terminal', { cwd: real });
        ctx.line(`opened a terminal in ${real}`);
        return 0;
      }
      // Readability is checked here rather than left to Notepad, so a refusal arrives as a
      // shell error beside the command instead of as a window that failed to start.
      ctx.fs.readFile(real);
      await ctx.desktop.launch('notepad', { path: real });
      ctx.line(`opened ${real} in Notepad`);
      return 0;
    },
  },

  format: {
    group: 'session',
    usage: 'format --yes',
    summary: 'put the seed filesystem back, discarding everything written',
    async run(ctx) {
      const { flags } = options(ctx, { long: ['yes'], max: 0 });
      if (!flags.has('--yes')) {
        ctx.warn('format: this discards every file. Run "format --yes" if that is what you want.');
        return 1;
      }
      const { resetFileSystem } = await import('../../shared/fs/fs.mjs');
      await resetFileSystem();
      ctx.shell.cwd = HOME;
      ctx.line('filesystem reset to the seed tree');
      return 0;
    },
  },
};

/* ---------------------------------------------------------------------------
   `ls`, which is big enough to live below the table.
   --------------------------------------------------------------------------- */

function sortEntries(entries, flags) {
  const sorted = [...entries].sort((a, b) => {
    if (flags.has('t')) return b.stat.mtime - a.stat.mtime;
    return a.name < b.name ? -1 : a.name > b.name ? 1 : 0;
  });
  if (flags.has('r')) sorted.reverse();
  return sorted;
}

function classify(stat) {
  if (stat.type === DIR) return '/';
  if (stat.type === LINK) return '@';
  if (stat.mode & 0o111) return '*';
  return '';
}

function render(ctx, entries, flags) {
  if (!entries.length) return '';
  if (flags.has('l')) {
    const sizes = Math.max(...entries.map((entry) => String(entry.stat.size).length));
    const links = Math.max(...entries.map((entry) => String(entry.stat.nlink).length));
    return entries.map((entry) => {
      const stat = entry.stat;
      const size = flags.has('h') ? human(stat.size) : String(stat.size);
      const arrow = stat.type === LINK ? ` -> ${stat.target}` : '';
      return `${stat.modeString} ${String(stat.nlink).padStart(links)} `
        + `${userName(stat.uid)} ${userName(stat.gid)} ${size.padStart(sizes)} `
        + `${timestamp(stat.mtime)} ${entry.name}${arrow}\n`;
    }).join('');
  }
  const names = entries.map((entry) => entry.name + (flags.has('F') ? classify(entry.stat) : ''));
  // One per line when nobody is reading it. Columns are for a pane of a known width; a pipe
  // has no width, and `ls | wc -l` has to count names rather than rows.
  if (flags.has('1') || !ctx.tty) return `${names.join('\n')}\n`;
  return columnize(names, ctx.columns);
}
