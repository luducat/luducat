# Submitting a Plugin to the Catalog

The official luducat plugin catalog is a curated collection of community
plugins. Submission is open to anyone who meets the guidelines below.

## Requirements

### Before Submitting

- [ ] **License:** Any OSI-approved license. Declare it in your repository and
  plugin directory.
- [ ] **Source repository:** Public git repository (GitHub, GitLab, Codeberg,
  etc.). We need somewhere to review code and track issues.
- [ ] **Contact:** Reachable author -- email, GitHub profile, or similar.
- [ ] **Tests:** At least basic tests for authentication, core methods, and
  error paths.
- [ ] **README:** Explains what the plugin does, how to configure it, and
  what data it accesses.

### Non-Negotiables

These are hard requirements. Plugins that violate them will be rejected.

| Requirement | Why |
|-------------|-----|
| **No telemetry** | `privacy.telemetry` must be `false`. No analytics, tracking, or data collection. |
| **PluginHttpClient only** | All HTTP goes through `self.http`. No bare `requests`, `urllib`, `httpx`. |
| **Domain allowlist** | `network.allowed_domains` must list every domain accessed. |
| **Privacy declaration** | `privacy` section must be present and accurate. |
| **SDK imports only** | No `from luducat.core` imports (for non-bundled plugins). |
| **No embedded credentials** | API keys, tokens, secrets go in the keyring via `set_credential()`. |
| **Version bumps** | Every change bumps `version` in `plugin.json`. |
| **Offline-safe** | Plugin must not crash when offline. Graceful degradation. |
| **Revocable opt-in** | Any user opt-in for data access must be revocable through the UI at any time, without restart. |
| **No dark patterns** | No misleading UI, no guilt-tripping, no hiding options. |
| **Source available** | Source must be available per the chosen open source license terms. |

### Your Code, Your Style

We don't enforce code style beyond the non-negotiables above. No mandatory
linters, formatters, or naming conventions for internal code. If your variable
names make sense to you and your tests pass, that's fine.

We care about:
- Does it work?
- Is it safe for users?
- Does it respect the sandbox?

We don't care about:
- Tabs vs spaces
- Docstring format
- Internal class structure
- Whether you use pathlib or os.path internally

## Review Process

1. **Submit:** Open an issue on the luducat repository with:
   - Link to your plugin's repository
   - Brief description of what it does
   - Which plugin type(s) it implements

2. **Initial review:** We check the non-negotiables. If something is missing,
   we'll tell you what to fix. This is usually quick.

3. **Functional review:** We install the plugin and test it. For store plugins,
   we need an account on the service (or you provide test credentials). For
   metadata plugins, we check data quality.

4. **Catalog listing:** Once approved, we add your plugin to the catalog with
   your repository URL. Users can find and install it.

5. **Updates:** Push new versions to your repository. Version bump in
   `plugin.json` triggers user updates.

## Update Policy

- **Breaking changes:** Coordinate with us. We'll notify users.
- **Security issues:** Report privately. We'll coordinate disclosure.
- **Abandonment:** If you can't maintain the plugin anymore, let us know.
  We can archive it or find a new maintainer.

## Catalog Categories

| Category | Description |
|----------|-------------|
| **Store** | Game library integrations (Battle.net, itch.io, etc.) |
| **Metadata** | Data enrichment (HLTB, OpenCritic, etc.) |
| **Platform** | Game engines and platforms (DOSBox, OpenMW, etc.) |
| **Runner** | Launch integrations (Lutris, Bottles, etc.) |

## Rejection Reasons

Common reasons plugins get rejected:

- **Telemetry or tracking code** found in source files
- **Hardcoded credentials** in source code
- **Missing domain allowlist** -- requests to undeclared domains
- **Direct core imports** -- `from luducat.core` in third-party plugin
- **No error handling** -- plugin crashes on network failure
- **Privacy violation** -- reads local data without consent check
- **No version in plugin.json** -- or version never bumped

## Questions?

Check the [FAQ](FAQ.md) or open an issue on the repository.
