---
description: Narrow what a catch handles and rethrow the rest; a swallowing catch-all only when absolutely necessary and agreed with the human dev.
appliesTo: "**/*.{ts,tsx}"
---

# No blind catches

The TypeScript twin of `../python/no-blind-except.md`. A `catch (e)` binds
`unknown`; narrow it (`e instanceof SomeError`, or a type guard) and handle only
what you narrowed. Rethrow everything else.

```ts
try {
  await save(doc);
} catch (e) {
  if (!(e instanceof ConflictError)) throw e;
  await reload(doc);
}
```

A catch-all that swallows is a blind catch: `catch {}`, `catch (e) { console.log(e) }`,
`promise.catch(() => {})`. It is allowed only when it rethrows, or when it is
absolutely necessary AND a human dev agreed. Mark it with a comment on or directly
above the handler:

```ts
// blind-catch -- analytics must never break checkout; agreed <who> #<PR>
```

The marker must say why, then end with `; agreed <who> #<PR>`. Ask the human; do not
write `agreed` on your own authority. Logging does NOT make a catch-all acceptable.

## Enforcement

Biome `suspicious/noEmptyBlockStatements` (enabled in `stacks/react/biome.json`) fails
empty `catch {}` blocks and empty `() => {}` handlers. Biome has no rule for a
catch-all that logs or ignores the error, so narrowing, rethrowing and the agreed
marker are reviewer-enforced.
