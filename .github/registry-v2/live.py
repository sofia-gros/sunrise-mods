"""Authenticate v2 catalogue files and assemble or guard a Pages deployment."""
import argparse
import hashlib
import itertools
import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

SITE = "https://sunrise-org.github.io/sunrise-mods/v2/"
LIMITS = {
    "index.json": 256 << 20,  # Existing mod tool's bounded::MAX_CATALOG.
    "index.json.minisig": 16 << 20,  # Existing registry fetch_releases.py metadata bound.
}
ICON_LIMIT = 256 << 10  # sunrise_mod_package::icon::MAX_ICON.
SCREENSHOT_LIMIT = 5 << 20  # sunrise_mod_package::page::MAX_SCREENSHOT_BYTES.
PROJECT = re.compile(r"[0-9A-Za-z]{8}")  # Same Modrinth ID rule as registry-bot.
COLLECTION = re.compile(r"[0-9a-z_-]+")  # registry-bot's slug rule.
IMAGE = re.compile(r"([0-9a-f]{64})\.(png|jpg)")
ERRORS = (KeyError, OSError, TypeError, ValueError, tarfile.TarError, subprocess.CalledProcessError)


def reason(error):
    # API failures, paths and metadata can contain hostile text.
    return ascii(str(error)).replace("::", "%3A%3A").replace("##[", "%23%23%5B")


def download(name, file, limit):
    file.parent.mkdir(parents=True, exist_ok=True)
    got = subprocess.run(
        ["curl", "--disable", "--silent", "--fail", "--location", "--globoff",
         "--proto", "=https", "--proto-redir", "=https", "--write-out", "%{http_code}",
         "--header", "Cache-Control: no-cache", "--max-filesize", str(limit),
         "--output", str(file), "--", f"{SITE}{name}?publish={time.time_ns()}"],
        capture_output=True,
    )
    if got.returncode == 22 and got.stdout == b"404":
        return False
    if got.returncode or got.stdout != b"200":
        raise ValueError(f"live v2 download failed (curl {got.returncode}, HTTP {got.stdout!r})")
    if file.stat().st_size > limit:
        raise ValueError("live v2 download exceeds the registry limit")
    return True


@dataclass(frozen=True)
class Index:
    serial: int
    body: bytes
    images: dict[str, tuple[int, str]]


def verify(root, key, bot):
    for name, limit in LIMITS.items():
        if (root / name).stat().st_size > limit:
            raise ValueError(f"{name} exceeds the registry limit")
    result = subprocess.run(
        [bot, "verify-index", root / "index.json", root / "index.json.minisig", "--key", key],
        capture_output=True,
    )
    if result.returncode:
        raise ValueError("index signature verification failed")
    body = (root / "index.json").read_bytes()
    index = json.loads(body)
    serial = index["serial"]
    if index["schema"] != 2 or type(serial) is not int or not 0 <= serial < 2**64:
        raise ValueError("unsupported signed index schema or serial")
    refs = {}
    # An index signed before collections existed has no such field.
    owners = [(project["id"], PROJECT, [(project["icon"], ICON_LIMIT)] + [
        (shot["path"], SCREENSHOT_LIMIT) for shot in project["screenshots"]
    ]) for project in index["projects"]] + [
        (collection["id"], COLLECTION, [(collection["icon"], ICON_LIMIT)])
        for collection in index.get("collections", [])
    ]
    for owner, shape, listed in owners:
        if not shape.fullmatch(owner):
            raise ValueError("invalid signed project or collection ID")
        prefix = f"pages/{owner}/"
        for name, limit in listed:
            image = IMAGE.fullmatch(name.removeprefix(prefix)) if name.startswith(prefix) else None
            if not image:
                raise ValueError("signed image path is not content-addressed under its owner")
            refs[name] = (min(refs.get(name, (limit, ""))[0], limit), image[1])
    return Index(serial, body, refs)


def check_images(root, index, fetch=False):
    for name, (limit, digest) in index.images.items():
        file = root / name
        if fetch and not download(name, file, limit):
            raise ValueError(f"missing published image: {name!r}")
        if file.stat().st_size > limit or hashlib.sha256(file.read_bytes()).hexdigest() != digest:
            raise ValueError(f"image does not match the signed catalogue: {name!r}")


def read_live(root, key, bot, with_images=False):
    found = [download(name, root / name, limit) for name, limit in LIMITS.items()]
    if not any(found):
        return None  # Only two 404s can mean the v2 site has never been published.
    if not all(found):
        raise ValueError("the live index and signature are not both available")
    index = verify(root, key, bot)
    if with_images:
        check_images(root, index, fetch=True)
    return index


def gh_api(endpoint):
    result = subprocess.run(["gh", "api", f"repos/{os.environ['GITHUB_REPOSITORY']}/{endpoint}"],
                            check=True, capture_output=True)
    return json.loads(result.stdout)


def last_deployed():
    # A publish that deployed and then reported a v2 error is still the last successful
    # Pages deploy. Workflow conclusion alone would skip that recovery snapshot. Check
    # the Pages environment's deployment, then tie its job to the exact publish run.
    repo = re.escape(f"{os.environ['GITHUB_SERVER_URL']}/{os.environ['GITHUB_REPOSITORY']}")
    job_url = re.compile(rf"{repo}/actions/runs/([0-9]+)/job/([0-9]+)")
    for page in itertools.count(1):
        deployments = gh_api(f"deployments?environment=github-pages&per_page=100&page={page}")
        if not deployments:
            return None
        for deployment in deployments:
            statuses = gh_api(f"deployments/{deployment['id']}/statuses?per_page=100")
            status = next((row for row in statuses if row["state"] == "success"), None)
            if status is None:
                continue
            match = job_url.fullmatch(status["log_url"])
            if deployment["ref"] != "main" or not match:
                raise ValueError("last successful Pages deployment is not a main workflow job")
            run_id, job_id = match.groups()
            run = gh_api(f"actions/runs/{run_id}")
            job = gh_api(f"actions/jobs/{job_id}")
            sha = deployment["sha"]
            if (run["path"] != ".github/workflows/publish.yml" or run["head_branch"] != "main"
                    or run["head_sha"] != sha or str(job["run_id"]) != run_id
                    or job["head_sha"] != sha or job["name"] != "deploy"):
                raise ValueError("last successful Pages deployment is not from the main publish job")
            return run_id, job["run_attempt"], sha


def read_deployed(root, key, bot):
    deployed = last_deployed()
    if deployed is None:
        return None
    run_id, deploy_attempt, sha = deployed
    tree_sha = gh_api(f"git/commits/{sha}")["tree"]["sha"]
    for component in ("registry", "index-key.pub"):
        tree = gh_api(f"git/trees/{tree_sha}")
        entry = next((row for row in tree["tree"] if row["path"] == component), None)
        if entry is None:
            if tree["truncated"]:
                raise ValueError("cannot establish whether the last Pages deployment had a v2 key")
            return None  # Confirmed legacy-only, not a missing v2 artifact.
        tree_sha = entry["sha"]

    # A deploy-only retry can reuse an earlier assembly. Resolve that attempt
    # from the jobs, not from which snapshots happen to be retained today.
    assembly = None
    for page in itertools.count(1):
        jobs = gh_api(f"actions/runs/{run_id}/jobs?filter=all&per_page=100&page={page}")["jobs"]
        for job in jobs:
            if (job["name"] == "assemble" and job["run_attempt"] <= deploy_attempt
                    and (assembly is None or job["run_attempt"] > assembly["run_attempt"])):
                assembly = job
        if len(jobs) < 100:
            break
    if assembly is None or assembly["conclusion"] != "success":
        raise ValueError("cannot identify the assembly job for the last v2 deployment")
    name = f"v2-snapshot-{assembly['run_attempt']}"
    subprocess.run(["gh", "run", "download", run_id, "--repo", os.environ["GITHUB_REPOSITORY"],
                    "--name", name, "--dir", str(root)], check=True, capture_output=True)
    index = verify(root, key, bot)
    check_images(root, index)
    return index


def check_archive(root, index):
    # Same GNU tar flags as upload-pages-artifact v3.0.1 at 56afc609. List its
    # output before upload, while we can still choose the recovery snapshot.
    result = subprocess.run(
        ["tar", "--dereference", "--hard-dereference", "--directory", str(root),
         "-cvf", os.devnull, "--exclude=.git", "--exclude=.github", "."],
        check=True, capture_output=True, text=True,
    )
    members = set(result.stdout.splitlines())
    if any(f"./v2/{name}" not in members for name in (*LIMITS, *index.images)):
        raise ValueError("Pages archive would omit signed v2 files")


def stage(site, work, root, index):
    with tempfile.TemporaryDirectory(dir=work) as staging:
        target = Path(staging) / "v2"
        target.mkdir()
        for name in (*LIMITS, *index.images):
            file = target / name
            file.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(root / name, file)
        check_archive(target.parent, index)
        shutil.move(target, site / "v2")


def assemble(site, candidate, pages, key, bot):
    if not key.is_file():
        return False  # No v2 network, including when the offline build failed.
    with tempfile.TemporaryDirectory(dir=site.parent) as work:
        work = Path(work)
        try:
            index = verify(candidate, key, bot)
            # Extract only the signed paths with the signed-image limits.
            with tarfile.open(pages, "r:") as archive:
                for member in archive:
                    if member.name not in index.images:
                        continue
                    limit, _ = index.images[member.name]
                    file = candidate / member.name
                    if not member.isfile() or member.size > limit or file.exists():
                        raise ValueError("invalid or duplicate staged image in candidate archive")
                    file.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as body, file.open("xb") as output:
                        shutil.copyfileobj(body, output)
            check_images(candidate, index)
            stage(site, work, candidate, index)
            return False
        except ERRORS as error:
            print(f"registry v2: candidate: {reason(error)}")
        # Carry over the newest authenticated snapshot. Pages can serve an older, validly signed
        # copy for a while after a deployment, so the live site alone does not say what is deployed:
        # read the last deployment's stored snapshot too and take the higher serial.
        found = []
        for source, read in (("live site", lambda root: read_live(root, key, bot, with_images=True)),
                             ("previous deployment artifact", lambda root: read_deployed(root, key, bot))):
            root = work / source.replace(" ", "-")
            try:
                index = read(root)
                if index is None:
                    raise ValueError("no signed v2 snapshot")
                found.append((index.serial, source, root, index))
            except ERRORS as error:
                print(f"registry v2: {source}: {reason(error)}")
        for _, source, root, index in sorted(found, key=lambda row: row[0], reverse=True):
            try:
                stage(site, work, root, index)
                print(f"registry v2: carried over serial {index.serial} from the {source}")
                return True
            except ERRORS as error:
                print(f"registry v2: {source}: {reason(error)}")
    raise ValueError("NO_AUTHENTICATED_V2_SNAPSHOT: preserving the existing Pages site without deploying")


def guard_deploy(root, key, bot, carried):
    if not key.is_file():
        return
    candidate = verify(root, key, bot)
    check_images(root, candidate)
    with tempfile.TemporaryDirectory(dir=root.parent) as work:
        live_root = Path(work) / "live"
        try:
            live = read_live(live_root, key, bot)
        except ERRORS as error:
            print(f"registry v2: unable to verify live index at deploy: {reason(error)}")
            live = None
            live_unavailable = True
        else:
            live_unavailable = False
        # Also check the actual deployment's immutable snapshot: Pages' cache can briefly
        # return an older but valid signature, including on a retry of only `deploy`.
        try:
            deployed = read_deployed(Path(work) / "deployed", key, bot)
        except ERRORS as error:
            print(f"registry v2: unable to verify the stored deployment snapshot: {reason(error)}")
            # A valid CDN response can still be stale. Only a confirmed legacy-only
            # (or absent) deployment can dispense with the stored serial.
            raise ValueError("NO_AUTHENTICATED_V2_SNAPSHOT: cannot verify the deployed serial") from error
        if live is None and deployed is None and (live_unavailable or carried):
            raise ValueError("NO_AUTHENTICATED_V2_SNAPSHOT: cannot verify the deployed serial")
        for previous in (live, deployed):
            if previous is None:
                continue
            if candidate.serial > previous.serial:
                continue
            if carried and candidate.serial == previous.serial and candidate.body == previous.body:
                continue
            raise ValueError("refusing to replace the deployed index with an old or different equal serial")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--out", type=Path, help="fetch only the verified live index and signature")
    action.add_argument("--assemble", type=Path, metavar="SITE")
    action.add_argument("--guard-deploy", type=Path, metavar="SNAPSHOT")
    parser.add_argument("--key", type=Path, default=Path("registry/index-key.pub"))
    parser.add_argument("--bot", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, default=Path("candidate"))
    parser.add_argument("--pages", type=Path, default=Path("v2-pages.tar"))
    parser.add_argument("--carried", action="store_true")
    args = parser.parse_args()
    try:
        if args.out is not None:
            if args.key.is_file():
                with tempfile.TemporaryDirectory(dir=args.out.parent) as directory:
                    root = Path(directory) / "live"
                    if read_live(root, args.key, args.bot) is not None:
                        shutil.move(root, args.out)
        elif args.assemble is not None:
            carried = assemble(args.assemble, args.candidate, args.pages, args.key, args.bot)
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                print(f"carried={str(carried).lower()}", file=output)
        else:
            guard_deploy(args.guard_deploy, args.key, args.bot, args.carried)
    except ERRORS as error:
        parser.exit(1, f"registry v2: {reason(error)}\n")


if __name__ == "__main__":
    main()
