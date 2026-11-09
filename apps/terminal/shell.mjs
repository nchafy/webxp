/* The shell: a line in, a pipeline out, and the state a session carries.
 *
 * Split from `commands.mjs` on purpose. This module knows about words, quotes, globs, pipes
 * and redirection and nothing about what any command does; `commands.mjs` knows what the
 * commands do and never sees a raw line. The parser is the part with edge cases, and it is
 * the part that can be reasoned about on its own.
 *
 * ## What the language is
 *
 *     cmd arg 'quoted arg' "also quoted" | cmd2 -n 3 > file
 *     cmd < file >> other
 *
 * Quotes, backslash escapes, globs (`*`, `?`, `[...]`), pipelines, `<`, `>`, `>>`, and `#`
 * comments. There are no variables and no expansion of any kind beyond globs and a leading
 * `~`: a `$` is a dollar sign. That is a deliberate floor rather than an unfinished feature —
 * see `apps/terminal/README.md`.
 *
 * Imports nothing at module scope but the command table, which itself imports nothing.
 */

import { COMMANDS, UsageError } from './commands.mjs';
import { expandGlob } from './glob.mjs';
import { HOME, HOSTNAME, userName } from '../../shared/fs/seed.mjs';
import { DIR, FsError } from '../../shared/fs/vfs.mjs';

/* ---------------------------------------------------------------------------
   Words.
   --------------------------------------------------------------------------- */

const OPERATORS = new Set(['|', '>', '>>', '<']);

/**
 * Split a line into words and operators, remembering which words were quoted.
 *
 * The quoted flag is load-bearing: `ls '*'` must look for a file called `*`, and a glob that
 * expanded regardless of quoting would make it impossible to name one.
 *
 * @returns {{text: string, quoted: boolean, operator?: boolean}[]}
 */
export function tokenize(line) {
  const tokens = [];
  let text = '';
  let quoted = false;
  let started = false;

  const push = () => {
    if (started) tokens.push({ text, quoted });
    text = '';
    quoted = false;
    started = false;
  };

  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];

    if (char === '#' && !started) break;

    if (char === '\\') {
      const next = line[i + 1];
      if (next === undefined) throw new UsageError('unexpected end of line after \\');
      text += next;
      quoted = true;
      started = true;
      i += 1;
      continue;
    }

    if (char === "'" || char === '"') {
      const close = line.indexOf(char, i + 1);
      if (close < 0) throw new UsageError(`unterminated ${char === "'" ? 'single' : 'double'} quote`);
      text += line.slice(i + 1, close);
      quoted = true;
      started = true;
      i = close;
      continue;
    }

    if (char === ' ' || char === '\t') {
      push();
      continue;
    }

    // Refused rather than taken as a filename. `mkdir d && echo x` is a line people type
    // from habit, and with `&&` read as a word it made four directories — `d`, `&&`, `echo`
    // and `x` — and reported success. An operator this shell does not have has to be an
    // error, not an argument.
    if (char === '&' || char === ';') {
      const run = line[i + 1] === char ? char + char : char;
      throw new UsageError(`${run} is not supported — one command per line`);
    }

    if (char === '|' || char === '<' || char === '>') {
      push();
      const double = char === '>' && line[i + 1] === '>';
      tokens.push({ text: double ? '>>' : char, quoted: false, operator: true });
      if (double) i += 1;
      continue;
    }

    text += char;
    started = true;
  }
  push();
  return tokens;
}

/**
 * Group tokens into pipeline stages.
 *
 * @returns {{words: {text: string, quoted: boolean}[], out: {path: string, append: boolean}|null,
 *            in: string|null}[]}
 */
export function parse(line) {
  const stages = [];
  let stage = { words: [], out: null, in: null };
  const tokens = tokenize(line);

  for (let i = 0; i < tokens.length; i += 1) {
    const token = tokens[i];
    if (!token.operator) {
      stage.words.push(token);
      continue;
    }
    if (token.text === '|') {
      if (!stage.words.length) throw new UsageError('syntax error near |');
      stages.push(stage);
      stage = { words: [], out: null, in: null };
      continue;
    }
    const target = tokens[i + 1];
    if (!target || target.operator) {
      throw new UsageError(`syntax error: ${token.text} needs a file`);
    }
    i += 1;
    if (token.text === '<') stage.in = target.text;
    else stage.out = { path: target.text, append: token.text === '>>' };
  }

  if (stage.words.length || stage.out || stage.in) {
    if (!stage.words.length) throw new UsageError('syntax error: redirection with no command');
    stages.push(stage);
  }
  // A pipe whose last stage is empty — `ls |` — is caught here rather than run as `ls`.
  if (stages.some((each) => !each.words.length)) throw new UsageError('syntax error near |');
  return stages;
}

/** `~` and `~/...` only, which is the whole of this shell's tilde handling. */
export function expandTilde(word) {
  if (word === '~') return HOME;
  if (word.startsWith('~/')) return HOME + word.slice(1);
  return word;
}

/* ---------------------------------------------------------------------------
   The session.
   --------------------------------------------------------------------------- */

//: The refusals a half-typed path produces. Tab offers nothing for these and says nothing;
//: any other refusal is a defect and is logged where it happens.
const UNREADABLE = new Set(['ENOENT', 'ENOTDIR', 'EACCES', 'ELOOP']);

export class Shell {
  /**
   * @param {object} context
   * @param {import('../../shared/fs/vfs.mjs').VFS} context.fs
   * @param {object} [context.desktop] The `Desktop`, for the commands that are about windows.
   * @param {object} [context.win]     This terminal's own window.
   * @param {string} [context.cwd]
   * @param {() => number} [context.columns] The pane's width in characters, read per command
   *        so a resized window changes the next `ls` rather than needing a new session.
   */
  constructor({ fs, desktop = null, win = null, cwd = HOME, columns = () => 80 }) {
    this.fs = fs;
    this.desktop = desktop;
    this.win = win;
    this.cwd = fs.exists(cwd) ? cwd : '/';
    this.columns = columns;
    this.history = [];
    //: Where `cd -` goes, and null until the first `cd`. Session state, declared with the
    //: rest of it rather than invented by the command that writes it.
    this.oldcwd = null;
    this.host = HOSTNAME;
  }

  /**
   * The prompt, XP-free and bash-shaped: the home directory abbreviates to `~`.
   *
   * The identity is read from the filesystem's uid every time rather than stored, so the
   * prompt cannot say `user` while `whoami` says `root`.
   */
  prompt() {
    return `${userName(this.fs.uid)}@${this.host}:${this.shortCwd()}$ `;
  }

  shortCwd() {
    if (this.cwd === HOME) return '~';
    if (this.cwd.startsWith(`${HOME}/`)) return `~${this.cwd.slice(HOME.length)}`;
    return this.cwd;
  }

  /**
   * Run one line.
   *
   * @param {string} line
   * @param {{write: (text: string) => void, warn: (text: string) => void,
   *          control?: (name: string) => void}} io `control` carries the two things a
   *        command cannot do by writing text — clearing the pane and closing the window.
   * @returns {Promise<number>} The last stage's exit status.
   */
  async run(line, io) {
    if (line.trim()) this.history.push(line);

    let stages;
    try {
      stages = parse(line);
    } catch (error) {
      if (error instanceof UsageError) {
        io.warn(`sh: ${error.message}\n`);
        return 2;
      }
      throw error;
    }
    if (!stages.length) return 0;

    let stdin = '';
    let status = 0;

    for (let i = 0; i < stages.length; i += 1) {
      const stage = stages[i];
      const last = i === stages.length - 1;

      const words = [];
      for (const word of stage.words) {
        const text = expandTilde(word.text);
        if (word.quoted) words.push(text);
        else words.push(...expandGlob(this.fs, this.cwd, text));
      }

      const name = words[0];
      const command = COMMANDS[name];
      if (!command) {
        io.warn(`${name}: command not found\n`);
        return 127;
      }

      if (stage.in !== null) {
        try {
          stdin = this.fs.readFile(expandTilde(stage.in), { cwd: this.cwd });
        } catch (error) {
          io.warn(`sh: ${error.message}\n`);
          return 1;
        }
      }

      // A piped or redirected stage collects its output; the last unredirected stage writes
      // straight through, so a long listing appears as it is produced.
      const collecting = !last || stage.out !== null;
      let buffer = '';
      const write = collecting ? (text) => { buffer += text; } : io.write;

      const context = {
        argv: words,
        stdin,
        write,
        line: (text = '') => write(`${text}\n`),
        // stderr is never piped and never redirected, which is why a failing command inside
        // a pipeline still says so.
        warn: (text) => io.warn(text.endsWith('\n') ? text : `${text}\n`),
        control: io.control || (() => {}),
        fs: this.fs,
        shell: this,
        desktop: this.desktop,
        win: this.win,
        columns: this.columns(),
        // Whether this stage's output is going to the pane. `ls` lays out columns for a
        // reader and one name per line for a pipe, which is the difference between
        // `ls | wc -l` counting files and counting 1.
        tty: !collecting,
      };

      try {
        status = (await command.run(context)) || 0;
      } catch (error) {
        if (error instanceof UsageError) {
          io.warn(`${name}: ${error.message}\n`);
          status = 2;
        } else if (error instanceof FsError) {
          io.warn(`${name}: ${error.message}\n`);
          status = 1;
        } else {
          // An unexpected throw is a defect in a command, and the console gets the stack.
          io.warn(`${name}: internal error: ${error.message}\n`);
          console.error(`terminal: ${name} threw`, error);
          status = 70;
        }
        // The line is not abandoned. A stage that failed produced no output, and the rest of
        // the pipeline runs on that nothing — which is why `cat missing | wc -l` prints 0
        // rather than printing nothing at all.
      }

      if (stage.out) {
        try {
          this.fs.writeFile(expandTilde(stage.out.path), buffer, {
            cwd: this.cwd,
            append: stage.out.append,
          });
        } catch (error) {
          io.warn(`sh: ${error.message}\n`);
          return 1;
        }
        stdin = '';
      } else {
        stdin = buffer;
      }
    }
    return status;
  }

  /**
   * What the Tab key offers: command names in the first word, paths after it.
   *
   * @returns {{matches: string[], prefix: string}} `prefix` is the word being completed, so
   *          the caller can replace exactly that much of the line.
   */
  complete(line) {
    const words = line.split(/\s+/);
    const word = words[words.length - 1];
    const first = words.filter(Boolean).length <= 1 && !/\s$/.test(line);

    if (first) {
      const matches = Object.keys(COMMANDS).filter((name) => name.startsWith(word)).sort();
      return { matches, prefix: word };
    }

    const expanded = expandTilde(word);
    const cut = expanded.lastIndexOf('/');
    const dir = cut < 0 ? '.' : expanded.slice(0, cut + 1) || '/';
    const stem = cut < 0 ? expanded : expanded.slice(cut + 1);
    let entries;
    try {
      entries = this.fs.list(dir, { cwd: this.cwd });
    } catch (error) {
      // A half-typed path names a directory that is not there yet, and a Tab is not the
      // place to report it. Anything other than a name that cannot be read is named, because
      // a Tab that silently offers nothing is indistinguishable from an empty directory.
      if (!UNREADABLE.has(error.code)) {
        console.error(`terminal: completion could not read ${dir}`, error);
      }
      return { matches: [], prefix: word };
    }
    const matches = entries
      .filter((entry) => entry.name.startsWith(stem) && (stem.startsWith('.') || !entry.name.startsWith('.')))
      // A directory completes with its slash, so the next Tab continues into it.
      .map((entry) => (cut < 0 ? '' : expanded.slice(0, cut + 1)) + entry.name + (entry.type === 'dir' ? '/' : ''))
      .sort();
    return { matches, prefix: word };
  }
}
