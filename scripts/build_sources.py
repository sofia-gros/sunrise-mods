"""The Nightly source list: `sources/<id>.toml` entries, checked and published as `sources.json` (README.md,
"Source mods").

    python3 -I scripts/build_sources.py check <base> <head>      # a pull request: every entry, then GitHub for new or changed ones
    python3 -I scripts/build_sources.py build <registry> --out site/sources.json

Set SUNRISE_REGISTRY_INDEX to the freshly built registry index for source icons and collections.
A listing says only that a public repository holds a Sunrise mod at a path on a branch or release. Nothing here
builds or runs the mod. An entry supplies no URLs.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

FOLDER = "sources"
REQUIRED = {"id", "name", "summary", "repo", "repo_id", "manifest", "track"}
ALLOWED = REQUIRED | {"icon"}
SITE = "https://sunrise-org.github.io/sunrise-mods/"
MANIFEST = "sunrise-mod.toml"
MAX_ENTRY = 16 * 1024
MAX_FETCHED = 128 * 1024
MAX_JSON = 2 * 1024 * 1024
MAX_U64 = (1 << 64) - 1

MOD_ID = re.compile(r"[a-z0-9_]{1,64}")
RESERVED_ID = re.compile(r"con|prn|aux|nul|(com|lpt)[1-9]")
OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}")
REPO = re.compile(r"[A-Za-z0-9._-]{1,100}")
SEGMENT = re.compile(r"[A-Za-z0-9._-]{1,255}")
REF_BAD = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//")


class Invalid(Exception):
    pass


def plain(text, most: int, what: str) -> str:
    if not isinstance(text, str) or not 1 <= len(text) <= most or text != text.strip():
        raise Invalid(f"{what} must be text of 1 to {most} characters, with no space at either end")
    if any(unicodedata.category(c).startswith("C") for c in text):
        raise Invalid(f"{what} must be plain text, without control or formatting characters")
    return text


def branch_name(ref, what: str) -> str:
    """Git's rules for a branch name (`git check-ref-format --branch`)."""
    if (
        not isinstance(ref, str)
        or not 1 <= len(ref) <= 255
        or REF_BAD.search(ref)
        or ref == "@"
        or ref.startswith(("-", "/"))
        or ref.endswith(("/", "."))
        or any(part.startswith(".") or part.endswith(".lock") for part in ref.split("/"))
    ):
        raise Invalid(f"{what} {ref!r} is not a valid branch name")
    return ref


def entry(stem: str, data: dict) -> dict:
    """One entry, checked whole; the same fields, in the same types, as `sources.json` carries."""
    missing, extra = sorted(REQUIRED - data.keys()), sorted(data.keys() - ALLOWED)
    if missing or extra:
        raise Invalid("; ".join(filter(None, [
            missing and f"missing {', '.join(missing)}",
            extra and f"unknown {', '.join(extra)}",
        ])))
    id_ = data["id"]
    if not isinstance(id_, str) or not MOD_ID.fullmatch(id_) or RESERVED_ID.fullmatch(id_):
        raise Invalid(f"id {id_!r} is not a mod id: 1 to 64 of a-z, 0-9 and _")
    if id_ != stem:
        raise Invalid(f"its id is {id_!r}; the file must be {FOLDER}/{id_}.toml")
    repo = data["repo"]
    owner, _, name = repo.partition("/") if isinstance(repo, str) else ("", "", "")
    if not (OWNER.fullmatch(owner) and REPO.fullmatch(name)) or name in (".", "..") or name.endswith(".git"):
        raise Invalid(f"repo {repo!r} must be a GitHub owner/name, without a URL or suffix")
    repo_id = data["repo_id"]
    if type(repo_id) is not int or not 1 <= repo_id <= MAX_U64:
        raise Invalid("repo_id must be the repository's GitHub id, a positive whole number")
    manifest = data["manifest"]
    parts = manifest.split("/") if isinstance(manifest, str) else []
    if not parts or not all(SEGMENT.fullmatch(p) and p not in (".", "..") for p in parts) or parts[-1] != MANIFEST:
        raise Invalid(f"manifest {manifest!r} must be a path in the repository to a {MANIFEST}")
    track = data["track"]
    if not isinstance(track, dict) or len(track) != 1:
        raise Invalid('track must be { branch = "<name>" } or { release = true }')
    if "branch" in track:
        branch_name(track["branch"], "track.branch")
    elif track.get("release") is not True:
        raise Invalid('track must be { branch = "<name>" } or { release = true }')
    parsed = {
        "id": id_,
        "name": plain(data["name"], 80, "name"),
        "summary": plain(data["summary"], 160, "summary"),
        "repo": repo,
        "repo_id": repo_id,
        "manifest": manifest,
        "track": dict(track),
    }
    if "icon" in data:
        icon = f"icons/{id_}.png"
        if data["icon"] != icon:
            raise Invalid(f"icon must be the registry-relative path {icon!r}")
        parsed["icon"] = icon
    return parsed


def read(registry: Path) -> tuple[dict[str, tuple[bytes, dict]], list[str]]:
    """Every entry under `registry/sources/` by id, with its file's bytes, and what is wrong with the rest."""
    folder = registry / FOLDER
    if not folder.exists() and not folder.is_symlink():
        return {}, []
    if folder.is_symlink() or not folder.is_dir():
        return {}, [f"{FOLDER}: must be a folder, not a link or a file"]
    entries, errors, places = {}, [], {}
    for path in sorted(folder.iterdir()):
        where = f"{FOLDER}/{path.name}"
        if path.is_symlink() or not path.is_file():
            errors.append(f"{where}: only files go here, not links or folders")
            continue
        if not path.name.endswith(".toml"):
            errors.append(f"{where}: only <id>.toml files go here")
            continue
        raw = path.read_bytes()
        try:
            if len(raw) > MAX_ENTRY:
                raise Invalid(f"larger than {MAX_ENTRY // 1024} KiB")
            parsed = entry(path.name.removesuffix(".toml"), tomllib.loads(raw.decode("utf-8")))
        except (Invalid, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            errors.append(f"{where}: {e}")
            continue
        place = (parsed["repo_id"], parsed["manifest"])
        if place in places:
            errors.append(f"{where}: {places[place]} already lists the mod at {parsed['manifest']} in {parsed['repo']}")
            continue
        places[place] = where
        entries[parsed["id"]] = (raw, parsed)
    return entries, errors


def registry_metadata(
    registry: Path, entries: dict[str, tuple[bytes, dict]], index: Path | None,
) -> tuple[list[dict], list[str]]:
    """Reuse the registry build's icon validation and its projection of collections/<id>.toml."""
    icons = {id_: parsed["icon"] for id_, (_, parsed) in entries.items() if "icon" in parsed}
    if index is None:
        if icons or any((registry / "collections").glob("*.toml")):
            return [], ["source icons and collections require SUNRISE_REGISTRY_INDEX to name the freshly built registry index"]
        return [], []
    try:
        published = json.loads(index.read_bytes())
        checked = {p["id"]: p.get("icon") for p in published["projects"]} if icons else {}
        collections = [
            {**row, "icon": f"icons/{row['id']}.png"}
            for row in published.get("collections", [])
            if all(member["id"] in entries for member in row["members"])
        ]
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as e:
        return [], [f"cannot read the registry index for sources: {e}"]

    # The index's icon URL fingerprints the validated bytes.
    errors = []
    for id_, icon in icons.items():
        path = registry / icon
        where = f"{FOLDER}/{id_}.toml: icon {icon}"
        if path.parent.is_symlink() or path.is_symlink() or not path.is_file():
            errors.append(f"{where} must exist as a regular file, not a link")
            continue
        if not checked.get(id_):
            errors.append(f"{where} has no validated project icon in the registry index")
            continue
        try:
            with path.open("rb") as file:
                digest = hashlib.file_digest(file, "sha256").hexdigest()[:12]
        except OSError as e:
            errors.append(f"{where} could not be read: {e}")
            continue
        if checked[id_] != f"{SITE}{icon}?v={digest}":
            errors.append(f"{where} does not match the validated project icon in the registry index")
    return collections, errors


def document(entries: dict[str, tuple[bytes, dict]], collections: list[dict]) -> bytes:
    sources = [parsed for _, (_, parsed) in sorted(entries.items())]
    payload = {"format": 1, "sources": sources, "collections": collections}
    text = json.dumps(payload, indent=1, ensure_ascii=False).encode() + b"\n"
    if len(text) > MAX_JSON:
        raise Invalid(f"sources.json would be {len(text)} bytes; at most {MAX_JSON}")
    return text


def gh_api(path: str):
    """GitHub's answer for `path`, or None when GitHub says it isn't there (404)."""
    done = subprocess.run(["gh", "api", path], capture_output=True, text=True)
    if done.returncode != 0:
        if "(HTTP 404)" in done.stderr:
            return None
        raise Invalid(f"GitHub didn't answer {path}: {done.stderr.strip()}")
    return json.loads(done.stdout)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def raw_file(repo: str, commit: str, path: str) -> bytes | None:
    """A file at a commit from GitHub's raw host, sent no credentials and never redirected; None when missing."""
    url = f"https://raw.githubusercontent.com/{repo}/{commit}/{path}"
    try:
        with urllib.request.build_opener(NoRedirect).open(url, timeout=30) as response:
            data = response.read(MAX_FETCHED + 1)
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise Invalid(f"{path} at {commit[:12]}: the raw host answered {e.code}") from None
    except urllib.error.URLError as e:
        raise Invalid(f"{path} at {commit[:12]}: {e.reason}") from None
    if len(data) > MAX_FETCHED:
        raise Invalid(f"{path} at {commit[:12]} is larger than {MAX_FETCHED // 1024} KiB")
    return data


def commit_of(api, repo: str, track: dict) -> tuple[str, str]:
    """The full commit the tracker names now, and how to say which tracker it was."""
    if "branch" in track:
        ref = api(f"repos/{repo}/git/ref/heads/{urllib.parse.quote(track['branch'])}")
        if ref is None:
            raise Invalid(f"{repo} has no branch {track['branch']!r}")
        said = f"branch {track['branch']}"
    else:
        release = api(f"repos/{repo}/releases/latest")
        if release is None:
            raise Invalid(f"{repo} has no published release (drafts and pre-releases don't count)")
        tag = branch_name(release.get("tag_name"), "the latest release's tag")
        ref = api(f"repos/{repo}/git/ref/tags/{urllib.parse.quote(tag)}")
        if ref is None:
            raise Invalid(f"{repo}'s latest release names tag {tag!r}, which doesn't exist")
        said = f"release {tag}"
    target = ref["object"]
    for _ in range(4):
        if target["type"] != "tag":
            break
        target = api(f"repos/{repo}/git/tags/{target['sha']}")["object"]
    if target["type"] != "commit" or not re.fullmatch(r"[0-9a-f]{40}", target["sha"]):
        raise Invalid(f"{repo}'s {said} doesn't name a commit")
    return target["sha"], said


def confirm(parsed: dict, api=gh_api, fetch=raw_file) -> str:
    """What GitHub says of a new or changed entry: a public repository with that exact name and id, whose
    tracker resolves to a commit holding the mod's manifest and its Cargo.toml. Nothing is built."""
    repo = parsed["repo"]
    found = api(f"repos/{repo}")
    if found is None or found.get("private") or found.get("visibility") not in (None, "public"):
        raise Invalid(f"{repo} isn't a public GitHub repository (or it moved: use its current owner/name)")
    if found.get("full_name") != repo:
        raise Invalid(f"GitHub names the repository {found.get('full_name')!r}; use exactly that")
    if found.get("id") != parsed["repo_id"]:
        raise Invalid(f"{repo}'s GitHub id is {found.get('id')}, not {parsed['repo_id']}")
    commit, said = commit_of(api, repo, parsed["track"])
    folder = parsed["manifest"].rpartition("/")[0]
    cargo_path = f"{folder}/Cargo.toml" if folder else "Cargo.toml"
    texts = {}
    for path in (parsed["manifest"], cargo_path):
        data = fetch(repo, commit, path)
        if data is None:
            raise Invalid(f"{repo} has no {path} at {said} ({commit[:12]})")
        try:
            texts[path] = tomllib.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
            raise Invalid(f"{path} at {commit[:12]} doesn't parse: {e}") from None
    manifest = texts[parsed["manifest"]]
    if manifest.get("id") != parsed["id"]:
        raise Invalid(f"{parsed['manifest']} at {commit[:12]} is mod {manifest.get('id')!r}, not {parsed['id']!r}")
    if not isinstance(manifest.get("native"), dict):
        raise Invalid(f"{parsed['manifest']} at {commit[:12]} has no [native] table: list a content mod in mods/ instead")
    if not isinstance(texts[cargo_path].get("package"), dict):
        raise Invalid(f"{cargo_path} at {commit[:12]} has no [package] table")
    return f"{repo} {said} → {commit[:12]}: {parsed['manifest']} is {parsed['id']}"


def check(base: Path, head: Path, api=gh_api, fetch=raw_file, *, index: Path | None = None) -> int:
    old, _ = read(base)
    new, errors = read(head)
    collections, metadata_errors = registry_metadata(head, new, index)
    errors.extend(metadata_errors)
    try:
        document(new, collections)
    except Invalid as e:
        errors.append(str(e))
    for id_ in sorted(set(old) - set(new)):
        print(f"removed: {id_}")
    for id_, (raw, parsed) in sorted(new.items()):
        if id_ in old and old[id_][0] == raw:
            continue
        try:
            print(f"{'changed' if id_ in old else 'new'}: {confirm(parsed, api, fetch)}")
        except Invalid as e:
            errors.append(f"{FOLDER}/{id_}.toml: {e}")
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    print(f"{len(new)} source {'entry' if len(new) == 1 else 'entries'}, {len(errors)} {'error' if len(errors) == 1 else 'errors'}")
    return 1 if errors else 0


def build(registry: Path, out: Path, *, index: Path | None = None) -> int:
    entries, errors = read(registry)
    collections, metadata_errors = registry_metadata(registry, entries, index)
    errors.extend(metadata_errors)
    try:
        text = document(entries, collections)
    except Invalid as e:
        errors.append(str(e))
    for e in errors:
        print(f"error: {e}", file=sys.stderr)
    if errors:
        return 1
    out.write_bytes(text)
    print(f"{out}: {len(entries)} sources, {len(collections)} {'collection' if len(collections) == 1 else 'collections'}")
    return 0


def main(args: list[str]) -> int:
    index = os.environ.get("SUNRISE_REGISTRY_INDEX")
    index = Path(index) if index else None
    if len(args) == 3 and args[0] == "check":
        return check(Path(args[1]), Path(args[2]), index=index)
    if len(args) == 4 and args[0] == "build" and args[2] == "--out":
        return build(Path(args[1]), Path(args[3]), index=index)
    sys.exit(__doc__)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
