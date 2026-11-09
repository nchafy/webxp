/* The tree a fresh filesystem starts with.
 *
 * Written as calls against the filesystem's own API rather than as a nested literal, so the
 * seed cannot describe a tree the API would refuse to build — a hard link to a directory,
 * a file inside a file, a mode nothing enforces. If it seeds, it is reachable.
 *
 * `/home/user/links/` exists for the reader: one of each kind of symlink, including the two
 * that fail, because "it has symlinks" is a claim and `ls -l links/` is the evidence.
 *
 * This module imports nothing. `fs.mjs` imports it when it needs it.
 */

export const HOME = '/home/user';

//: The machine's name. One fact: `/etc/hosts` is rendered from it and the shell prompt reads
//: it, so a prompt saying `webxp` cannot sit over a `/etc/hosts` that says anything else.
export const HOSTNAME = 'webxp';

//: Who exists. The filesystem checks permissions against a uid; `/etc/passwd`, `ls -l`,
//: `whoami` and the prompt all name the same uid, and they do it from here rather than from
//: three copies of the same two rows.
const ACCOUNTS = [
  { uid: 0, gid: 0, name: 'root', home: '/root' },
  { uid: 1000, gid: 1000, name: 'user', home: HOME },
];

/** The name an id belongs to, or the number when nothing claims it. */
export function userName(uid) {
  const account = ACCOUNTS.find((each) => each.uid === uid);
  return account ? account.name : String(uid);
}

const README = `webxp filesystem
================

This is a real filesystem, not a list of pretend files: names live in directories, the
data lives in inodes, and the two are joined by a number. That is why ln makes a second
name for one file and ln -s makes a file that holds a path.

Try:

  ls -l links/        one of each kind of symlink, including the broken one
  cat notes/todo.txt
  find / -name '*.log'
  grep -n error /var/log/boot.log
  ls -l /etc          secret.conf is mode 000, and cat says so
  open notes/todo.txt opens it in Notepad; Ctrl+S writes it back here
  help                every command, with its flags

Files written here survive a reload. "format --yes" puts this tree back.
`;

const TODO = `- read the window model in docs/windows.md
- find out why realpath and pwd disagree inside links/
- try: cd links/relative-dir, then pwd, then pwd -P
- write something with Notepad and cat it from here
`;

const IDEAS = `A folder is a file that holds names.
A symlink is a file that holds a path.
A hard link is a name that holds a file.
Everything else is accounting.
`;

const BOOT_LOG = `[    0.000000] webxp kernel booting
[    0.010000] CPU: one, imaginary
[    0.120000] memory: whatever the tab was given
[    0.240000] vfs: mounting the seed tree
[    0.260000] vfs: 1 superblock, inodes in a Map
[    0.300000] display: 1280x820, Luna theme
[    0.420000] taskbar: ready
[    0.500000] warning: no network device
[    0.640000] warning: clock has no seconds hand
[    0.700000] error: no sound card found
[    0.880000] error: tried to open /dev/dsp, got nothing
[    1.000000] shell: terminal available
[    1.100000] login: user
`;

const HOSTS = `127.0.0.1   localhost
127.0.0.1   ${HOSTNAME}
::1         localhost
`;

const PASSWD = `${ACCOUNTS.map(
  (account) => `${account.name}:x:${account.uid}:${account.gid}:${account.name}:${account.home}:/bin/sh`,
).join('\n')}\n`;

const MOTD = `Windows XP's chrome. A POSIX shell behind it. Type help.
`;

const SECRET = `If you can read this, the permission bits are not being checked.
`;

const SH = `This file is not the shell. The shell is a module — apps/terminal/shell.mjs — and
/etc/passwd points here because a login shell has to be a path.
`;

const DOC = `webxp ships the window manager, the apps and this filesystem.
The filesystem's own notes are in shared/fs/README.md in the source tree.
`;

/**
 * Build the starting tree on an empty filesystem.
 *
 * @param {import('./vfs.mjs').VFS} fs
 */
export function buildSeed(fs) {
  for (const dir of ['/bin', '/etc', '/home', HOME, '/usr/share/doc/webxp', '/var/log']) {
    fs.mkdir(dir, { recursive: true });
  }
  // The one world-writable directory, which is what distinguishes /tmp from everywhere else.
  fs.mkdir('/tmp', { mode: 0o777 });

  // The only executable in the tree, so `ls -l` has an `x` bit to show and `/etc/passwd`
  // points at something that exists. The shell itself is a module, not a file here.
  fs.writeFile('/bin/sh', SH, { mode: 0o755 });

  fs.writeFile('/etc/motd', MOTD);
  fs.writeFile('/etc/hosts', HOSTS);
  fs.writeFile('/etc/passwd', PASSWD, { mode: 0o644 });
  fs.writeFile('/etc/secret.conf', SECRET);
  // Unreadable on purpose. `cat /etc/secret.conf` is the check that the mode bits are not
  // decoration, and the file has to say what it would say so a reader can tell the
  // difference between a refusal and an empty file.
  fs.chmod('/etc/secret.conf', 0o000);

  fs.writeFile('/var/log/boot.log', BOOT_LOG);
  fs.writeFile('/usr/share/doc/webxp/README', DOC);

  fs.writeFile(`${HOME}/readme.txt`, README);
  fs.mkdir(`${HOME}/notes`);
  fs.writeFile(`${HOME}/notes/todo.txt`, TODO);
  fs.writeFile(`${HOME}/notes/ideas.txt`, IDEAS);

  fs.mkdir(`${HOME}/links`);
  fs.symlink('/etc/motd', `${HOME}/links/absolute`);
  fs.symlink('../readme.txt', `${HOME}/links/relative`);
  fs.symlink('/home/user/notes', `${HOME}/links/relative-dir`);
  fs.symlink('nowhere.txt', `${HOME}/links/broken`);
  // A link to itself. Resolution gives up after 40 hops and says so, which is the only
  // answer a cycle has.
  fs.symlink('loop', `${HOME}/links/loop`);
  // Two names, one inode: editing either changes both, and `ls -l` shows the link count.
  fs.link(`${HOME}/notes/ideas.txt`, `${HOME}/links/ideas-too`);
}
