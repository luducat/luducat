# Plugin Philosophy

## Mission

Be the place where your entire game library comes together. Every game, every
store, every launcher -- unified and accessible. Plugins are how luducat reaches
into the wider ecosystem without hardcoding every integration into the core.

## Guiding Rules

1. **User consent and agency above all else.** Plugins never act without the
   user's knowledge. Data access is opt-in. Credentials are stored in the
   system keyring, never embedded.

2. **Offline-first.** Everything works without a network connection. Plugins
   cache aggressively. Sync is explicit, not background-silent.

3. **Privacy by default.** No telemetry. No analytics. No data sent anywhere
   the user didn't explicitly approve. Plugins declare their network domains
   up front and the core enforces it. Any opt-in must be revocable through
   the user interface at any time, without requiring a restart.

4. **No dark patterns.** No misleading UI, no guilt-tripping, no hiding
   options, no making the wrong choice look like the right one.

5. **Quality over quantity.** A few well-built plugins beat many half-finished
   ones. Ship what works, iterate openly, mark unfinished features clearly.

## Design Principles

### Cooperative, Not Competitive

Luducat works *with* other launchers, stores, and tools -- not against them.
We integrate data from Steam, GOG, Epic, Heroic, Lutris, and more. More
sources means a more complete catalog, which means more value for everyone.

### Earn Trust Through Quality

Good UX, good performance, respectful data handling. No over-promising.
Features ship when they work.

### Never Lock In

Industry-standard data formats. Users can always export their data and leave.
The goal is to be useful enough that they choose to stay.

### Honest About What's Incomplete

Under-promise, over-deliver. If a plugin capability is still in development,
say so. Don't ship stubs that pretend to work.

## Approach for Plugin Authors

### Build for the User

Your plugin exists to solve a real problem. Before writing code, ask: "What
does the user gain from this?" If the answer isn't clear, reconsider.

### Respect the Sandbox

Plugins run inside a security sandbox. Your HTTP requests go through
`PluginHttpClient` (domain-checked, rate-limited). Your files go through
`PluginStorage` (path-confined). Your credentials go through the system
keyring. This isn't bureaucracy -- it's protection for your users.

### Keep It Simple

A plugin that does one thing well is better than one that does five things
poorly. If your store plugin also needs metadata enrichment, that's two
plugins. The SDK makes composition easy.

### Test Locally

Every plugin can be developed and tested without modifying the core
application. Drop your plugin directory into the plugins folder, restart
luducat, and see it appear. The feedback loop should be fast.

### Contribute Back

If you build something useful, consider submitting it to the plugin catalog.
The community benefits from shared work, and you benefit from review and
testing by other users.

## What Plugins Can Do

| Type | Purpose | Examples |
|------|---------|---------|
| **Store** | Import game libraries from storefronts | Steam, GOG, Epic |
| **Metadata** | Enrich game data with additional information | IGDB, PCGamingWiki, SteamGridDB |
| **Platform** | Provide game engines and platforms | DOSBox, ScummVM, OpenMW |
| **Runner** | Handle game launching through external apps | Heroic, Lutris, Steam Client |

## What Plugins Cannot Do

- Modify the core application or other plugins
- Access the filesystem outside their designated directories
- Make HTTP requests to undeclared domains
- Send telemetry or analytics
- Import directly from `luducat.core.*` (third-party plugins)
- Embed API keys or credentials in source code
