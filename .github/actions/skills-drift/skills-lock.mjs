#!/usr/bin/env node
// Hash and drift-check vendored agent skills against skills-lock.json.
//
// The hash is the one the `skills` CLI writes as `computedHash`. It is a port of
// `computeSkillFolderHash` / `collectFiles` from vercel-labs/skills
// (src/local-lock.ts, MIT License, https://github.com/vercel-labs/skills):
// SHA-256 over every regular file under the skill folder, sorted by POSIX
// relative path with `localeCompare`, each file feeding update(path) then
// update(bytes); `.git` and `node_modules` directories are skipped and symlinks
// are not followed. Keep it byte-for-byte compatible with the CLI.
//
// Usage:
//   node skills-lock.mjs hash <dir>
//   node skills-lock.mjs check [--root <repo>] [--template]
//   node skills-lock.mjs write [--root <repo>] [--tag <vX.Y.Z>]
//
// `check` exit codes: 0 every entry matches; 1 drift or a missing folder;
// 2 usage error or unusable lock file.
//
// `check --template` is for the template repo itself: an entry with a `ref` is
// verified against that tag's copy of the skill folder (the tag must be
// fetched), one without against the working tree. `write` regenerates the
// template's lock from the folders under .agents/skills/ that hold a SKILL.md;
// with --tag each entry gets `ref` and the hash of the folder as tagged. In
// template mode the working-tree hash covers only files git does not ignore
// (so local bytecode or .DS_Store never reach the lock); plain `check` and
// `hash` hash every file on disk, exactly as the skills CLI does.
//
// Standard library only, so the action needs no `npm install`.

import { execFile } from 'node:child_process';
import { createHash } from 'node:crypto';
import { lstat, mkdir, mkdtemp, readFile, readdir, rm, stat, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join, relative, resolve } from 'node:path';
import { promisify } from 'node:util';

const run = promisify(execFile);

const LOCK_FILE = 'skills-lock.json';
const LOCK_VERSION = 1;
// Canonical install dir first, then Claude Code's harness dir.
const SKILL_DIRS = ['.agents/skills', '.claude/skills'];
// The template's own skills live here, and `write` records them from here.
const TEMPLATE_SKILLS_DIR = '.agents/skills';
const TEMPLATE_SOURCE = 'emigre459/agentic-ai-dev-template';
const USAGE = [
  'usage: skills-lock.mjs hash <dir>',
  '       skills-lock.mjs check [--root <repo>] [--template]',
  '       skills-lock.mjs write [--root <repo>] [--tag <vX.Y.Z>]',
].join('\n');

class UsageError extends Error {}

/** Collect every regular file under `currentDir` as {relativePath, content}. */
async function collectFiles(baseDir, currentDir, results) {
  const entries = await readdir(currentDir, { withFileTypes: true });
  await Promise.all(
    entries.map(async (entry) => {
      const fullPath = join(currentDir, entry.name);
      if (entry.isDirectory()) {
        if (entry.name === '.git' || entry.name === 'node_modules') return;
        await collectFiles(baseDir, fullPath, results);
      } else if (entry.isFile()) {
        const content = await readFile(fullPath);
        const relativePath = relative(baseDir, fullPath).split('\\').join('/');
        results.push({ relativePath, content });
      }
    }),
  );
}

/** SHA-256 hex digest of {relativePath, content} files, as the skills CLI computes it. */
function digestFiles(files) {
  files.sort((a, b) => a.relativePath.localeCompare(b.relativePath));
  const hash = createHash('sha256');
  for (const file of files) {
    hash.update(file.relativePath);
    hash.update(file.content);
  }
  return hash.digest('hex');
}

/** SHA-256 hex digest of a skill folder, identical to the skills CLI's. */
export async function computeSkillFolderHash(skillDir) {
  const files = [];
  await collectFiles(skillDir, skillDir, files);
  return digestFiles(files);
}

/**
 * Same digest, over only the files git does not ignore (tracked, or untracked
 * and not gitignored) under `.agents/skills/<name>` in the repo at `root`, so
 * local bytecode, `.DS_Store` and the like never reach the template's lock.
 * Null when the folder has no such files. Template mode only: consumers keep
 * the exact CLI hash.
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
  const files = [];
  for (const path of listing.split('\0').filter((entry) => entry !== '')) {
    const relativePath = path.slice(skillPath.length + 1);
    // Mirror the plain walk: skip `.git`/`node_modules` directories, and anything
    // that is not a regular file (a tracked file deleted from disk, a symlink).
    if (relativePath.split('/').slice(0, -1).some((part) => part === '.git' || part === 'node_modules')) continue;
    const info = await lstat(join(root, path)).catch((error) => {
      if (error.code === 'ENOENT') return null;
      throw error;
    });
    if (info === null || !info.isFile()) continue;
    files.push({ relativePath, content: await readFile(join(root, path)) });
  }
  return files.length === 0 ? null : digestFiles(files);
}

/** Read and validate skills-lock.json under `root`; throw UsageError if unusable. */
async function readLock(root) {
  const lockPath = join(root, LOCK_FILE);
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
  if (lock.version !== LOCK_VERSION) {
    throw new UsageError(
      `${lockPath} has unsupported version ${lock.version} (expected ${LOCK_VERSION})`,
    );
  }
  for (const [name, entry] of Object.entries(lock.skills)) {
    if (name === '' || name === '.' || name === '..' || /[\\/]/.test(name)) {
      throw new UsageError(`${lockPath} has an invalid skill name: ${JSON.stringify(name)}`);
    }
    if (!isObject(entry) || typeof entry.computedHash !== 'string') {
      throw new UsageError(`${lockPath}: skill "${name}" has no computedHash`);
    }
  }
  return lock;
}

/** Path of the installed folder for `name` under `root`, or null when absent. */
async function findSkillDir(root, name) {
  for (const dir of SKILL_DIRS) {
    const candidate = join(root, dir, name);
    try {
      await readdir(candidate);
      return candidate;
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

/** Compare every locked skill with its installed folder; return the exit code. */
async function check(root, { template }) {
  const lock = await readLock(root);
  const problems = [];
  for (const [name, entry] of Object.entries(lock.skills)) {
    if (template && typeof entry.ref === 'string') {
      assertTagName(entry.ref);
      await assertTagExists(root, entry.ref);
      const actual = await hashSkillAtTag(root, entry.ref, name);
      if (actual === null) {
        problems.push(`MISSING ${name}: no ${TEMPLATE_SKILLS_DIR}/${name} at tag ${entry.ref}`);
      } else if (actual !== entry.computedHash) {
        problems.push(
          `DRIFT ${name}: ${TEMPLATE_SKILLS_DIR}/${name} at ${entry.ref} hashes to ${actual}, skills-lock.json has ${entry.computedHash}`,
        );
      }
      continue;
    }
    if (template) {
      const actual = await hashSkillInWorkingTree(root, name);
      if (actual === null) {
        problems.push(`MISSING ${name}: no files in ${TEMPLATE_SKILLS_DIR}/${name}`);
      } else if (actual !== entry.computedHash) {
        problems.push(
          `DRIFT ${name}: ${TEMPLATE_SKILLS_DIR}/${name} hashes to ${actual}, skills-lock.json has ${entry.computedHash}`,
        );
      }
      continue;
    }
    const dir = await findSkillDir(root, name);
    if (dir === null) {
      problems.push(`MISSING ${name}: no folder at ${SKILL_DIRS.map((d) => `${d}/${name}`).join(' or ')}`);
      continue;
    }
    const actual = await computeSkillFolderHash(dir);
    if (actual !== entry.computedHash) {
      const where = relative(root, dir).split('\\').join('/');
      problems.push(
        `DRIFT ${name}: ${where} hashes to ${actual}, skills-lock.json has ${entry.computedHash}`,
      );
    }
  }
  if (problems.length === 0) return 0;
  console.log(problems.join('\n'));
  console.log(
    (template
      ? [
          'skills-lock.json no longer matches the template skills. If the change is',
          'intended, run `make skills_lock` (add TAG=vX.Y.Z for a release) and commit',
          'skills-lock.json.',
        ]
      : [
          'Vendored skills must match skills-lock.json. Revert the edit, or move',
          'repo-specific customizations to .agents/rules/ or .agents/skill-config/<skill>.md.',
          'To restore a skill from the lock: npx -y skills@latest experimental_install --force',
        ]
    ).join('\n'),
  );
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

/** Regenerate skills-lock.json for the template's own skills; return the exit code. */
async function write(root, { tag }) {
  if (tag !== null) {
    assertTagName(tag);
    await assertTagExists(root, tag);
  }
  let names;
  try {
    names = await listTemplateSkills(root);
  } catch (error) {
    throw new UsageError(`cannot list ${TEMPLATE_SKILLS_DIR} under ${root}: ${error.message}`);
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
  await writeFile(join(root, LOCK_FILE), `${JSON.stringify(lock, null, 2)}\n`);
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
  if (!(error instanceof UsageError)) throw error;
  console.error(`skills-lock: ${error.message}\n${USAGE}`);
  process.exitCode = 2;
}
