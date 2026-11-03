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
//
// `check` exit codes: 0 every entry matches; 1 drift or a missing folder;
// 2 usage error or unusable lock file.
//
// Standard library only, so the action needs no `npm install`.

import { createHash } from 'node:crypto';
import { readFile, readdir } from 'node:fs/promises';
import { join, relative, resolve } from 'node:path';

const LOCK_FILE = 'skills-lock.json';
const LOCK_VERSION = 1;
// Canonical install dir first, then Claude Code's harness dir.
const SKILL_DIRS = ['.agents/skills', '.claude/skills'];
const USAGE = [
  'usage: skills-lock.mjs hash <dir>',
  '       skills-lock.mjs check [--root <repo>] [--template]',
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

/** SHA-256 hex digest of a skill folder, identical to the skills CLI's. */
export async function computeSkillFolderHash(skillDir) {
  const files = [];
  await collectFiles(skillDir, skillDir, files);
  files.sort((a, b) => a.relativePath.localeCompare(b.relativePath));
  const hash = createHash('sha256');
  for (const file of files) {
    hash.update(file.relativePath);
    hash.update(file.content);
  }
  return hash.digest('hex');
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

/** Compare every locked skill with its installed folder; return the exit code. */
async function check(root) {
  const lock = await readLock(root);
  const problems = [];
  for (const [name, entry] of Object.entries(lock.skills)) {
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
    [
      'Vendored skills must match skills-lock.json. Revert the edit, or move',
      'repo-specific customizations to .agents/rules/ or .agents/skill-config/<skill>.md.',
      'To restore a skill from the lock: npx -y skills@latest experimental_install --force',
    ].join('\n'),
  );
  return 1;
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
    // `--template` (the template repo's own stricter check) arrives in a later
    // change; for now it behaves like the plain check.
    const { root } = parseCheckArgs(rest);
    return check(resolve(root));
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
