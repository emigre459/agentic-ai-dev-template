#!/usr/bin/env node
// Hash and drift-check vendored agent skills against their lock: a repo's
// skills-lock.json, or the template's own .github/template-skills-lock.json.
//
// The hash is the one the `skills` CLI writes as `computedHash` in a version-2
// lock. It is a port of `computeSkillFolderHash` / `hashSkillFiles` from
// vercel-labs/skills (src/skill-hash.ts, MIT License,
// https://github.com/vercel-labs/skills): SHA-256 over the skill folder's files,
// sorted by the UTF-8 bytes of their `/`-separated relative paths, each framed as
// `path NUL length NUL content`, with CRLF turned into LF in text files (files
// without a NUL byte). Files named `metadata.json` and `.git`, `__pycache__` and
// `__pypackages__` directories are left out; symlinks count as their target when
// it is inside the folder and are left out otherwise. Keep it byte-for-byte
// compatible with the CLI version pinned in ../skills-cli/package.json.
//
// Usage:
//   node skills-lock.mjs hash <dir>
//   node skills-lock.mjs check [--root <repo>] [--template]
//   node skills-lock.mjs write [--root <repo>] [--tag <vX.Y.Z>]
//
// `check` exit codes: 0 every entry matches; 1 drift or a missing folder;
// 2 usage error, unusable lock file, or a CLI run that failed for another reason.
//
// Plain `check` (a repo that installed the skills) asks the pinned CLI:
// `npx -y skills@<pin> check`, which hashes each installed skill against the
// lock (either lock version: the CLI migrates a version-1 lock itself). Its
// "has local changes" lines are drift. Its "has an update" lines are not: a
// newer upstream version is the update PR's business, so they print as UPDATE
// and pass. A skill whose folder (with a SKILL.md) is gone is reported as
// MISSING before the CLI runs, since the CLI reads it as an update too.
//
// `check --template` and `write` are for the template repo itself, whose lock is
// .github/template-skills-lock.json, not a root skills-lock.json: the skills CLI
// hides every skill a source repo's root lock lists (it reads them as installed
// copies), so a root lock would stop `npx skills add` from finding the
// template's skills. `make init` moves it to the new repo's skills-lock.json.
//
// `check --template` never runs the CLI:
// the CLI compares a lock entry without a tag against the source's default
// branch, so every PR that edits a skill would read as an "update". An entry
// with a `ref` is verified against that tag's copy of the skill folder (the tag
// must be fetched), one without against the working tree. `write` regenerates
// the template's lock from the folders under .agents/skills/ that hold a
// SKILL.md; with --tag it lists them from that tag's tree, and each entry gets
// `ref` and the hash of the folder as tagged. Without --tag it lists the working
// tree, and refuses (exit 2) to overwrite a lock whose entries have a `ref`. In
// template mode the working-tree hash covers only files git does not ignore (so
// a local .DS_Store never reaches the lock); `hash` hashes every file on disk,
// exactly as the skills CLI does.
//
// Standard library only, so the action needs no `npm install`.

import { execFile, spawn } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { lstat, mkdir, mkdtemp, readFile, readdir, realpath, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join, relative, resolve, sep } from 'node:path';
import { promisify, stripVTControlCharacters } from 'node:util';

const run = promisify(execFile);

// A repo that installed the skills (plain `check`).
const LOCK_FILE = 'skills-lock.json';
// The template's own lock (`check --template` and `write`).
const TEMPLATE_LOCK_FILE = '.github/template-skills-lock.json';
// The lock version this script writes and template mode requires. Plain `check`
// also accepts 1, which the CLI migrates.
const LOCK_VERSION = 2;
const CONSUMER_LOCK_VERSIONS = new Set([1, 2]);
// Canonical install dir first, then Claude Code's harness dir.
const SKILL_DIRS = ['.agents/skills', '.claude/skills'];
// The template's own skills live here, and `write` records them from here.
const TEMPLATE_SKILLS_DIR = '.agents/skills';
const TEMPLATE_SOURCE = 'emigre459/agentic-ai-dev-template';
// The CLI version every skills-* action runs. Dependabot bumps it there.
const CLI_PIN = new URL('../skills-cli/package.json', import.meta.url);
const EXCLUDE_FILES = new Set(['metadata.json']);
const EXCLUDE_DIRS = new Set(['.git', '__pycache__', '__pypackages__']);
const USAGE = [
  'usage: skills-lock.mjs hash <dir>',
  '       skills-lock.mjs check [--root <repo>] [--template]',
  '       skills-lock.mjs write [--root <repo>] [--tag <vX.Y.Z>]',
].join('\n');

class UsageError extends Error {}
// A run that could not finish (the CLI failed or could not start): exit 2, no usage text.
class RunError extends Error {}

/** The pinned skills CLI version (an exact X.Y.Z). */
function pinnedCliVersion() {
  let version;
  try {
    version = JSON.parse(readFileSync(CLI_PIN, 'utf-8')).dependencies?.skills;
  } catch (error) {
    throw new UsageError(`cannot read the skills CLI pin ${CLI_PIN.pathname}: ${error.message}`);
  }
  if (typeof version !== 'string' || !/^\d+\.\d+\.\d+$/.test(version)) {
    throw new UsageError(`the skills CLI pin must be an exact X.Y.Z version, not ${JSON.stringify(version)}`);
  }
  return version;
}

/** Whether the installer leaves out an entry named `name`. */
function isExcluded(name, isDirectory) {
  return EXCLUDE_FILES.has(name) || (isDirectory && EXCLUDE_DIRS.has(name));
}

/** isExcluded() for a `/`-separated path relative to the skill folder. */
function isExcludedPath(relativePath) {
  const parts = relativePath.split('/').filter(Boolean);
  return parts.some((part, index) => isExcluded(part, index < parts.length - 1));
}

/**
 * Collect the files under `dir` as {path, content}, as the CLI does: excluded
 * names skipped, symlinks resolved and kept only inside `root` (the skill
 * folder's real path), a directory reached again through a link skipped.
 */
async function collectFiles(dir, prefix, root, ancestors, results) {
  const realDir = await realpath(dir);
  if (ancestors.has(realDir)) return;
  const branch = new Set(ancestors).add(realDir);
  const entries = await readdir(dir, { withFileTypes: true });
  await Promise.all(
    entries.map(async (entry) => {
      if (isExcluded(entry.name, entry.isDirectory())) return;
      const fullPath = join(dir, entry.name);
      const path = prefix + entry.name;
      let isDirectory = entry.isDirectory();
      if (entry.isSymbolicLink()) {
        const target = await realpath(fullPath).catch(() => null);
        if (!target || (target !== root && !target.startsWith(root + sep))) return;
        isDirectory = (await stat(target)).isDirectory();
        if (isDirectory && isExcluded(entry.name, true)) return;
      } else if (!isDirectory && !entry.isFile()) {
        return;
      }
      if (isDirectory) {
        await collectFiles(fullPath, `${path}/`, root, branch, results);
      } else {
        results.push({ path, content: await readFile(fullPath) });
      }
    }),
  );
}

/** CRLF to LF in a text file (one with no NUL byte); binary content is kept as is. */
function normalizeLineEndings(content) {
  if (content.includes(0) || !content.includes('\r\n')) return content;
  return Buffer.from(content.toString('latin1').replaceAll('\r\n', '\n'), 'latin1');
}

/** SHA-256 hex digest of {path, content} files, as the skills CLI computes it. */
function digestFiles(files) {
  const entries = files.map((file) => ({
    path: Buffer.from(file.path, 'utf-8'),
    content: normalizeLineEndings(file.content),
  }));
  entries.sort((a, b) => Buffer.compare(a.path, b.path));
  const hash = createHash('sha256');
  for (const { path, content } of entries) {
    hash.update(path);
    hash.update('\0');
    hash.update(String(content.length));
    hash.update('\0');
    hash.update(content);
  }
  return hash.digest('hex');
}

/** SHA-256 hex digest of a skill folder, identical to the skills CLI's. */
export async function computeSkillFolderHash(skillDir) {
  const root = await realpath(skillDir);
  const files = [];
  await collectFiles(skillDir, '', root, new Set(), files);
  return digestFiles(files);
}

/**
 * Same digest, over only the files git does not ignore (tracked, or untracked
 * and not gitignored) under `.agents/skills/<name>` in the repo at `root`, so
 * a local `.DS_Store` and the like never reach the template's lock. Null when
 * the folder has no such files. Template mode only.
 */
async function hashSkillInWorkingTree(root, name) {
  const skillPath = `${TEMPLATE_SKILLS_DIR}/${name}`;
  let listing;
  try {
    ({ stdout: listing } = await run(
      'git',
      ['ls-files', '--cached', '--others', '--exclude-standard', '-z', '--', skillPath],
      { cwd: root, maxBuffer: 64 * 1024 * 1024 },
    ));
  } catch (error) {
    throw new UsageError(`cannot list files of ${skillPath} with git in ${root}: ${error.message}`);
  }
  const skillRoot = await realpath(join(root, skillPath)).catch(() => null);
  if (skillRoot === null) return null;
  const files = [];
  for (const path of listing.split('\0').filter((entry) => entry !== '')) {
    const relativePath = path.slice(skillPath.length + 1);
    if (isExcludedPath(relativePath)) continue;
    const fullPath = join(root, path);
    // A tracked file deleted from disk is simply absent.
    const info = await lstat(fullPath).catch((error) => {
      if (error.code === 'ENOENT') return null;
      throw error;
    });
    if (info === null) continue;
    if (info.isSymbolicLink()) {
      // Mirror the CLI: a link counts as its target when that is inside the folder.
      const target = await realpath(fullPath).catch(() => null);
      if (!target || (target !== skillRoot && !target.startsWith(skillRoot + sep))) continue;
      if ((await stat(target)).isDirectory()) {
        if (isExcluded(relativePath.split('/').pop(), true)) continue;
        // The CLI's cycle guard holds the real path of every directory above the link.
        const ancestors = new Set([skillRoot]);
        const parents = relativePath.split('/').slice(0, -1);
        for (let i = 1; i <= parents.length; i += 1) {
          ancestors.add(await realpath(join(root, skillPath, ...parents.slice(0, i))));
        }
        await collectFiles(fullPath, `${relativePath}/`, skillRoot, ancestors, files);
        continue;
      }
    } else if (!info.isFile()) {
      continue;
    }
    files.push({ path: relativePath, content: await readFile(fullPath) });
  }
  return files.length === 0 ? null : digestFiles(files);
}

/** Path of the lock `check`/`write` use under `root`: the template's or a consumer's. */
function lockPathFor(root, { template }) {
  return join(root, template ? TEMPLATE_LOCK_FILE : LOCK_FILE);
}

/** Read and validate the lock under `root`; throw UsageError if unusable. */
async function readLock(root, { template }) {
  const lockPath = lockPathFor(root, { template });
  let text;
  try {
    text = await readFile(lockPath, 'utf-8');
  } catch (error) {
    throw new UsageError(`cannot read ${lockPath}: ${error.message}`);
  }
  let lock;
  try {
    lock = JSON.parse(text);
  } catch (error) {
    throw new UsageError(`${lockPath} is not valid JSON: ${error.message}`);
  }
  const isObject = (value) =>
    typeof value === 'object' && value !== null && !Array.isArray(value);
  if (!isObject(lock) || !isObject(lock.skills)) {
    throw new UsageError(`${lockPath} must be an object with a "skills" map`);
  }
  if (template && lock.version !== LOCK_VERSION) {
    throw new UsageError(
      `${lockPath} has version ${lock.version}; the template's lock must be version ${LOCK_VERSION}. ` +
        'Rewrite it with `make skills_lock` (TAG=<its release tag> if its entries have a "ref").',
    );
  }
  if (!template && !CONSUMER_LOCK_VERSIONS.has(lock.version)) {
    throw new UsageError(
      `${lockPath} has unsupported version ${lock.version} (expected one of ${[...CONSUMER_LOCK_VERSIONS].join(', ')})`,
    );
  }
  for (const [name, entry] of Object.entries(lock.skills)) {
    if (name === '' || name === '.' || name === '..' || /[\\/]/.test(name)) {
      throw new UsageError(`${lockPath} has an invalid skill name: ${JSON.stringify(name)}`);
    }
    // A version-2 entry may hold an empty hash (a v1 lock the CLI migrated with
    // the skill missing or edited); the CLI then reports it as changed.
    if (!isObject(entry) || typeof entry.computedHash !== 'string') {
      throw new UsageError(`${lockPath}: skill "${name}" has no computedHash`);
    }
  }
  return lock;
}

/** Path of the installed folder for `name` (one holding a SKILL.md), or null. */
async function findSkillDir(root, name) {
  for (const dir of SKILL_DIRS) {
    const candidate = join(root, dir, name);
    try {
      if ((await stat(join(candidate, 'SKILL.md'))).isFile()) return candidate;
    } catch (error) {
      if (error.code !== 'ENOENT' && error.code !== 'ENOTDIR') throw error;
    }
  }
  return null;
}

/** Reject tag names git could read as an option or that are not plain ref names. */
function assertTagName(tag) {
  if (!/^[A-Za-z0-9][A-Za-z0-9._/-]*$/.test(tag)) {
    throw new UsageError(`invalid tag name: ${JSON.stringify(tag)}`);
  }
}

/** Throw a UsageError unless the tag exists in the repo at `root`. */
async function assertTagExists(root, tag) {
  try {
    await run('git', ['rev-parse', '--verify', '--quiet', `refs/tags/${tag}^{commit}`], { cwd: root });
  } catch {
    throw new UsageError(
      `tag ${tag} is not present in ${root}; fetch tags first (git fetch --tags) or check the name`,
    );
  }
}

/**
 * SHA-256 of `.agents/skills/<name>` as it exists at `tag`: `git archive`
 * extracted to a throwaway directory. Null when the tag has no such folder.
 */
async function hashSkillAtTag(root, tag, name) {
  const scratch = await mkdtemp(join(tmpdir(), 'skills-lock-'));
  try {
    const skillPath = `${TEMPLATE_SKILLS_DIR}/${name}`;
    const archive = join(scratch, 'skill.tar');
    try {
      await run('git', ['archive', '--format=tar', `--output=${archive}`, `refs/tags/${tag}`, skillPath], {
        cwd: root,
      });
    } catch {
      return null;
    }
    const out = join(scratch, 'out');
    await mkdir(out);
    await run('tar', ['-xf', archive, '-C', out]);
    return await computeSkillFolderHash(join(out, skillPath));
  } finally {
    await rm(scratch, { recursive: true, force: true });
  }
}

/** Compare the template's locked skills with the tag or working tree; return the problems. */
async function templateProblems(root, lock) {
  const problems = [];
  for (const [name, entry] of Object.entries(lock.skills)) {
    if (typeof entry.ref === 'string') {
      assertTagName(entry.ref);
      await assertTagExists(root, entry.ref);
      const actual = await hashSkillAtTag(root, entry.ref, name);
      if (actual === null) {
        problems.push(`MISSING ${name}: no ${TEMPLATE_SKILLS_DIR}/${name} at tag ${entry.ref}`);
      } else if (actual !== entry.computedHash) {
        problems.push(
          `DRIFT ${name}: ${TEMPLATE_SKILLS_DIR}/${name} at ${entry.ref} hashes to ${actual}, ${TEMPLATE_LOCK_FILE} has ${entry.computedHash}`,
        );
      }
      continue;
    }
    const actual = await hashSkillInWorkingTree(root, name);
    if (actual === null) {
      problems.push(`MISSING ${name}: no files in ${TEMPLATE_SKILLS_DIR}/${name}`);
    } else if (actual !== entry.computedHash) {
      problems.push(
        `DRIFT ${name}: ${TEMPLATE_SKILLS_DIR}/${name} hashes to ${actual}, ${TEMPLATE_LOCK_FILE} has ${entry.computedHash}`,
      );
    }
  }
  return problems;
}

/** Run `npx -y skills@<version> check` in `root`; resolve to {code, output}. */
function runCliCheck(root, version) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn('npx', ['-y', `skills@${version}`, 'check'], {
      cwd: root,
      env: { ...process.env, DISABLE_TELEMETRY: '1', NO_COLOR: '1', FORCE_COLOR: '0' },
      stdio: ['ignore', 'pipe', 'pipe'],
    });
    let output = '';
    child.stdout.on('data', (chunk) => {
      output += chunk;
    });
    child.stderr.on('data', (chunk) => {
      output += chunk;
    });
    child.on('error', (error) => {
      reject(new RunError(`skills check could not start: cannot run npx: ${error.message}`));
    });
    child.on('close', (code) => resolvePromise({ code: code ?? 1, output }));
  });
}

/**
 * Drift-check an installed repo with the pinned CLI; return the problems and the
 * UPDATE notices. Throws a UsageError when the CLI failed without naming a skill.
 */
async function cliProblems(root, lock) {
  const problems = [];
  const missing = new Set();
  for (const name of Object.keys(lock.skills)) {
    if ((await findSkillDir(root, name)) === null) {
      missing.add(name);
      problems.push(`MISSING ${name}: no SKILL.md in ${SKILL_DIRS.map((d) => `${d}/${name}`).join(' or ')}`);
    }
  }
  const version = pinnedCliVersion();
  const { code, output } = await runCliCheck(root, version);
  const text = stripVTControlCharacters(output);
  process.stdout.write(text.endsWith('\n') || text === '' ? text : `${text}\n`);
  const updates = [];
  let failed = false;
  for (const line of text.split('\n').map((l) => l.trim())) {
    const changed = /^✎ (.+) has local changes$/.exec(line);
    const update = /^↑ (.+) has an update$/.exec(line);
    if (changed && !missing.has(changed[1])) {
      problems.push(`DRIFT ${changed[1]}: the installed files differ from its computedHash in skills-lock.json`);
    } else if (update && !missing.has(update[1])) {
      updates.push(`UPDATE ${update[1]}: a newer version is available upstream (not drift; the skills update PR moves it)`);
    } else if (/^Failed to update \d+ skill/.test(line)) {
      failed = true;
    }
  }
  const explained = problems.length > 0 || updates.length > 0;
  if (failed || (code !== 0 && !explained)) {
    throw new RunError(
      `skills check failed: npx -y skills@${version} check exited ${code} without naming a changed skill; see its output above`,
    );
  }
  return { problems, updates, version };
}

/** Check every locked skill; return the exit code. */
async function check(root, { template }) {
  const lock = await readLock(root, { template });
  let problems;
  let updates = [];
  let version;
  if (template) {
    problems = await templateProblems(root, lock);
  } else {
    ({ problems, updates, version } = await cliProblems(root, lock));
  }
  if (updates.length > 0) console.log(updates.join('\n'));
  if (problems.length === 0) return 0;
  console.log(problems.join('\n'));
  if (template) {
    // A pinned lock must be re-pinned with TAG: a bare `make skills_lock` would
    // drop every ref (and `write` refuses to).
    const ref = Object.values(lock.skills).find((entry) => typeof entry.ref === 'string')?.ref;
    const remedy = ref === undefined ? '`make skills_lock`' : `\`make skills_lock TAG=${ref}\` (or TAG=<newest release tag>)`;
    console.log(
      [
        `${TEMPLATE_LOCK_FILE} no longer matches the template skills. If the change is`,
        `intended, run ${remedy} and commit ${TEMPLATE_LOCK_FILE}.`,
      ].join('\n'),
    );
  } else {
    console.log(
      [
        'Vendored skills must match skills-lock.json. Revert the edit, or move',
        'repo-specific customizations to .agents/rules/ or .agents/skill-config/<skill>.md.',
        `To restore a skill from the lock: npx -y skills@${version} experimental_install --force`,
      ].join('\n'),
    );
  }
  return 1;
}

/** Names of the template's own skills: folders under .agents/skills/ with a SKILL.md. */
async function listTemplateSkills(root) {
  const entries = await readdir(join(root, TEMPLATE_SKILLS_DIR), { withFileTypes: true });
  const names = [];
  for (const entry of entries) {
    if (!entry.isDirectory()) continue;
    try {
      await stat(join(root, TEMPLATE_SKILLS_DIR, entry.name, 'SKILL.md'));
      names.push(entry.name);
    } catch (error) {
      if (error.code !== 'ENOENT') throw error;
    }
  }
  return names.sort();
}

/**
 * Names of the template's skills as released at `tag`: folders directly under
 * .agents/skills/ that hold a SKILL.md in that tag's tree. Skills added on main
 * after the tag are unreleased and stay out of a lock pinned to it.
 */
async function listTemplateSkillsAtTag(root, tag) {
  let listing;
  try {
    ({ stdout: listing } = await run(
      'git',
      ['ls-tree', '-r', '-z', '--name-only', `refs/tags/${tag}`, '--', `${TEMPLATE_SKILLS_DIR}/`],
      { cwd: root, maxBuffer: 64 * 1024 * 1024 },
    ));
  } catch (error) {
    throw new UsageError(`cannot list ${TEMPLATE_SKILLS_DIR} at tag ${tag}: ${error.message}`);
  }
  const prefix = `${TEMPLATE_SKILLS_DIR}/`;
  const names = listing
    .split('\0')
    .filter((path) => path.startsWith(prefix))
    .map((path) => path.slice(prefix.length).split('/'))
    .filter((parts) => parts.length === 2 && parts[1] === 'SKILL.md')
    .map((parts) => parts[0]);
  if (names.length === 0) {
    throw new UsageError(`tag ${tag} has no skills under ${TEMPLATE_SKILLS_DIR}/`);
  }
  return names.sort();
}

/**
 * Throw a UsageError when the existing lock pins any entry to a `ref`: `write`
 * without --tag would silently un-pin the template, so `make init` would start
 * new repos on unreleased main. A lock that is not valid JSON (say, mid-merge
 * conflict) counts as pinned when its text mentions a "ref" key.
 */
async function assertNotPinned(root) {
  const lockPath = lockPathFor(root, { template: true });
  let text;
  try {
    text = await readFile(lockPath, 'utf-8');
  } catch (error) {
    if (error.code === 'ENOENT') return;
    throw new UsageError(`cannot read ${lockPath}: ${error.message}`);
  }
  let pinned;
  try {
    const skills = JSON.parse(text)?.skills;
    pinned =
      typeof skills === 'object' &&
      skills !== null &&
      Object.values(skills).some((entry) => typeof entry?.ref === 'string');
  } catch {
    pinned = /"ref"\s*:/.test(text);
  }
  if (pinned) {
    throw new UsageError(
      `${lockPath} is pinned to a release (its entries have a "ref"); writing it without --tag would un-pin it. ` +
        `Pass --tag <newest release tag> (make skills_lock TAG=vX.Y.Z), or delete ${TEMPLATE_LOCK_FILE} first if you mean to un-pin.`,
    );
  }
}

/** Regenerate the template's own lock (TEMPLATE_LOCK_FILE); return the exit code. */
async function write(root, { tag }) {
  let names;
  if (tag !== null) {
    assertTagName(tag);
    await assertTagExists(root, tag);
    names = await listTemplateSkillsAtTag(root, tag);
  } else {
    await assertNotPinned(root);
    try {
      names = await listTemplateSkills(root);
    } catch (error) {
      throw new UsageError(`cannot list ${TEMPLATE_SKILLS_DIR} under ${root}: ${error.message}`);
    }
  }
  const skills = {};
  for (const name of names) {
    const computedHash =
      tag === null
        ? await hashSkillInWorkingTree(root, name)
        : await hashSkillAtTag(root, tag, name);
    if (computedHash === null) {
      throw new UsageError(
        `${TEMPLATE_SKILLS_DIR}/${name} has no files${tag === null ? '' : ` at tag ${tag}`}`,
      );
    }
    skills[name] = {
      source: TEMPLATE_SOURCE,
      ...(tag === null ? {} : { ref: tag }),
      sourceType: 'github',
      skillPath: `${TEMPLATE_SKILLS_DIR}/${name}/SKILL.md`,
      computedHash,
    };
  }
  const lock = { version: LOCK_VERSION, skills };
  const lockPath = lockPathFor(root, { template: true });
  await mkdir(dirname(lockPath), { recursive: true });
  await writeFile(lockPath, `${JSON.stringify(lock, null, 2)}\n`);
  return 0;
}

function parseCheckArgs(args) {
  const options = { root: '.', template: false };
  for (let i = 0; i < args.length; i += 1) {
    if (args[i] === '--root' && i + 1 < args.length) {
      options.root = args[(i += 1)];
    } else if (args[i] === '--template') {
      options.template = true;
    } else {
      throw new UsageError(`unexpected argument: ${args[i]}`);
    }
  }
  return options;
}

function parseWriteArgs(args) {
  const options = { root: '.', tag: null };
  for (let i = 0; i < args.length; i += 1) {
    if (args[i] === '--root' && i + 1 < args.length) {
      options.root = args[(i += 1)];
    } else if (args[i] === '--tag' && i + 1 < args.length) {
      options.tag = args[(i += 1)];
    } else {
      throw new UsageError(`unexpected argument: ${args[i]}`);
    }
  }
  return options;
}

async function main(argv) {
  const [command, ...rest] = argv;
  if (command === 'hash' && rest.length === 1) {
    try {
      console.log(await computeSkillFolderHash(resolve(rest[0])));
    } catch (error) {
      throw new UsageError(`cannot hash ${rest[0]}: ${error.message}`);
    }
    return 0;
  }
  if (command === 'check') {
    const { root, template } = parseCheckArgs(rest);
    return check(resolve(root), { template });
  }
  if (command === 'write') {
    const { root, tag } = parseWriteArgs(rest);
    return write(resolve(root), { tag });
  }
  throw new UsageError(command === undefined ? 'missing command' : `bad command or arguments: ${argv.join(' ')}`);
}

try {
  process.exitCode = await main(process.argv.slice(2));
} catch (error) {
  if (error instanceof RunError) {
    console.error(`skills-lock: ${error.message}`);
  } else if (error instanceof UsageError) {
    console.error(`skills-lock: ${error.message}\n${USAGE}`);
  } else {
    throw error;
  }
  process.exitCode = 2;
}
