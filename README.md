# Claude Browser Bypass Patch

[简体中文](README.zh-CN.md)

A community macOS patch that makes **session Bypass permissions** apply to the ordinary built-in Browser site-consent gate in Claude Desktop's **third-party inference mode**.

It does not select Bypass for you. Third-party Auto, Manual, Accept edits and Plan retain their existing behavior. Other site-safety, organization and private-network checks remain separate.

**Experimental. Tested with Claude Desktop 4.16336.0 on Apple silicon. Other versions are refused. Windows and Linux application patching are not supported. This project is not affiliated with Anthropic.**

## Read before applying

Changing a signed application requires local re-signing. This tool:

- Creates a verified copy of the original app before installation.
- Patches a staging copy, verifies it, and replaces the installed app only after preparation succeeds.
- Keeps ASAR integrity validation enabled and updates its file, header and embedded framework digests.
- Uses an ad-hoc signature and changes **app-scoped library validation** for the locally signed main process and Electron helpers.
- Removes vendor-specific application identity and shared-Keychain entitlements from locally signed components. Vendor SSO, passkeys and other identity-dependent features **may be affected**. Existing OS privacy permissions may need to be granted again.

The tool does not edit your model configuration, credentials, Keychain contents or Allowed sites list. It does not disable SIP or system-wide Gatekeeper, install a daemon, terminate Claude, or disable updates. It makes no network requests. Official updates can overwrite the patch.

The signing changes are part of this workaround, not an improvement to the app's security. Use it only on an app and machine you control and where your deployment policy permits the change. Select Bypass only for work you intend to run without the ordinary approval prompts.

## Requirements

- macOS, an owner-writable Claude Desktop **4.16336.0** installation, Python **3.10+**.
- Built-in macOS `codesign`, `ditto` and `ps` commands.
- Enough free space for an original-app backup and a staging copy.
- Claude **fully quit**, including its helpers, when applying or restoring.

Apple silicon was tested. Intel macOS has not been runtime-tested. You must supply your own official Claude installation; this repository contains no Claude app, ASAR archive, vendor JavaScript bundle or patched binary.

## Usage

Download the source or clone this repository. Review the script and signing tradeoffs before running it.

```sh
git clone https://github.com/TheNight-Watch/claude-browser-bypass-patch.git
cd claude-browser-bypass-patch
python3 claude_browser_bypass.py check
```

`check` is read-only. It checks the version, known permission-function shape, module hashes, ASAR header and embedded integrity digests. On macOS it also verifies the current code signature. `stock` means the supported original behavior; `patched` means the expected patch is present.

After fully quitting Claude:

```sh
python3 claude_browser_bypass.py apply --accept-local-signing
```

Then reopen Claude, create or use a third-party session, and select **Bypass permissions** for that session. Test the native Browser on an ordinary public site that is not already in Allowed sites.

For a different installation path, pass `--app` to each command:

```sh
python3 claude_browser_bypass.py check --app ~/Applications/Claude.app
```

No administrator elevation is requested. If you cannot write the installation directory, use an installation you own rather than running an unfamiliar patch with elevated privileges.

### Restore the original app

`apply` prints a `backup` directory. Keep it. By default backups are stored under:

```text
~/Library/Application Support/claude-browser-bypass-patch/backups/
```

After fully quitting Claude, use the exact directory printed by `apply`:

```sh
python3 claude_browser_bypass.py restore --backup "/path/printed/by/apply"
```

Restore checks the backup checksum, original signature, target path and version. It can repair a missing or corrupted installed ASAR, provided the installed app's version metadata is still readable. It refuses to silently downgrade a different installed version. App configuration and Keychain contents are not part of the backup or restore operation.

### After an official update

Run `check` again. An official update may replace the patched files. This release supports only the explicitly listed version and refuses unfamiliar versions or code shapes. Do not turn off updates or system protections to preserve a patch.

## Scope

| Behavior | This patch |
| --- | --- |
| Ordinary Browser site consent, third-party Bypass session | Skipped |
| Third-party Auto / Manual / Plan / Accept edits | Unchanged |
| Existing hosted-mode Auto / Bypass behavior | Unchanged |
| Missing session or failed permission-mode lookup | Fails closed |
| Site safety, organization rules, private-network handling | Separate checks remain |
| Web Search / WebFetch | Not modified |
| LLM endpoint, model menu, authentication, plugins | Configuration not modified |
| Official updates | Not disabled; may overwrite patch |

This is not an "allow every possible website" switch. It changes one permission-mode decision; a site can still be unavailable or blocked for other reasons.

## Validation

The underlying patch was runtime-tested in a third-party Bypass session: the built-in Browser opened IANA Example Domains and Python.org, neither preapproved, and read both document titles without site-consent prompts. That confirms those ordinary-site paths; it is not a certification of every Browser operation or enterprise feature.

The public CLI includes automated tests for the mode decision, integrity preservation, unsupported-input refusal, repeated application, backups, rollback after installation failure and restoring a damaged archive. The public packaging flow is also tested on an isolated copy of the supported macOS app.

```sh
python3 -m unittest discover -s tests -v
```

Node.js is used only by the development test that executes a **synthetic** policy function. End users need only Python and macOS's signing tools. Tests do not download or execute vendor code, and CI does not modify an installed Claude app.

A [GitHub Actions workflow template](ci/github-actions.example.yml) is included, but is not enabled by default. A repository owner can review it and copy it to `.github/workflows/test.yml` using an account authorized to write workflows. The template tests Linux and macOS with Python 3.10/3.14 and Node 22; those hosted runs have not been executed for this release. The reported 21 passing tests were run locally.

## Implementation references

- [Claude Desktop third-party configuration](https://claude.com/docs/third-party/claude-desktop/configuration)
- [Related Browser consent report](https://github.com/anthropics/claude-code/issues/96400)
- [Electron's embedded integrity-dictionary digest format](https://github.com/electron/electron/blob/main/shell/common/asar/integrity_digest.mm)

The source and license in this repository cover this community tool, not Claude or Electron distributions. See [LICENSE](LICENSE), [SECURITY.md](SECURITY.md), and [CHANGELOG.md](CHANGELOG.md).
