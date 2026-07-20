# .tmp/ — throwaway working files

Scratch space for temporary artifacts that should **never** be committed:
Playwright MCP screenshots, page snapshots, console logs, ad-hoc scripts,
intermediate data dumps, before/after comparison images, etc.

This whole directory (and `.playwright-mcp/`) is gitignored.

## Rules

- Anything in here is disposable. Do not put anything you want to keep here.
- **Delete files when you are done with the task that produced them.** Don't let
  screenshots and snapshots accumulate across sessions.

## Cleanup

Empty it out when finished (keeps this README):

```sh
# from the project root
git clean -fdx .tmp .playwright-mcp   # respects nothing-tracked; wipes both
# or, keeping this README:
find .tmp -mindepth 1 ! -name README.md -delete
rm -rf .playwright-mcp
```
