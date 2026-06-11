# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- Add the `OF_WEBDAV_AUTH` environment variable to select the WebDAV HTTP
  authentication scheme (`basic` by default, or `digest` for servers that
  reject Basic auth with `401`)

### Changed

- Remove the temporary `pip-audit` ignore for `CVE-2026-4539` now that a
  fixed `Pygments` release is available in the resolved dependency graph

## [1.1.0] - 2026-04-07

### Added

- Add a hardened HTTPS API with FastAPI, authenticated OpenAPI, and mandatory
  TLS 1.3 for network automation clients such as n8n

### Changed

- Harden the HTTP transport with trusted host validation, security headers,
  auth failure throttling, request timeouts, and structured OTel-style logs
- Promote OpenAPI to the canonical machine-readable HTTP contract and tighten
  the public CLI, MCP, and HTTP documentation around shipped behavior
- Expand public transport-facing docstrings so runtime entrypoints and API
  schemas are clearer to operators and integrators
- Update the FastAPI and Starlette dependency ranges to pull in the fixed
  Starlette release for `CVE-2025-62727`

## [1.0.12] - 2026-03-25

### Added

- Add first-class tag workflows across CLI and MCP, including tag create,
  update, drop, listing, and tag-based filtering for tasks and projects

### Changed

- Split detailed user-facing documentation into dedicated CLI, MCP, and
  container reference pages linked from the README
- Keep OmniFocus tag metadata as first-class parsed and serialized model data
  so tag round-trips no longer depend on assignment-only internals

## [1.0.11] - 2026-03-25

### Added

- Add project review metadata to the parsed model and preserve it across write
  paths so MCP and CLI updates no longer discard OmniFocus review state
- Add MCP project review workflows with `get_project`,
  `list_projects_for_review`, and `mark_project_reviewed`

### Changed

- Clarify in the README that `omnifocus-cli` runs directly against the WebDAV
  bundle without requiring OmniFocus.app, macOS, or Automation permissions

### Fixed

- Replace deprecated `datetime.utcnow()` defaults with timezone-aware UTC
  timestamps in the in-memory model

## [1.0.10] - 2026-03-25

### Added

- Parse OmniFocus multi-parent delta DAGs and materialize `reference-snapshot`
  updates so synced folder and project structure stays consistent with the
  upstream bundle

### Changed

- Split sync graph traversal into a dedicated `omnifocus.sync.graph` module to
  keep store orchestration logic focused and easier to maintain
- Differentiate `projects` from `folders` in the CLI by rendering a
  project-centric view grouped by folder with richer project metadata
- Document and enforce a temporary `pip-audit` allowlist for
  `CVE-2026-4539` until upstream publishes a fixed `Pygments` release

## [1.0.9] - 2026-03-24

### Fixed

- Preserve folder entities across partial folder updates that omit `<name>`,
  so valid folders no longer disappear into `Missing Folder`
- Preserve project folder assignment across partial `<project>` updates that
  omit `<folder>`, so projects no longer fall into `No Folder` spuriously
- Accept explicit `op="delete"` for folder removals and stop relying on the
  ambiguous legacy name-less folder tombstone shape

## [1.0.8] - 2026-03-24

### Added

- Add full folder management across CLI and MCP with `folders`, `folder-add`,
  `folder-update`, `folder-drop`, and MCP `get_folder`, `get_folder_tree`,
  `add_folder`, `update_folder`, and `drop_folder`
- Add hierarchical folder tree rendering and JSON output with direct child
  projects plus `No Folder` and missing-folder buckets

### Changed

- Extend project updates in CLI and MCP to support assigning projects to
  folders and clearing folder assignment
- Validate folder reparenting before sync writes to reject missing parents,
  self-parenting, and cycles

## [1.0.7] - 2026-03-24

### Fixed

- Publish GHCR runtime images as a multi-architecture manifest for both `linux/amd64` and
  `linux/arm64`
- Clarify in the container usage docs that `:latest` may require `podman pull` or
  `--pull=always` when a stale local image is already cached

## [1.0.6] - 2026-03-24

### Added

- Support task reassignment during updates in both CLI and MCP, including moving tasks into a
  project by `project_id`, moving them back to inbox, replacing tags, and clearing tags

### Fixed

- Persist sync client identity separately from transient writer state so the same cache
  directory reuses the same OmniFocus registered client identity across restarts and repairs
- Use a stable default sync client device name instead of the container hostname

## [1.0.5] - 2026-03-24

### Fixed

- Preserve freshly created tasks in the parsed model when later OmniFocus `op="update"`
  deltas omit `<name>`, so MCP `get_task` and `update_task` can immediately resolve IDs
  returned by `add_task`
- Merge partial task updates in the parser instead of treating name-less update deltas as
  deletion markers

## [1.0.4] - 2026-03-24

### Fixed

- CLI and container `--version` output now reads the installed package metadata instead of a stale hardcoded module constant
- Release `v1.0.4` restores consistency between Git tag, package metadata, release assets, and runtime version reporting

## [1.0.3] - 2026-03-24

### Changed

- Split CI badge publishing into a separate write-scoped job while keeping the public badge URL stable
- Normalized release metadata so package version, changelog, tags, and release assets are aligned again
- Refreshed the local agent operating rules for the now-public repository and hardened release process

### Fixed

- Main branch protection readiness for required checks and signed-commit enforcement
- Public-repo release hygiene around version drift between `pyproject.toml`, changelog entries, and release tags

## [1.0.2] - 2026-03-24

### Changed

- Pinned GitHub Actions workflow dependencies to full commit SHAs for public-repo policy compliance
- Improved README release badging and container release presentation

### Fixed

- Release workflow deprecation warnings by removing the remaining Node 20-based actions
- Coverage badge branch publishing by configuring Git identity in the temporary checkout
- Coverage badge workflow YAML errors discovered during release hardening

## [1.0.1] - 2026-03-24

### Added

- `pip-audit` in the release-quality verification workflow
- runtime container smoke tests in CI for the `of` launcher
- dynamic coverage badge publishing for the default branch

### Changed

- Simplified container CLI invocation around the `of` launcher
- Improved CLI help output and container-mode messaging
- Refreshed README, contribution guide, and release automation docs
- Clarified GHCR and GitHub Releases as the active publish channels
- Aligned internal engineering guidance with the Python 3.14 runtime baseline

### Fixed

- GitHub Actions readiness for the Node 24 transition
- CI regressions around formatting and CLI launcher coverage discovered during release hardening

## [1.0.0] - 2026-03-23

### Added

- Initial OmniFocus CLI and MCP server release
