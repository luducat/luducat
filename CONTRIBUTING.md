# Contributing to luducat

## How We Work

luducat is built with AI-assisted development. This section explains what
that means in practice, because "AI-assisted" covers everything from
copy-pasting ChatGPT output to disciplined tool-augmented engineering.
We aim for the latter.

### The Development Loop

1. **Human designs.** Architecture decisions, feature scope, UI/UX design,
   and technical direction are human-driven. Major features start as design
   documents before any code is written.

2. **Human directs.** Implementation tasks are specified with context:
   what to build, which patterns to follow, what constraints apply, what
   not to do. The AI works within these boundaries.

3. **Human reviews and signs off.** Every change is read and understood
   before commit. Code that doesn't make sense gets questioned, reworked,
   or rejected. The human is responsible for what ships. This includes
   commit messages, public-facing text, and release notes.

4. **Tests verify.** The test suite runs against every change. New
   functionality requires new tests. Regressions are caught before they
   reach users.

This is not "generate and ship." It is closer to pair programming where
one partner is very fast at typing but needs clear direction and regular
correction.

### What "Industry-Standard Practices" Means Here

- **Version control** with commit history
- **Automated testing** with broad coverage (unit, integration, contract)
- **Database migrations** with upgrade/downgrade paths
- **Architecture** a well planned out, rigorous executed and implemented architecture
- **Modularity** modular and code deduplication
- **Security model** designed and documented before implementation
- **Privacy by design** data stays local, no telemetry, no silent outbound calls
- **Structured, typed Code** typed interfaces, defensive coding standards, readable code
- **Error handling** graceful degradation, never silent failures
- **Code review** on every change (human reviews AI output, same as
  reviewing a colleague's PR)
- **Dependency management** with pinned versions and license auditing

### What the AI Does NOT Do

- Make architecture or design decisions
- Choose libraries or dependencies
- Decide what features to build or priorities
- Interact with users or make project governance decisions
- Push code without human review

## Non-Negotiable Principles

These apply to all contributions, including plugins and themes. They are
not guidelines — they are requirements.

- **No telemetry.** No analytics, no tracking, no data collection of
  any kind.
- **No data leaves the machine without explicit user opt-in.** If a
  feature needs to contact an external service, the user must opt in
  first. That opt-in must be revocable through the user interface at any
  time, without requiring a restart.
- **Offline usability.** The application must work offline. Network
  failures are handled gracefully, never with crashes.
- **No dark patterns.** No misleading UI, no guilt-tripping, no hiding
  options, no making the wrong choice look like the right one.
- **Source availability.** All contributions must comply with their
  chosen open source license. Plugin and theme source must be available
  per the license terms.

## Contributing

Contributions are welcome, especially:

- **Store plugins** for additional game platforms
- **Metadata providers** for new data sources
- **Themes** and color variants
- **Translations** — language corrections and new languages welcome
- **macOS testing and support** — source runs on macOS but is untested;
  help appreciated
- **Bug reports** with reproduction steps

### Getting Started

1. Fork the repository
2. Read the [Plugin SDK documentation](docs/plugins/Home.md) if writing
   a plugin
3. Run the test suite: `pytest`
4. Submit a pull request

luducat has a built-in log viewer at Tools > Developer Console (always
enabled, no debug mode needed) which is helpful for testing and
troubleshooting during development.

### Plugin Contributions

Plugins that use only the SDK can be licensed under any OSI-approved open
source license. The SDK provides everything a plugin needs: HTTP client,
storage, configuration, credentials, UI dialogs, and more.

**Plugin requirements:**

- All HTTP through `PluginHttpClient` (no bare `requests`, `urllib`, etc.)
- Declare all accessed domains in `plugin.json` `network.allowed_domains`
- Include a `privacy` section in `plugin.json` with `telemetry: false`
- Use `sdk.storage` for file operations (path-confined)
- Use `sdk.config` for credentials (system keyring, never plain-text)
- Bump `version` in `plugin.json` on every change
- Handle network failures gracefully

See the [Plugin SDK documentation](docs/plugins/Home.md) for the full
reference, the [submission guidelines](docs/plugins/submitting-guidelines.md)
for catalog listing, and the [licensing guide](docs/plugins/licensing.md)
for the GPL exception details.

### Core Contributions

Contributions that touch anything outside the Plugin SDK boundary
(anything under `luducat/core/`, `luducat/ui/`, or other non-SDK modules)
fall under GPL-3.0. You will still be credited as the author.

**Core guidelines:**

- **Tests required** for new functionality
- **No hardcoded colors** in UI code — use `palette()` references
- **No hardcoded paths** — use `pathlib.Path` or `os.path`
- **Wrap user-facing strings** with `_()` for translation
- **No hardcoded font sizes** — use relative sizing from system font

### Pull Request Review

Every pull request is reviewed against these contribution guidelines and
the Plugin SDK rules. Plugin contributions are checked for SDK compliance
(import audit, domain allowlist, privacy declaration). Core contributions
are reviewed for consistency with existing architecture and patterns.

### Bug Reports

File issues at
[github.com/luducat/luducat/issues](https://github.com/luducat/luducat/issues)
with:

- luducat version (Help > About)
- Operating system and desktop environment
- Steps to reproduce
- Expected vs actual behavior
- Log file if applicable (Tools > Developer Console, or
  `~/.config/luducat/logs/`)

## License

By contributing, you agree that your contributions will be licensed under
the project's [GPL-3.0 with Plugin and Theme Exception](LICENSE).
Plugins and themes using only the SDK may use any OSI-approved license.
