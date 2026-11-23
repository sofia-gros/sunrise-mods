use std::fs;
use std::io::{self, Write};
use std::path::Path;

use semver::Version;
use serde::Serialize;

use crate::{builds::Build, registry::Registry, schema::*};

#[derive(Serialize)]
struct Index<'a> {
    schema: u8,
    serial: u64,
    generated: &'a str,
    builds: &'a [Build],
    categories: &'a [Category],
    projects: Vec<IndexProject<'a>>,
    collections: Vec<IndexCollection<'a>>,
    revoked: &'a [Revocation],
}

#[derive(Serialize)]
struct IndexProject<'a> {
    id: &'a ProjectId,
    slug: &'a Slug,
    #[serde(rename = "mod")]
    mod_id: &'a ModId,
    kind: Kind,
    name: &'a Text,
    summary: &'a Text,
    description: &'a Text,
    authors: &'a [Text],
    owners: &'a [Login],
    license: &'a Text,
    homepage: Option<&'a Link>,
    issues: Option<&'a Link>,
    source: Option<&'a Link>,
    icon: String,
    screenshots: Vec<IndexScreenshot<'a>>,
    categories: &'a [Slug],
    repository: IndexRepository<'a>,
    workflow: &'a Text,
    releases: Vec<IndexRelease<'a>>,
}

#[derive(Serialize)]
struct IndexCollection<'a> {
    id: &'a Slug,
    name: &'a Text,
    summary: &'a Text,
    description: &'a str,
    owners: &'a [Login],
    icon: String,
    members: Vec<&'a Member>,
}

#[derive(Serialize)]
struct IndexScreenshot<'a> {
    path: String,
    caption: &'a str,
}

#[derive(Serialize)]
struct IndexRepository<'a> {
    id: RepositoryId,
    name: &'a RepositoryName,
    path: &'a PackagePath,
}

#[derive(Serialize)]
struct IndexRelease<'a> {
    version: &'a Version,
    published: &'a CalendarDate,
    sunrise: &'a BuildName,
    workflow: &'a SignerWorkflowRef,
    fits: &'a [BuildName],
    #[serde(skip_serializing_if = "<[BuildName]>::is_empty")]
    does_not_fit: &'a [BuildName],
    file: &'a Asset,
    dependencies: &'a [Dependency],
}

/// Copies a validated image to `pages/<owner>/<sha256>.<png|jpg>` and returns that public path.
fn stage(source: &Path, pages: &Path, owner: &str) -> io::Result<String> {
    let bytes = fs::read(source)?;
    // Select the public extension from the bytes, not the author's filename.
    let extension = if bytes.starts_with(b"\x89PNG\r\n\x1a\n") {
        "png"
    } else if bytes.starts_with(b"\xff\xd8\xff") {
        "jpg"
    } else {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "expected a PNG or JPEG image",
        ));
    };
    let digest = sunrise_release_sign::sha256_hex(&bytes);
    let file = format!("{digest}.{extension}");
    fs::write(pages.join(owner).join(&file), bytes)?;
    Ok(format!("pages/{owner}/{file}"))
}

pub fn write(
    registry: &Registry,
    checkout: &Path,
    serial: u64,
    generated: &str,
    dest: &Path,
) -> io::Result<()> {
    fs::create_dir_all(dest)?;
    let pages = dest.join("pages");
    // A publish never mixes the current page set with images in an earlier output.
    fs::create_dir(&pages)?;
    let projects = registry
        .projects
        .values()
        .filter(|entry| !entry.releases.is_empty())
        .filter_map(|entry| entry.page.as_ref().map(|page| (entry, page)))
        .map(|(entry, page)| {
            let project = &entry.project;
            let id = project.id.as_str();
            let source = checkout.join("registry/projects").join(id).join("page");
            fs::create_dir(pages.join(id))?;
            let stage = |path: &RelativePath| stage(&source.join(path.as_str()), &pages, id);
            let fit = registry.fits.get(&project.id);
            Ok(IndexProject {
                id: &project.id,
                slug: &project.slug,
                mod_id: &project.mod_id,
                kind: project.kind,
                name: &page.name,
                summary: &page.summary,
                description: &page.description,
                authors: &page.authors,
                owners: &project.owners,
                license: &page.license,
                homepage: page.homepage.as_ref(),
                issues: page.issues.as_ref(),
                source: page.source.as_ref(),
                icon: stage(&page.icon)?,
                screenshots: page
                    .screenshots
                    .iter()
                    .map(|shot| {
                        Ok(IndexScreenshot {
                            path: stage(&shot.path)?,
                            caption: &shot.caption,
                        })
                    })
                    .collect::<io::Result<_>>()?,
                categories: &page.categories,
                repository: IndexRepository {
                    id: project.repository,
                    name: &entry.repository_name,
                    path: &project.path,
                },
                workflow: &project.workflow,
                releases: entry
                    .releases
                    .values()
                    .map(|release| {
                        let row = fit.and_then(|rows| rows.get(&release.version));
                        IndexRelease {
                            version: &release.version,
                            published: &release.published,
                            sunrise: &release.sunrise,
                            workflow: &release.workflow,
                            fits: row.map_or(&[], |row| row.fits.as_slice()),
                            does_not_fit: row.map_or(&[], |row| row.does_not_fit.as_slice()),
                            file: &release.file,
                            dependencies: &release.dependencies,
                        }
                    })
                    .collect(),
            })
        })
        .collect::<io::Result<Vec<_>>>()?;
    let listed: std::collections::BTreeSet<_> = projects.iter().map(|project| project.id).collect();
    let collections = registry
        .collections
        .iter()
        .filter_map(|(id, collection)| {
            // A member appears once its project is listed; the card needs what every
            // collection needs: two members and one selected by default.
            let members: Vec<_> = collection
                .members
                .iter()
                .filter(|member| listed.contains(&member.project))
                .collect();
            (members.len() >= 2 && members.iter().any(|member| member.default))
                .then_some((id, collection, members))
        })
        .map(|(id, collection, members)| {
            let id_text = id.as_str();
            fs::create_dir(pages.join(id_text))?;
            let icon = checkout
                .join("registry/collections")
                .join(format!("{id_text}.png"));
            Ok(IndexCollection {
                id,
                name: &collection.name,
                summary: &collection.summary,
                description: &collection.description,
                owners: &collection.owners,
                icon: stage(&icon, &pages, id_text)?,
                members,
            })
        })
        .collect::<io::Result<_>>()?;
    let index = Index {
        schema: 2,
        serial,
        generated,
        builds: &registry.builds,
        categories: &registry.categories,
        projects,
        collections,
        revoked: &registry.revoked,
    };
    // Rename over the destination so an existing index.json symlink is never followed.
    let mut output = tempfile::NamedTempFile::new_in(dest)?;
    let mut writer = io::BufWriter::new(output.as_file_mut());
    serde_json::to_writer_pretty(&mut writer, &index)?;
    writer.write_all(b"\n")?;
    writer.flush()?;
    drop(writer);
    output.persist(dest.join("index.json"))?;
    Ok(())
}
