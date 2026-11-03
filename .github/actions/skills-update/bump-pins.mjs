#!/usr/bin/env node
// Bump tag-pinned agent skills to their source's newest stable release.
//
// Usage:
//   node bump-pins.mjs --root <repo>
//
// Reads <root>/skills-lock.json. An entry whose `ref` looks like vMAJOR.MINOR.PATCH
// is tag-pinned; every other entry tracks a branch and is left to
// `npx skills@latest update -p -y`, which never moves a tag-pinned entry off its
// tag. For each source with tag-pinned entries:
//
//   1. `gh api repos/<source>/releases --paginate`: the newest release that is
//      neither a draft nor a prerelease and is tagged vMAJOR.MINOR.PATCH.
//   2. If it is newer than the pin: `gh api repos/<source>/git/trees/<tag>?recursive=1`
//      lists the skills upstream at that tag. Only SKILL.md files exactly one level
//      under a skill root count; the roots are the parents of the installed
//      skills' folders (from each entry's `skillPath`, e.g. `skills` or
//      `.agents/skills`), so a fixture SKILL.md elsewhere in the repo is not a
//      skill. Installed and upstream skills are matched by `skillPath`, not name.
//   3. The skills installed from the source that still exist upstream are
//      reinstalled at the new tag with ONE call per source:
//      `npx -y skills@latest add <source>#<tag> --skill <n> ... -a <agent> -y`
//      (`universal` when the folders live under .agents/skills/, else
//      `claude-code`). The CLI rewrites the folders and the lock's ref/hash.
//
// Skills new upstream are reported as "available" and never installed; skills
// gone upstream are reported as "removed" and kept (not reinstalled, not deleted).
//
// stdout is one line of JSON:
//   {"bumped":[{name,source,from,to,notes_url}],"available":[{source,name}],
//    "removed":[{name,source}],"no_releases":[source]}
// Everything else (CLI chatter, errors) goes to stderr. Any failed `gh` or `npx`
// call exits non-zero.
//
// Standard library only, so the action needs no `npm install`. `gh` and `npx`
// are looked up on PATH.

import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync } from 'node:fs';
import { basename, dirname, join, resolve } from 'node:path';

const LOCK_FILE = 'skills-lock.json';
const SEMVER_TAG = /^v(\d+)\.(\d+)\.(\d+)$/;

class Failure extends Error {}

function fail(message) {
  throw new Failure(message);
}

/** Run `cmd args` and return stdout. Non-zero exit or spawn error throws. */
function sh(cmd, args, { cwd, passthrough = false } = {}) {
  const result = spawnSync(cmd, args, {
    cwd,
    encoding: 'utf8',
    maxBuffer: 256 * 1024 * 1024,
    env: { ...process.env, DISABLE_TELEMETRY: '1' },
    // The CLI's chatter must not pollute our JSON on stdout.
    stdio: passthrough ? ['ignore', 2, 2] : ['ignore', 'pipe', 'pipe'],
  });
  const shown = `${cmd} ${args.join(' ')}`;
  if (result.error) fail(`${shown} could not run: ${result.error.message}`);
  if (result.status !== 0) {
    const detail = passthrough ? '' : `: ${(result.stderr || '').trim()}`;
    fail(`${shown} failed (exit ${result.status})${detail}`);
  }
  return passthrough ? '' : result.stdout;
}

/**
 * Parse JSON values written back to back, e.g. the `[..][..]` that
 * `gh api --paginate` prints for an array endpoint (one array per page).
 */
function parseConcatenatedJson(text) {
  const values = [];
  let depth = 0;
  let start = -1;
  let inString = false;
  let escaped = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === '"') inString = false;
    } else if (ch === '"') {
      inString = true;
    } else if (ch === '[' || ch === '{') {
      if (depth === 0) start = i;
      depth++;
    } else if (ch === ']' || ch === '}') {
      depth--;
      if (depth === 0) values.push(JSON.parse(text.slice(start, i + 1)));
    }
  }
  if (depth !== 0) fail('unterminated JSON in gh output');
  return values;
}

function semverParts(tag) {
  const m = SEMVER_TAG.exec(tag);
  return m ? [Number(m[1]), Number(m[2]), Number(m[3])] : null;
}

function compareSemver(a, b) {
  const pa = semverParts(a);
  const pb = semverParts(b);
  for (let i = 0; i < 3; i++) {
    if (pa[i] !== pb[i]) return pa[i] - pb[i];
  }
  return 0;
}

/** Highest published, non-prerelease vX.Y.Z tag of `source`, or null. */
function newestStableTag(source) {
  const out = sh('gh', ['api', `repos/${source}/releases`, '--paginate']);
  const releases = parseConcatenatedJson(out).flat();
  const tags = releases
    .filter((r) => r && r.draft === false && r.prerelease === false)
    .map((r) => r.tag_name)
    .filter((tag) => typeof tag === 'string' && SEMVER_TAG.test(tag));
  return tags.length ? tags.reduce((a, b) => (compareSemver(b, a) > 0 ? b : a)) : null;
}

/** Parent directory of a POSIX path, "" for a top-level entry. */
function parentOf(path) {
  const parent = dirname(path);
  return parent === '.' ? '' : parent;
}

/**
 * The skill root of a lock entry: the parent of its skill folder, from
 * `skillPath` (`<root>/<name>/SKILL.md`; root "" when the folder is top-level).
 * Null when `skillPath` is not of that shape.
 */
function skillRootOf(skillPath) {
  if (typeof skillPath !== 'string' || !skillPath.endsWith('/SKILL.md')) return null;
  return parentOf(parentOf(skillPath));
}

/**
 * The SKILL.md paths `source` holds at `tag` exactly one level under one of
 * `roots`, as a Set. Anything deeper or elsewhere (fixtures, nested examples)
 * is not a skill.
 */
function upstreamSkillPaths(source, tag, roots) {
  const out = sh('gh', ['api', `repos/${source}/git/trees/${tag}?recursive=1`]);
  const [tree] = parseConcatenatedJson(out);
  if (!tree || !Array.isArray(tree.tree)) fail(`unexpected tree listing for ${source}@${tag}`);
  if (tree.truncated) fail(`the tree listing for ${source}@${tag} is truncated`);
  const paths = new Set();
  for (const entry of tree.tree) {
    if (entry.type !== 'blob' || !entry.path.endsWith('/SKILL.md')) continue;
    if (roots.has(parentOf(parentOf(entry.path)))) paths.add(entry.path);
  }
  return paths;
}

/** `universal` when the skill folder lives under .agents/skills/, else `claude-code`. */
function agentFor(root, name) {
  if (existsSync(join(root, '.agents', 'skills', name))) return 'universal';
  if (existsSync(join(root, '.claude', 'skills', name))) return 'claude-code';
  process.stderr.write(
    `warning: no folder found for skill ${name}; assuming .agents/skills (universal)\n`,
  );
  return 'universal';
}

function parseArgs(argv) {
  let root = '.';
  for (let i = 0; i < argv.length; i++) {
    if (argv[i] === '--root' && i + 1 < argv.length) root = argv[++i];
    else fail(`usage: bump-pins.mjs --root <repo> (got ${argv[i]})`);
  }
  return resolve(root);
}

function main() {
  const root = parseArgs(process.argv.slice(2));
  const summary = { bumped: [], available: [], removed: [], no_releases: [] };

  const lockPath = join(root, LOCK_FILE);
  if (!existsSync(lockPath)) {
    process.stderr.write(`no ${LOCK_FILE} in ${root}; nothing to bump\n`);
    return summary;
  }
  const lock = JSON.parse(readFileSync(lockPath, 'utf8'));
  const entries = Object.entries(lock.skills ?? {})
    .filter(([, e]) => e && typeof e.source === 'string')
    .filter(([, e]) => (e.sourceType ?? 'github') === 'github');

  // source -> Map(name -> skillPath) of every skill installed from it (any ref),
  // and the tag-pinned names grouped by ref.
  const installedBySource = new Map();
  const pinnedBySource = new Map();
  for (const [name, e] of entries) {
    const pinned = typeof e.ref === 'string' && SEMVER_TAG.test(e.ref);
    if (pinned && skillRootOf(e.skillPath) === null) {
      fail(`${LOCK_FILE}: skill ${name} has no usable skillPath (${JSON.stringify(e.skillPath)})`);
    }
    if (!installedBySource.has(e.source)) installedBySource.set(e.source, new Map());
    installedBySource.get(e.source).set(name, e.skillPath);
    if (pinned) {
      if (!pinnedBySource.has(e.source)) pinnedBySource.set(e.source, new Map());
      const byRef = pinnedBySource.get(e.source);
      if (!byRef.has(e.ref)) byRef.set(e.ref, []);
      byRef.get(e.ref).push(name);
    }
  }

  const available = new Map(); // `${source}\0${skillPath}` -> {source, name}
  for (const [source, byRef] of pinnedBySource) {
    const newest = newestStableTag(source);
    if (newest === null) {
      summary.no_releases.push(source);
      continue;
    }
    const installed = installedBySource.get(source);
    const installedPaths = new Set(installed.values());
    const roots = new Set();
    for (const skillPath of installedPaths) {
      const skillRoot = skillRootOf(skillPath);
      if (skillRoot !== null) roots.add(skillRoot);
    }
    for (const [ref, names] of byRef) {
      if (compareSemver(newest, ref) <= 0) continue;

      const upstream = upstreamSkillPaths(source, newest, roots);
      for (const path of upstream) {
        if (!installedPaths.has(path)) {
          available.set(`${source}\0${path}`, { source, name: basename(parentOf(path)) });
        }
      }
      const keep = names.filter((n) => upstream.has(installed.get(n)));
      for (const name of names) {
        if (!upstream.has(installed.get(name))) summary.removed.push({ name, source });
      }
      if (keep.length === 0) continue;

      const agent = agentFor(root, keep[0]);
      const args = ['-y', 'skills@latest', 'add', `${source}#${newest}`];
      for (const name of keep) args.push('--skill', name);
      args.push('-a', agent, '-y');
      sh('npx', args, { cwd: root, passthrough: true });

      for (const name of keep) {
        summary.bumped.push({
          name,
          source,
          from: ref,
          to: newest,
          notes_url: `https://github.com/${source}/releases/tag/${newest}`,
        });
      }
    }
  }
  summary.available = [...available.values()];
  return summary;
}

try {
  process.stdout.write(`${JSON.stringify(main())}\n`);
} catch (err) {
  if (!(err instanceof Failure)) throw err;
  process.stderr.write(`bump-pins: ${err.message}\n`);
  process.exit(1);
}
