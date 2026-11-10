## What & why

<!-- One paragraph. What changes, and what breaks if we don't do it. -->

## How it was verified

<!-- Commands you ran, and the output that proves it. "Tests pass" is not enough if a bug is involved. -->

```
make check
```

## Checklist

- [ ] A test fails before this change and passes after (behavioural changes)
- [ ] `make check` passes locally
- [ ] Comments explain *why*, not *what*
- [ ] Docs updated if user-facing behaviour changed (`README.md` / `README.zh-CN.md`)
- [ ] No new runtime dependency without justification
