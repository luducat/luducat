# luducat Security Model

This document describes how luducat protects your store credentials from
tampered plugins. 

Publishing the design gives malware authors nothing they couldn't find by reading
the source. It gives you the ability to scrutinize, poke holes, and hold us
accountable.

Found a flaw? Open an issue. We mean it.

---

## 1. The Problem We Solve

Your Steam API key, GOG session cookie, Epic OAuth token — these live in
luducat's credential store and flow through plugins every time you sync.
A plugin is a directory of Python files. If something rewrites one of those
files — malware, a careless script, a supply-chain compromise — the modified
code runs with full access to your credentials next time luducat starts.

We built a tamper-detection system that catches this. Every plugin directory
is fingerprinted. If something changes between launches, luducat notices and 
shuts the plugin down before it touches your credentials.

**This is not DRM.** There is no license check, no activation, no phone-home
requirement, no feature gate. luducat is GPLv3 — you can read the verification
code yourself. The system protects you; it does not restrict you.

---

## 2. Frequently Asked Questions

### Can I disable it?

Local verification always runs but only disables compromised plugins — it never
prevents luducat from starting. You retain full control.

### Does luducat phone home?

luducat can check for application updates on startup (opt-in, off by default).
The server sees that an IP address made a GET request and returns a static JSON
response. No user ID, no game library, no plugin list, no credentials. Same
response for everyone. Works over HTTPS. Can be disabled.

### What if I'm offline?

Everything works. The keyring has all the hashes needed for local verification.
luducat is designed offline-first.

### What if I modify a bundled plugin?

It will be flagged and disabled on next startup. If you're developing or
experimenting, run luducat from the source tree — source-tree plugins skip
verification structurally (there is no bypass flag to abuse; see Section 7).

### What about third-party plugins?

Third-party plugins that arrive as installed packages start without stored
trust data. Their fingerprint is computed.  Subsequent launches verify against 
that fingerprint.

---

## 3. Threat Model

### What We Catch

| Threat | How |
|--------|-----|
| Modified plugin files on disk | Fingerprint mismatch at startup |
| Trojanized plugin package | Hash comparison against stored fingerprint |
| Plugin importing telemetry libraries | Import audit blocks load |
| Plugin reaching unauthorized servers | Network firewall rejects the request |

### What We Don't Catch

| Threat | Why |
|--------|-----|
| Modified luducat core | If the verifier itself is compromised, verification is meaningless. Python distributions are effectively source — there is no compiled binary to sign. We are honest about this gap. |
| Root-level attacker | Root can bypass any userspace protection, including the keyring. |
| Memory-level attacks | Runtime memory manipulation is outside Python's security boundary. |

### Defence In Depth

No single layer is unbreakable. Together they raise the cost of a successful
attack well beyond "edit a `.py` file."

1. **Fingerprint** — detects file modifications (primary defence)
2. **Keyring** — stores known-good hashes in credential storage,
   a different attack surface than the filesystem
3. **Import auditing** — catches plugins that import telemetry or forbidden
   core modules before they execute
4. **Network firewall** — plugins declare their domains; everything else is
   blocked at the HTTP layer

---

## 4. Trust Architecture

### Tiers

| Tier | Source | How It Gets Trusted |
|------|--------|---------------------|
| **Bundled** | Ships with luducat | Implicit — known-good at install time |
| **Third-party** | Installed by user | Fingerprint stored on first load |


### What Happens on Mismatch

When a plugin's fingerprint does not match the hash, luducat immediately 
disables it. The plugin's ability to act is revoked.

---

## 5. The Keyring as Trust Anchor

The system keyring (GNOME Keyring, KWallet, macOS Keychain, Windows Credential
Manager) provides a trust anchor on a different attack surface than the
filesystem. An attacker who can modify files on disk may not be able to touch
keyring entries — different permissions, different access controls, potentially
hardware-backed storage.

### Keyring Unavailable

If the keyring is non-functional, the trust file is written without a
signature. This is logged as a warning. Verification still runs — it just lacks
the tamper-detection layer on the trust data itself. The same security posture
as any file-based configuration.

---

## 6. Offline-First

luducat works fully offline. The integrity system does too.

On startup:

1. Load trust data 
2. Compute local fingerprints of plugins
3. Compare against hashes

No network needed. Works on air-gapped machines, on planes, behind corporate
firewalls.

### Keyring Cleared

If the keyring entry disappears (user cleared keyring, OS migration), luducat
re-seeds from the current installation and logs a prominent warning.

---

## 7. Development Mode

There is no `--dev-mode` flag, no `LUDUCAT_SKIP_VERIFICATION` environment
variable, no configuration toggle. The distinction is based on the load path,
not a switch an attacker could flip.

```
[INFO] Plugin 'steam' loaded from ~/.local/share/luducat/plugins/steam/ (installed, verified)
[INFO] Plugin 'epic' loaded from /home/dev/luducat/luducat/plugins/epic/ (source tree)
```

---

## 8. Honest Limitations

We would rather tell you what we cannot do than pretend we have no gaps.

**Core application integrity**: luducat is Python. Every distribution format —
PyInstaller, AppImage, source tarball — is effectively source code. If an
attacker modifies the code itself, verification is meaningless.

The plugin system mitigates this because credentials flow through plugins, not
core, and plugins are what get checked. But core self-audit remains an
open problem for Python applications. We detect the distribution format
(PyInstaller, AppImage, Flatpak, Snap, pip, source) and log it, but we cannot
currently verify core integrity at runtime.

**Keyring is not hardware-backed everywhere**: On some Linux configurations
(no desktop environment, no secret service daemon), the keyring falls back to
plaintext storage. luducat logs a warning when this happens. 

If you have expertise in Python application security and see ways to improve
this, we welcome the conversation.

---

## 9. References

- [Plugin SDK Documentation](plugins/Home.md) — Plugin development guide and API reference
- [Community Plugins](wiki/Community-Plugins.md) — Plugin directory and submission guidelines
