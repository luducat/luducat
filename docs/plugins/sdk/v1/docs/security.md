# Security Model for Plugins

This is a summary of the security features relevant to plugin authors. For the
full security model, see [docs/security-model.md](../../../../security-model.md).

## Plugin Integrity

Every plugin directory is fingerprinted using a SHA-256 Merkle hash of all
`.py` files and `plugin.json`. This hash is verified at startup and before
sync operations.

If a plugin's files are modified after installation, luducat detects the
mismatch and disables the plugin with a warning dialog.

**What this means for you:** Don't modify installed plugin files in-place. To
update a plugin, bump its version in `plugin.json` so the update mechanism
handles it cleanly.

## Import Audit

At load time, luducat scans every `.py` file in your plugin for:

1. **Telemetry libraries** -- Always blocked. No exceptions.
   - `analytics`, `sentry_sdk`, `mixpanel`, `amplitude`, `posthog`,
     `datadog`, `newrelic`, `bugsnag`, `rollbar`

2. **Core imports** -- Blocked for third-party plugins.
   - `from luducat.core` or `import luducat.core`
   - Bundled plugins get a warning but are allowed.

If your plugin is blocked, check `~/.local/share/luducat/luducat.log` for the
specific import violation.

## Domain Allowlists

Your plugin declares which domains it needs in `plugin.json`:

```json
{
  "network": {
    "allowed_domains": ["api.example.com", "cdn.example.com"]
  }
}
```

All HTTP requests through `PluginHttpClient` are checked against this list.
Requests to undeclared domains are blocked and logged.

## Path Confinement

`PluginStorage` confines all file operations to your plugin's three directories
(config, cache, data). Path traversal attempts (`../`) are blocked with
`PluginStorageError`.

## Credential Storage

Credentials stored via `set_credential()` go into the system keyring (GNOME
Keyring, KWallet, macOS Keychain, Windows Credential Manager). They're never
stored in plain text on the filesystem.

## Trust Tiers

| Tier | Description | Audit Behavior |
|------|-------------|---------------|
| **Bundled** | Ships with luducat | Implicit trust. Core imports warned, not blocked. |
| **Trusted User** | Explicitly approved by user | Full audit. Core imports blocked. |
| **Untrusted** | New, not yet approved | Non-functional until user approves. |

## Non-Negotiables for Plugin Catalog

If you submit a plugin to the official catalog, these are mandatory:

- No telemetry, no analytics, no tracking
- All HTTP through `PluginHttpClient` (no bare `requests`)
- Domain allowlist in `plugin.json`
- Privacy declaration in `plugin.json`
- SDK imports only (no `luducat.core.*`)
- No embedded credentials
- Version bumps on every change

See [Submitting Guidelines](../../../submitting-guidelines.md) for the full
checklist.

## Gotchas

- **Development mode.** When running from the source tree (not installed),
  plugins skip fingerprint verification. This is structural (source tree
  detection), not a bypass flag.
- **Pre-commit hook.** Don't be surprised if `git diff` shows changes to
  `plugin.json` version -- this is the version bump requirement.
- **Keyring availability.** On minimal Linux installs without a desktop
  environment, the keyring may fall back to a file-based backend. This is
  handled automatically by the credential manager.
