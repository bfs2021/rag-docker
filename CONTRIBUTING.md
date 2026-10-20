# Contributing

Thanks for your interest in improving the RAG Platform. Bug reports, fixes, and
documentation improvements are all welcome.

## Before you start

- **Found a bug or want a feature?** Open an issue using one of the templates
  before starting significant work, so we can agree on the approach first.
- **Found a security problem?** Don't open a public issue. See
  [SECURITY.md](SECURITY.md).
- By participating you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Making a change

1. Fork the repository and create a branch from `develop`.
2. Bring the stack up and make your change (see [Quick start](README.md#quick-start)).
3. Run the verification suite. It runs on a disposable verify project, never
   on your own stack: `stack.sh` builds your branch as compose project
   `rag-verify` on port 8081, with its own empty volumes, runs the suite and
   removes the project again.

   ```bash
   bash scripts/verify/stack.sh run                  # everything, ~20 min plus start-up
   RAG_SKIP_SLOW=1 bash scripts/verify/stack.sh run  # skip LLM work, ~8 min, start-up included
   ```

   The suite exits non-zero if any check fails. See
   [scripts/verify/README.md](scripts/verify/README.md) for details.
4. If you change dependencies, follow
   [Dependency management](README.md#dependency-management) and commit the
   regenerated lock files.
5. Open a pull request against `develop` and fill in the template.

## Pull request expectations

- Keep each pull request focused on one change.
- Describe what changed and why, and link the related issue.
- Update `README.md` or the relevant spec when behaviour changes.
- Say which verification run you did (full or `RAG_SKIP_SLOW=1`) and whether it
  passed.

Only the maintainer can merge. Workflows on pull requests from forks run after
the maintainer approves them.

## License

By contributing, you agree that your contributions are licensed under the
[MIT License](LICENSE).

First-party UI and MCP versions track the repository release. When preparing a release, update `ui/package.json`, the root package versions in `ui/package-lock.json`, `mcp/mcpapp.py`, and the `service.version` default in `api/services/telemetry.py` together with their embedded documentation.

## Release checklist

- Before publishing a release, confirm that `SECURITY.md` names the supported
  release policy accurately and links to the latest published release.
