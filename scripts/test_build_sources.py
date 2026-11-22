"""Tests for build_sources.py: python3 -m unittest discover -s scripts"""
import base64
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import build_sources as bs

COMMIT = "a" * 40
TAG_OBJECT = "b" * 40
ICON_PATH = "icons/trail_atlas.png"
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAAAAACPAi4CAAAAG0lEQVR4nO3BgQAAAADDoPlT3+AEVQEAAAB8AxBAAAFGOA0dAAAAAElFTkSuQmCC"
)

ENTRY = """id = "trail_atlas"
name = "Trail Atlas"
summary = "A field atlas for marking trails and camps."
repo = "driftwood/trail-atlas"
repo_id = 123456789
manifest = "mods/trail_atlas/sunrise-mod.toml"
track = { branch = "main" }
"""

GITHUB = {
    "repos/driftwood/trail-atlas": {"full_name": "driftwood/trail-atlas", "id": 123456789, "private": False, "visibility": "public"},
    "repos/driftwood/trail-atlas/git/ref/heads/main": {"object": {"type": "commit", "sha": COMMIT}},
    "repos/driftwood/trail-atlas/releases/latest": {"tag_name": "v1.2.0"},
    "repos/driftwood/trail-atlas/git/ref/tags/v1.2.0": {"object": {"type": "tag", "sha": TAG_OBJECT}},
    f"repos/driftwood/trail-atlas/git/tags/{TAG_OBJECT}": {"object": {"type": "commit", "sha": COMMIT}},
}

FILES = {
    "mods/trail_atlas/sunrise-mod.toml": b'id = "trail_atlas"\n[native]\ngame = "26.3"\n',
    "mods/trail_atlas/Cargo.toml": b'[package]\nname = "mod-trail-atlas"\n',
}


def edited(**changes: str) -> str:
    lines = ENTRY.splitlines()
    for key, value in changes.items():
        lines = [f"{key} = {value}" if line.startswith(f"{key} =") else line for line in lines]
    return "\n".join(lines) + "\n"


class Case(unittest.TestCase):
    def registry(self, entries: dict[str, str]) -> Path:
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = Path(folder.name)
        if entries:
            (path / "sources").mkdir()
        for name, text in entries.items():
            (path / "sources" / name).write_text(text)
        return path

    def run_check(self, base: dict, head: dict, github=GITHUB, files=FILES) -> tuple[int, list]:
        asked = []

        def api(path):
            asked.append(path)
            return github.get(path)

        def fetch(repo, commit, path):
            asked.append(f"raw {repo} {commit} {path}")
            return files.get(path) if commit == COMMIT else None

        base, head = self.registry(base), self.registry(head)
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = bs.check(base, head, api, fetch)
        return code, asked


class Entries(Case):
    def errors(self, entries: dict) -> list[str]:
        return bs.read(self.registry(entries))[1]

    def test_a_valid_entry_publishes_every_field(self):
        registry = self.registry({"trail_atlas.toml": ENTRY})
        out = registry / "sources.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(bs.build(registry, out), 0)
        self.assertEqual(json.loads(out.read_text()), {
            "format": 1,
            "sources": [{
                "id": "trail_atlas",
                "name": "Trail Atlas",
                "summary": "A field atlas for marking trails and camps.",
                "repo": "driftwood/trail-atlas",
                "repo_id": 123456789,
                "manifest": "mods/trail_atlas/sunrise-mod.toml",
                "track": {"branch": "main"},
            }],
            "collections": [],
        })

    def registry_with_icon(self) -> tuple[Path, Path]:
        registry = self.registry({"trail_atlas.toml": ENTRY + f'icon = "{ICON_PATH}"\n'})
        (registry / "icons").mkdir()
        (registry / ICON_PATH).write_bytes(PNG)
        index = registry / "index.json"
        digest = hashlib.sha256(PNG).hexdigest()[:12]
        index.write_text(json.dumps({"projects": [{
            "id": "trail_atlas",
            "icon": f"https://sunrise-org.github.io/sunrise-mods/{ICON_PATH}?v={digest}",
        }]}))
        return registry, index

    def test_a_valid_icon_publishes_its_relative_path(self):
        registry, index = self.registry_with_icon()
        out = registry / "sources.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(bs.build(registry, out, index=index), 0)
        self.assertEqual(json.loads(out.read_text())["sources"][0]["icon"], ICON_PATH)

    def test_a_missing_icon_stops_the_build(self):
        registry, index = self.registry_with_icon()
        (registry / ICON_PATH).unlink()
        out = registry / "sources.json"
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(bs.build(registry, out, index=index), 1)
        self.assertIn(f"icon {ICON_PATH} must exist as a regular file", errors.getvalue())
        self.assertFalse(out.exists())

    def test_a_path_outside_the_project_icon_is_refused(self):
        for path in [
            "elsewhere/trail_atlas.png", "icons/other.png", "icons/../trail_atlas.png",
            "icons\\trail_atlas.png", "icons/%74rail_atlas.png", "/icons/trail_atlas.png",
            "https://example.com/icons/trail_atlas.png", True,
        ]:
            with self.subTest(path=path):
                errors = self.errors({"trail_atlas.toml": ENTRY + f"icon = {json.dumps(path)}\n"})
                self.assertIn(f"icon must be the registry-relative path '{ICON_PATH}'", errors[0])

    def test_an_invalid_png_does_not_match_the_validated_icon(self):
        registry, index = self.registry_with_icon()
        (registry / ICON_PATH).write_bytes(b"not a PNG")
        out = registry / "sources.json"
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(bs.build(registry, out, index=index), 1)
        self.assertIn(f"icon {ICON_PATH} does not match the validated project icon", errors.getvalue())
        self.assertFalse(out.exists())

    def test_an_icon_without_a_validated_project_is_refused(self):
        registry, index = self.registry_with_icon()
        index.write_text('{"projects": []}')
        with redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(bs.build(registry, registry / "sources.json", index=index), 1)
        self.assertIn(f"icon {ICON_PATH} has no validated project icon", errors.getvalue())

    def test_sources_are_sorted_by_id(self):
        registry = self.registry({
            "zeta.toml": edited(id='"zeta"', repo_id="2"),
            "alpha.toml": edited(id='"alpha"', repo_id="1"),
        })
        out = registry / "sources.json"
        with redirect_stdout(io.StringIO()):
            bs.build(registry, out)
        self.assertEqual([s["id"] for s in json.loads(out.read_text())["sources"]], ["alpha", "zeta"])

    def test_no_folder_is_an_empty_list(self):
        registry = self.registry({})
        out = registry / "sources.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(bs.build(registry, out), 0)
        self.assertEqual(json.loads(out.read_text()), {"format": 1, "sources": [], "collections": []})

    def test_each_bad_field_is_refused(self):
        for change in [
            {"id": '"Trail"'},
            {"id": '"con"'},
            {"name": '""'},
            {"name": '" Trail Atlas"'},
            {"name": f'"{"x" * 81}"'},
            {"summary": '"line\\nbreak"'},
            {"summary": '"hidden\\u202etext"'},
            {"repo": '"https://github.com/driftwood/trail-atlas"'},
            {"repo": '"driftwood/trail-atlas.git"'},
            {"repo": '"-driftwood/trail-atlas"'},
            {"repo": '"driftwood/.."'},
            {"repo_id": "0"},
            {"repo_id": "-4"},
            {"repo_id": '"123456789"'},
            {"repo_id": "true"},
            {"manifest": '"mods/trail_atlas/Cargo.toml"'},
            {"manifest": '"/sunrise-mod.toml"'},
            {"manifest": '"mods//sunrise-mod.toml"'},
            {"manifest": '"mods/../sunrise-mod.toml"'},
            {"manifest": '"mods\\\\sunrise-mod.toml"'},
            {"manifest": '"mods/%2e%2e/sunrise-mod.toml"'},
            {"manifest": '"https://x/sunrise-mod.toml"'},
            {"track": '{ branch = "main", release = true }'},
            {"track": "{ release = false }"},
            {"track": '{ tag = "v1" }'},
            {"track": '{ branch = "a..b" }'},
            {"track": '{ branch = "-main" }'},
            {"track": '{ branch = "main.lock" }'},
            {"track": '{ branch = "with space" }'},
            {"track": '"main"'},
        ]:
            with self.subTest(change=change):
                self.assertTrue(self.errors({"trail_atlas.toml": edited(**change)}))

    def test_missing_and_unknown_keys_are_refused(self):
        self.assertTrue(self.errors({"trail_atlas.toml": ENTRY.replace('name = "Trail Atlas"\n', "")}))
        self.assertTrue(self.errors({"trail_atlas.toml": ENTRY + 'url = "https://example.com"\n'}))

    def test_the_file_must_be_named_for_its_id(self):
        self.assertTrue(self.errors({"other.toml": ENTRY}))
        self.assertTrue(self.errors({"trail_atlas.json": ENTRY}))

    def test_a_release_tracker_is_accepted(self):
        self.assertEqual(self.errors({"trail_atlas.toml": edited(track="{ release = true }")}), [])

    def test_two_entries_for_the_same_repository_path_are_refused(self):
        self.assertTrue(self.errors({"trail_atlas.toml": ENTRY, "atlas_two.toml": edited(id='"atlas_two"')}))

    def test_two_mods_of_one_repository_at_different_paths_are_allowed(self):
        other = edited(id='"atlas_two"', manifest='"mods/atlas_two/sunrise-mod.toml"')
        self.assertEqual(self.errors({"trail_atlas.toml": ENTRY, "atlas_two.toml": other}), [])

    def test_links_are_refused(self):
        registry = self.registry({"trail_atlas.toml": ENTRY})
        (registry / "sources" / "linked.toml").symlink_to(registry / "sources" / "trail_atlas.toml")
        self.assertTrue(bs.read(registry)[1])


class Collections(Case):
    COLLECTION = {
        "id": "trail_tools",
        "name": "Trail Tools",
        "summary": "A set of tools for making trails.",
        "description": "Pick and choose the tools you need.",
        "owners": ["driftwood"],
        "icon": f"{bs.SITE}icons/trail_tools.png?v=0123456789ab",
        "members": [{"id": "trail_atlas"}, {"id": "camp_markers", "default": False}],
    }

    def test_complete_collection_publishes_the_registry_shape_with_a_relative_icon(self):
        registry = self.registry({
            "trail_atlas.toml": ENTRY,
            "camp_markers.toml": edited(id='"camp_markers"', repo_id="2"),
        })
        index = registry / "index.json"
        index.write_text(json.dumps({"collections": [self.COLLECTION]}))
        out = registry / "sources.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(bs.build(registry, out, index=index), 0)
        self.assertEqual(json.loads(out.read_text())["collections"], [
            {**self.COLLECTION, "icon": "icons/trail_tools.png"},
        ])

    def test_collection_stays_registry_only_if_even_an_unticked_member_has_no_listing(self):
        registry = self.registry({"trail_atlas.toml": ENTRY})
        index = registry / "index.json"
        index.write_text(json.dumps({"collections": [self.COLLECTION]}))
        out = registry / "sources.json"
        with redirect_stdout(io.StringIO()):
            self.assertEqual(bs.build(registry, out, index=index), 0)
        published = json.loads(out.read_text())
        self.assertEqual(published["collections"], [])
        self.assertEqual([source["id"] for source in published["sources"]], ["trail_atlas"])

    def test_check_requires_the_registry_index_for_collection_definitions(self):
        registry = self.registry({"trail_atlas.toml": ENTRY})
        folder = registry / "collections"
        folder.mkdir()
        (folder / "trail_tools.toml").touch()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()) as errors:
            self.assertEqual(bs.check(registry, registry), 1)
        self.assertIn("require SUNRISE_REGISTRY_INDEX", errors.getvalue())


class Check(Case):
    def test_a_new_entry_is_confirmed_with_github(self):
        code, asked = self.run_check({}, {"trail_atlas.toml": ENTRY})
        self.assertEqual(code, 0)
        self.assertIn(f"raw driftwood/trail-atlas {COMMIT} mods/trail_atlas/Cargo.toml", asked)

    def test_a_release_tracker_resolves_an_annotated_tag_to_its_commit(self):
        code, asked = self.run_check({}, {"trail_atlas.toml": edited(track="{ release = true }")})
        self.assertEqual(code, 0)
        self.assertIn(f"raw driftwood/trail-atlas {COMMIT} mods/trail_atlas/sunrise-mod.toml", asked)

    def test_branch_and_tag_names_are_escaped_in_api_paths(self):
        github = {
            **GITHUB,
            "repos/driftwood/trail-atlas/git/ref/heads/fix/a%231%2Bb": GITHUB["repos/driftwood/trail-atlas/git/ref/heads/main"],
            "repos/driftwood/trail-atlas/releases/latest": {"tag_name": "v1#2"},
            "repos/driftwood/trail-atlas/git/ref/tags/v1%232": {"object": {"type": "commit", "sha": COMMIT}},
        }
        for track in ['{ branch = "fix/a#1+b" }', "{ release = true }"]:
            with self.subTest(track=track):
                self.assertEqual(self.run_check({}, {"trail_atlas.toml": edited(track=track)}, github=github)[0], 0)

    def test_an_unchanged_entry_asks_github_nothing(self):
        code, asked = self.run_check({"trail_atlas.toml": ENTRY}, {"trail_atlas.toml": ENTRY})
        self.assertEqual((code, asked), (0, []))

    def test_a_changed_entry_is_confirmed_again(self):
        code, _ = self.run_check({"trail_atlas.toml": ENTRY}, {"trail_atlas.toml": edited(repo_id="999")})
        self.assertEqual(code, 1)

    def test_what_github_must_confirm(self):
        repo = GITHUB["repos/driftwood/trail-atlas"]
        for github in [
            {**GITHUB, "repos/driftwood/trail-atlas": None},
            {**GITHUB, "repos/driftwood/trail-atlas": {**repo, "private": True}},
            {**GITHUB, "repos/driftwood/trail-atlas": {**repo, "full_name": "Driftwood/trail-atlas"}},
            {**GITHUB, "repos/driftwood/trail-atlas": {**repo, "id": 5}},
            {**GITHUB, "repos/driftwood/trail-atlas/git/ref/heads/main": None},
        ]:
            with self.subTest(github=github):
                self.assertEqual(self.run_check({}, {"trail_atlas.toml": ENTRY}, github=github)[0], 1)

    def test_what_the_commit_must_hold(self):
        manifest, cargo = "mods/trail_atlas/sunrise-mod.toml", "mods/trail_atlas/Cargo.toml"
        for files in [
            {cargo: FILES[cargo]},
            {manifest: FILES[manifest]},
            {**FILES, manifest: b'id = "other"\n[native]\n'},
            {**FILES, manifest: b'id = "trail_atlas"\n'},
            {**FILES, manifest: b"id = = \n"},
            {**FILES, cargo: b'[workspace]\nmembers = []\n'},
        ]:
            with self.subTest(files=files):
                self.assertEqual(self.run_check({}, {"trail_atlas.toml": ENTRY}, files=files)[0], 1)

    def test_a_root_manifest_reads_the_root_cargo_toml(self):
        files = {"sunrise-mod.toml": FILES["mods/trail_atlas/sunrise-mod.toml"], "Cargo.toml": FILES["mods/trail_atlas/Cargo.toml"]}
        code, _ = self.run_check({}, {"trail_atlas.toml": edited(manifest='"sunrise-mod.toml"')}, files=files)
        self.assertEqual(code, 0)


if __name__ == "__main__":
    unittest.main()
