# Reviewing registry v2 pull requests

## Release pull requests

The bot adds one `registry/projects/<project>/releases/<version>.toml` and updates that project's `page/`
from the separate page bundle. Review the exact `.smod` named by the release row, not the repository's
current branch: use the package the checks verified for this PR head, or check that the file you
downloaded has the release row's SHA-256 before reading it. Read the scan summary from the same run.
The package is data: don't build or run it while reviewing.

Wait for the required checks on the PR head. The bot checks package and page format and manifests,
checks both SHA-256 hashes, verifies both GitHub attestations against the registered repository,
signer workflow and release tag, and checks that the mod builds against the named Sunrise build.
These checks do not establish that the code is safe.

Read the scanner summary (`summary.md` in the current attempt's `scan-summary-<attempt>` artifact). **Flags** groups findings by
subject with file and line references. **Release diff** lists added, removed and changed files and
locked dependencies against the last approved release. For a first release, everything is new. Use
that list to find the code to read. Start with build scripts (including the mod's own `build.rs`),
procedural macros and every new or changed locked dependency. Build scripts and proc macros can
run on the player's computer before the game starts; their presence alone is not a reason to refuse.

The [Semgrep rules](scanner/rules/rust.yml) flag:

- **Processes:** Check which commands run and where their arguments come from.
- **Network:** Check who the mod contacts, why, and what data it sends.
- **Filesystem:** Check reads, writes and deletions, especially paths outside the game folder.
- **Credentials and browser data:** Check references to credential stores and browser profiles.
- **Environment secrets:** Follow environment values read at build time or runtime to where they go.
- **Dynamic libraries:** Check how the library path is chosen and what code gets loaded.
- **Raw syscalls and assembly:** Check what they do with the host's privileges and data.
- **Encoded blobs:** Identify what long hex or Base64 strings contain and how they are used.
- **Non-asset `include_bytes!`:** Inspect the embedded file or computed path.
- **Unresolved source inclusions:** Check the files named by `include!`, path overrides and file-backed modules; the scanner does not resolve them.
- **Unsafe code:** Check host effects and the assumptions the unsafe code relies on.

Flags point to code to read, not reasons to reject. Ordinary RustSec advisories and yanks are warnings.
The only hard dependency-policy failures are dependencies not sourced from crates.io (including paths
outside the package) and crates RustSec lists as malicious. Path dependencies inside the package, such
as bundled workspace crates, are allowed. An invalid package or an incomplete scan
still cannot pass the automatic checks. A clean summary is not a substitute for reading the code.

Look only for malware, dangerous patterns or suspicious obfuscation. If something is suspicious,
request changes with the file and line and explain the security concern. Approve otherwise; the
reviewer who approves is responsible for that approval. Bugs, style, copyright, licences and
provenance are out of scope. Send ordinary bugs or suggestions to the author's repository as
friendly issues, never as a reason to block the release.

To refuse a release, close its PR and keep its branch. Deleting the branch makes the watcher open the
same release again; do that only to have a PR generated again
([Refusing or withdrawing a release](docs/registry-v2.md#refusing-or-withdrawing-a-release)).

For a takedown after merge, use [`registry/revoked.toml`](registry/revoked.toml):
record the project, reason and date, with the release's SHA-256 for a single file or without it to
take down the whole project.

## Registration pull requests

A registration PR adds the package to its repository's `registry/repositories/<repository ID>.toml` and creates its `project.toml`. The
[Submit a mod](https://playsunrise.net/mods/submit) form opens most of them, and has already checked
what follows. One opened by hand, by its author or by a maintainer answering a Register issue, has
been checked by nobody before you: confirm that the PR's author, or the issue's opener,
administers the named repository (or is one of its organisation's owners), that the numeric
repository ID is that repository's, that the caller workflow and both manifests are on its default
branch, and that the PR changes nothing but those two kinds of file. This is the one human review that claims the project ID. Check that the claimed repository and package belong
to the submitter and that the ID and slug are not being squatted. Sharing an in-game ID is allowed:
different ports are distinct projects. Registration does not approve any release code.
