# Git hooks

Version-controlled hooks for this repo. They are **not** active until you point
git at this directory (git does not auto-enable hooks from the working tree):

```sh
git config core.hooksPath .githooks
```

Run that once per clone — on your dev machine and on the Raspberry Pi.

## `commit-msg`

Strips AI attribution before a commit is written: an assistant "co-author"
trailer or a tool "generated-with" footer. This is a mechanical guard — it does
not depend on any tool remembering a rule. It is intentionally narrow, so human
`Co-authored-by:` lines and legitimate text are preserved.

`git commit --no-verify` bypasses all hooks; don't use it here.
