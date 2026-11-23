use semver::Version;
use serde::{Deserialize, Serialize};
use sunrise_mod_package::{Manifest, index::stable_tag};
use sunrise_release_sign::{Channel, is_build};
use time::{Date, macros::format_description};
use url::Url;

macro_rules! text_type {
    ($name:ident, $expected:literal, $valid:expr) => {
        #[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord, Serialize)]
        #[serde(transparent)]
        pub struct $name(String);

        #[allow(dead_code)] // Only identifiers used for paths and lookups need view access.
        impl $name {
            pub fn parse(value: String) -> Result<Self, &'static str> {
                if ($valid)(&value) {
                    Ok(Self(value))
                } else {
                    Err($expected)
                }
            }

            pub fn as_str(&self) -> &str {
                &self.0
            }
        }

        impl<'de> Deserialize<'de> for $name {
            fn deserialize<D: serde::Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
                Self::parse(String::deserialize(d)?).map_err(serde::de::Error::custom)
            }
        }
    };
}

text_type!(
    ProjectId,
    "expected an 8-character base62 project ID",
    |s: &str| {
        // Modrinth: apps/labrinth/src/database/models/ids.rs:29–30, 236.
        s.len() == 8 && s.bytes().all(|b| b.is_ascii_alphanumeric())
    }
);
text_type!(
    Slug,
    "expected a slug using a-z, 0-9, '-' or '_'",
    |s: &str| {
        !s.is_empty()
            && s.bytes()
                .all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || matches!(b, b'-' | b'_'))
    }
);
text_type!(
    ModId,
    "expected an in-game ID valid in sunrise-mod.toml",
    |s: &str| {
        // `load_before` uses exactly `valid_mod_id`; call it directly when the pin exports it.
        let manifest = toml::Table::from_iter([
            ("id".into(), "registry_id_check".into()),
            ("name".into(), "Registry ID check".into()),
            ("version".into(), "0.0.0".into()),
            ("load_before".into(), toml::Value::Array(vec![s.into()])),
        ]);
        toml::to_string(&manifest).is_ok_and(|text| Manifest::parse(&text).is_ok())
    }
);
text_type!(
    Sha256,
    "expected a 64-digit lowercase hexadecimal SHA-256",
    |s: &str| {
        s.len() == 64
            && s.bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
    }
);
text_type!(
    RelativePath,
    "expected a relative path with forward slashes and no dot segments",
    |s: &str| {
        s.split('/').all(|part| {
            !part.is_empty()
                && part != "."
                && part != ".."
                && !part.contains(['\\', ':'])
                && !part.chars().any(char::is_control)
        })
    }
);
text_type!(
    CalendarDate,
    "expected a calendar date in YYYY-MM-DD form",
    |s: &str| {
        Date::parse(
            s,
            format_description!("[year repr:full sign:automatic]-[month]-[day]"),
        )
        .is_ok_and(|date| date.to_string() == s && !s.starts_with('-'))
    }
);
text_type!(
    RepositoryName,
    "expected a GitHub owner/repository name",
    github_repository
);

pub fn github_repository(s: &str) -> bool {
    s.split_once('/').is_some_and(|(owner, repo)| {
        github_login(owner)
            && !repo.is_empty()
            && repo != "."
            && repo != ".."
            && repo
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || matches!(b, b'.' | b'_' | b'-'))
    })
}

fn github_login(s: &str) -> bool {
    !s.is_empty() && s.bytes().all(|b| b.is_ascii_alphanumeric() || b == b'-')
}

text_type!(Login, "expected a GitHub login", github_login);
pub const RELEASE_WORKFLOW: &str = "sunrise-org/sunrise-mods/.github/workflows/release.yml";

text_type!(
    WorkflowUse,
    "expected the registry release workflow at a release-v<number> tag",
    |s: &str| {
        s.split_once('@')
            .is_some_and(|(path, tag)| path == RELEASE_WORKFLOW && workflow_tag(tag))
    }
);
text_type!(
    SignerWorkflowRef,
    "expected a signer workflow path at refs/tags/release-v<number>",
    |s: &str| {
        s.split_once('@').is_some_and(|(path, r)| {
            !path.is_empty() && r.strip_prefix("refs/tags/").is_some_and(workflow_tag)
        })
    }
);

fn workflow_tag(tag: &str) -> bool {
    tag.strip_prefix("release-v")
        .is_some_and(|n| !n.is_empty() && n.bytes().all(|b| b.is_ascii_digit()))
}

text_type!(
    Commit,
    "expected a full lowercase game commit SHA",
    sunrise_mod_package::manifest::revision
);

text_type!(
    BuildName,
    "expected a stable Sunrise tag or an exact Nightly/Dev build ID",
    |s: &str| { stable_tag(s).is_some() || Channel::of(s).is_some_and(|ch| is_build(ch, s)) }
);

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct ReleaseWorkflow {
    #[serde(rename = "uses")]
    pub uses: WorkflowUse,
}
text_type!(
    HttpsUrl,
    "expected an absolute HTTPS URL without credentials",
    |s: &str| { s.starts_with("https://") && web_url(s) }
);
text_type!(
    Link,
    "expected an absolute HTTP(S) URL without credentials",
    web_url
);
text_type!(Text, "expected nonblank text", |s: &str| !s
    .trim()
    .is_empty());

fn web_url(s: &str) -> bool {
    (s.starts_with("https://") || s.starts_with("http://"))
        && !s.contains('\\')
        && !s.chars().any(|c| c.is_control() || c.is_whitespace())
        && Url::parse(s).is_ok_and(|url| {
            url.has_host() && url.username().is_empty() && url.password().is_none()
        })
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Deserialize, Serialize)]
#[serde(transparent)]
pub struct RepositoryId(std::num::NonZeroU64);

impl RepositoryId {
    pub fn get(self) -> u64 {
        self.0.get()
    }
}

#[derive(Debug, PartialEq, Eq, PartialOrd, Ord, Deserialize, Serialize)]
#[serde(untagged)]
pub enum PackagePath {
    Root(RootPath),
    Folder(RelativePath),
}

#[derive(Debug, PartialEq, Eq, PartialOrd, Ord, Deserialize, Serialize)]
pub enum RootPath {
    #[serde(rename = ".")]
    Dot,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord, Deserialize, Serialize)]
#[serde(rename_all = "kebab-case")]
pub enum Kind {
    Mod,
    NativeMod,
    Modpack,
    Resourcepack,
    Shader,
    Shaderpack,
    Datapack,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Watched {
    #[serde(default)]
    pub repository: Vec<Repository>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Repository {
    pub id: RepositoryId,
    pub name: RepositoryName,
    pub package: Vec<Package>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Package {
    pub path: PackagePath,
    pub project: ProjectId,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Categories {
    pub category: Vec<Category>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Category {
    pub id: Slug,
    pub icon: Slug,
    pub kinds: Vec<Kind>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Project {
    pub id: ProjectId,
    pub slug: Slug,
    #[serde(rename = "mod")]
    pub mod_id: ModId,
    pub kind: Kind,
    pub owners: Vec<Login>,
    pub repository: RepositoryId,
    pub path: PackagePath,
    pub workflow: Text,
    #[serde(rename = "registered")]
    pub _registered: CalendarDate,
}

#[derive(Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Release {
    pub version: Version,
    pub tag: Text,
    pub published: CalendarDate,
    pub sunrise: BuildName,
    pub workflow: SignerWorkflowRef,
    pub file: Asset,
    pub page: Asset,
    #[serde(default)]
    pub dependencies: Vec<Dependency>,
}

#[derive(Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Asset {
    pub url: HttpsUrl,
    pub size: u64,
    pub sha256: Sha256,
    pub attestation: HttpsUrl,
}

#[derive(Clone, Deserialize, Serialize, PartialEq)]
#[serde(deny_unknown_fields)]
pub struct Dependency {
    #[serde(rename = "mod")]
    pub mod_id: ModId,
    pub project: Option<ProjectId>,
    pub required: bool,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Page {
    pub name: Text,
    pub summary: Text,
    pub description: Text,
    pub authors: Vec<Text>,
    pub license: Text,
    pub homepage: Option<Link>,
    pub issues: Option<Link>,
    pub source: Option<Link>,
    pub icon: RelativePath,
    #[serde(default)]
    pub categories: Vec<Slug>,
    pub screenshots: Vec<Screenshot>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Collection {
    pub id: Slug,
    pub name: Text,
    pub summary: Text,
    #[serde(default)]
    pub description: String,
    pub owners: Vec<Login>,
    pub members: Vec<Member>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Member {
    pub project: ProjectId,
    /// Whether the member is selected when a player first opens the collection.
    #[serde(default = "selected")]
    pub default: bool,
}

fn selected() -> bool {
    true
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Screenshot {
    pub path: RelativePath,
    pub caption: String,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Fit {
    #[serde(default)]
    pub release: Vec<FitRelease>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct FitRelease {
    pub version: Version,
    pub fits: Vec<BuildName>,
    #[serde(default, skip_serializing_if = "Vec::is_empty")]
    pub does_not_fit: Vec<BuildName>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Revocations {
    #[serde(default)]
    pub revoked: Vec<Revocation>,
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Revocation {
    pub project: ProjectId,
    pub sha256: Option<Sha256>,
    pub reason: Text,
    pub date: CalendarDate,
}
