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
import { expandGlob, hasMagic } from './glob.mjs';
import { HOME, HOSTNAME, userName } from '../../shared/fs/seed.mjs';
import { DIR, FsError } from '../../shared/fs/vfs.mjs';

/* ---------------------------------------------------------------------------
   Words.
   --------------------------------------------------------------------------- */

/**
 * Split a line into words and operators, reporting which words still carry a glob.
 *
 * Quoting is tracked per character span, not per word. `magic` is true only for a `*`, `?`
 * or `[` the user left unquoted, which is what makes both halves of the rule work: `ls '*'`
 * looks for a file called `*`, and `ls "no"*` still expands — a word-wide flag made one
 * quoted character suppress globbing across the whole word.
 *
 * @returns {{text: string, magic: boolean, operator?: boolean}[]}
 */
export function tokenize(line) {
  const tokens = [];
  let text = '';
  let magic = false;
  let started = false;

  const push = () => {
    if (started) tokens.push({ text, magic });
    text = '';
    magic = false;
    started = false;
  };

  for (let i = 0; i < line.length; i += 1) {
    const char = line[i];

    if (char === '#' && !started) break;

    if (char === '\\') {
      const next = line[i + 1];
      if (next === undefined) throw new UsageError('unexpected end of line after \\');
      text += next;
      started = true;
      i += 1;
      continue;
    }

    if (char === "'") {
      const close = line.indexOf("'", i + 1);
      if (close < 0) throw new UsageError('unterminated single quote');
      text += line.slice(i + 1, close);
      started = true;
      i = close;
      continue;
    }

    if (char === '"') {
      const span = doubleQuoted(line, i);
      text += span.text;
      started = true;
      i = span.end;
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
      tokens.push({ text: double ? '>>' : char, magic: false, operator: true });
      if (double) i += 1;
      continue;
    }

    text += char;
    if (hasMagic(char)) magic = true;
    started = true;
  }
  push();
  return tokens;
}

/**
 * The contents of a double-quoted span, and the index of its closing quote.
 *
 * `\` is honoured, and only before the two characters that are still special inside double
 * quotes. A scan that looked for the next `"` with `indexOf` stopped at an escaped one, so
 * `echo "a\"b"` reported an unterminated quote for a line that has none.
 *
 * @param {string} line
 * @param {number} open Index of the opening quote.
 */
function doubleQuoted(line, open) {
  let text = '';
  let i = open + 1;
  while (i < line.length && line[i] !== '"') {
    if (line[i] === '\\' && (line[i + 1] === '"' || line[i + 1] === '\\')) {
      text += line[i + 1];
      i += 2;
      continue;
    }
    text += line[i];
    i += 1;
  }
  if (i >= line.length) throw new UsageError('unterminated double quote');
  return { text, end: i };
}

/**
 * Group tokens into pipeline stages.
 *
 * `outs` is every `>` and `>>` in the stage, in the order they were written. A shell opens
 * all of them — `echo a > f1 > f2` creates `f1` as well — and the last one gets the output.
 *
 * @returns {{words: {text: string, magic: boolean}[],
 *            outs: {path: string, append: boolean}[], in: string|null}[]}
 */
export function parse(line) {
  const stages = [];
  let stage = { words: [], outs: [], in: null };
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
      stage = { words: [], outs: [], in: null };
      continue;
    }
    const target = tokens[i + 1];
    if (!target || target.operator) {
      throw new UsageError(`syntax error: ${token.text} needs a file`);
    }
    i += 1;
    if (token.text === '<') stage.in = target.text;
    else stage.outs.push({ path: target.text, append: token.text === '>>' });
  }

  if (stage.words.length || stage.outs.length || stage.in) {
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
        if (word.magic) words.push(...expandGlob(this.fs, this.cwd, text));
        else words.push(text);
      }

      const name = words[0];
      const command = COMMANDS[name];
      if (!command) {
        io.warn(`${name}: command not found\n`);
        return 127;
      }

      // Mounted files have no contents until something fetches them, and the command table
      // declares which commands need contents rather than each command discovering it.
      // `grep -rn x /usr/src/webxp` has to pull a tree; `ls -l` and `find` never fetch
      // anything, because the index already carries every size and date they print.
      // `< file` is a read by the shell itself, and it needs fetching whatever the command
      // declares: `wc -c < /usr/src/webxp/README.md` has no operands at all, so hydrating
      // only the words showed the reader `the caller must await hydrate() first` — a message
      // about a defect in the program — for an ordinary redirection.
      const needed = [];
      if (command.reads === 'operands') needed.push(...words.slice(1));
      else if (command.reads === 'tree') needed.push(...this.#subtrees(words.slice(1)));
      if (stage.in !== null) needed.push(expandTilde(stage.in));
      if (needed.length) {
        const { hydrate } = await import('../../shared/fs/fs.mjs');
        await hydrate(this.fs, needed, { cwd: this.cwd });
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
      const collecting = !last || stage.outs.length > 0;
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

      if (stage.outs.length) {
        // Every target is opened, and only the last one is written: `echo a > f1 > f2`
        // leaves `f1` there and empty. Writing only the last one created neither the file
        // nor a word about it.
        for (let out = 0; out < stage.outs.length; out += 1) {
          const { path, append } = stage.outs[out];
          const written = out === stage.outs.length - 1;
          try {
            // An earlier `>>` opens without truncating, so its file is created and left as
            // it was — the one case a write of '' would get wrong.
            if (!written && append) this.fs.touch(expandTilde(path), { cwd: this.cwd });
            else {
              this.fs.writeFile(expandTilde(path), written ? buffer : '', {
                cwd: this.cwd,
                append,
              });
            }
          } catch (error) {
            io.warn(`sh: ${error.message}\n`);
            return 1;
          }
        }
        stdin = '';
      } else {
        stdin = buffer;
      }
    }
    return status;
  }

  /**
   * Every file under these paths, for a command that reads a whole tree.
   *
   * A word that is not a directory comes back as itself, so `grep -r pattern file` still
   * works and an option like `-n` resolves to nothing and is harmless. The filesystem
   * decides which of these actually need fetching.
   */
  #subtrees(words) {
    const paths = [];
    for (const word of words) {
      let stat;
      try {
        stat = this.fs.stat(word, { cwd: this.cwd });
      } catch {
        continue;
      }
      if (stat.type !== 'dir') {
        paths.push(word);
        continue;
      }
      for (const entry of this.fs.walk(word, { cwd: this.cwd })) {
        if (!entry.error && entry.stat.type === 'file') paths.push(entry.path);
      }
    }
    return paths;
  }

  /**
   * What the Tab key offers: command names in the first word, paths after it.
   *
   * @returns {{matches: string[], prefix: string}} `prefix` is the word being completed, so
   *          the caller can replace exactly that much of the line.
   */
  complete(line) {
    const word = line.split(/\s+/).pop();
    // A command position is the start of a line or the word after a `|`, so `ls | ca`
    // offers `cat`. Decided from what precedes the word rather than from its index: the
    // first word of the line is not the only place a command name goes.
    const before = line.slice(0, line.length - word.length);

    if (/(^|\|)\s*$/.test(before)) {
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
      .map((entry) => (cut < 0 ? '' : expanded.slice(0, cut + 1)) + entry.name + (entry.type === DIR ? '/' : ''))
      .sort();
    return { matches, prefix: word };
  }
}
