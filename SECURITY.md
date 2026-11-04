# Security and support scope

This tool deliberately changes an approval decision in an application you own.
It is not a security enhancement and must not be used to evade an organization's
deployment policy or modify someone else's installation.

Supported target: macOS Claude Desktop 4.16336.0. The patch refuses unknown
versions, unfamiliar function layouts, damaged integrity metadata, an unexpected
vendor signature on the source app, and running app processes. Local signing
tradeoffs must be accepted explicitly with `--accept-local-signing`.

The original app is backed up before installation. The tool does not modify model
configuration or Keychain contents. Local signing changes app-scoped library
validation and vendor identity/shared-Keychain entitlements. Enterprise SSO,
passkeys and existing OS privacy grants may be affected. System-wide SIP,
Gatekeeper and automatic updates are not disabled.

Before reporting a problem, preserve your backup and restore the official app if
necessary. Report reproducible, redacted details through GitHub Issues: tool
version, app version, macOS version, CPU architecture, failing command and error.
Never post credentials, cookies, raw application logs, private account details,
or full configuration files. Do not attach vendor app bundles or archives.

Unknown app versions need a new compatibility review. A passing offline test is
not a replacement for checking real browser behavior and identity-dependent
features on that version.
