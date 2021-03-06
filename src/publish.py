__doc__ = """

Script that publishes verified schemas to the objectstore.

Course of action is as follows:

* A zipfile of the master branch of the 'amsterdam-schema' repo is fetched from github
* The zip is unpacked to a temporary directory
* The relevant schema files (metaschema + datasets) are pushed to the objectstore
* An json file with an index of the datasets is pushed to the the objectstore
* It is possible to publish the local schema files to the objectstore using the --use-local flag

"""
import json
import logging
from io import BytesIO
import os
from pathlib import Path
from os.path import splitext
import click
import in_place
import requests
from tempfile import TemporaryDirectory
import shutil
from zipfile import ZipFile
from swiftclient.service import SwiftError, SwiftService, SwiftUploadObject

logger = logging.getLogger("__name__")


publishable_prefixes = ("datasets", "schema@")
DEFAULT_BASE_URL = "https://schemas.data.amsterdam.nl"
DEFAULT_GITHUB_URL = (
    "https://github.com/Amsterdam/amsterdam-schema/archive/develop.zip"
    if os.getenv("DATAPUNT_ENVIRONMENT", "acceptance") == "acceptance"
    else "https://github.com/Amsterdam/amsterdam-schema/archive/master.zip"
)


def fetch_publishable_paths(paths):
    publishable_paths = []
    for path in paths:
        path_parts = path.split("/")
        if path_parts[1].startswith(publishable_prefixes) and path_parts[-1]:
            publishable_paths.append(path_parts)
    return publishable_paths


def extract_and_fetch_paths(github_url, temp_dir):
    response = requests.get(github_url, stream=True)
    tmp_file = Path(temp_dir) / "out.zip"
    with open(tmp_file, "wb") as wf:
        shutil.copyfileobj(response.raw, wf)

    with ZipFile(tmp_file, "r") as zip_file:
        publishable_paths = fetch_publishable_paths(zip_file.namelist())
        zip_file.extractall(
            temp_dir,
            members=("/".join(path_parts) for path_parts in publishable_paths),
        )
    return publishable_paths


def fetch_repo_root():
    return Path(__file__).resolve().parents[1]


def fetch_local_as_publishable(repo_root):
    repo_root_name = repo_root.name
    publishable_paths = []
    for subdir in repo_root.iterdir():
        if subdir.is_dir() and subdir.name.startswith(publishable_prefixes):
            for dirpath, dirnames, filenames in os.walk(subdir):
                if filenames:
                    subpath = list(Path(dirpath).relative_to(repo_root).parts)
                    publishable_paths.extend(
                        [[repo_root_name] + subpath + [fn] for fn in filenames]
                    )

    return publishable_paths


def get_index_file_obj(publishable_paths):
    index = {}
    for path_parts in publishable_paths:
        if path_parts[1] != "datasets":
            continue
        folder, dataset_ext = path_parts[2:]
        dataset = splitext(dataset_ext)[0]
        index[folder] = f"{folder}/{dataset}"
    return BytesIO(json.dumps(index).encode("utf-8"))


def create_object_name(path_parts):
    """ We need some path mangling and move objects from version directories
        to the root with an @vx.y.z postfix, e.g.:
            schema@v1.1.1/row-meta-schema -> row-meta-schema@v1.1.1
            schema@v1.1.1/meta/auth -> meta/auth@v1.1.1
    """
    parts = path_parts[:]
    # Always remove json extension
    parts[-1] = splitext(parts[-1])[0]
    # For metaschema files, move to root level
    if "@" in parts[1]:
        _, version = parts[1].split("@")
        parts[-1] = f"{parts[-1]}@{version}"
        parts.pop(1)
    return "/".join(parts[1:])


def replace_schema_base_url(temp_dir, schema_base_url):
    """ Do a replacement of the schema_base_url, needed to
        valdidate schemas when served from another base url,
        e.g. for testing
    """
    for root, dirs, file_names in os.walk(temp_dir):
        root_path = Path(root)
        for file_name in file_names:
            file_path = root_path / file_name
            if file_path.suffix == ".json":
                with in_place.InPlace(root_path / file_name) as file:
                    for line in file:
                        line = line.replace(DEFAULT_BASE_URL, schema_base_url)
                        file.write(line)


@click.command()
@click.option(
    "--dp-env",
    envvar="DATAPUNT_ENVIRONMENT",
    default="acceptance",
    help="Override the environment to be used, values can be 'acceptance' or 'production'",
)
@click.option(
    "--github-url",
    envvar="GITHUB_ZIP_URL",
    default=DEFAULT_GITHUB_URL,
    help="Override the url to the zip on github (to use a specific branch for testing)",
)
@click.option(
    "--schema-base-url",
    envvar="SCHEMA_BASE_URL",
    help="Override the base url in schema files (for testing)",
)
@click.option(
    "--use-local",
    is_flag=True,
    help="Use local schema files, not the github zip archive",
)
def main(dp_env, github_url, schema_base_url, use_local):

    # We extract the zip, because otherwise we need a big set
    # of open file handles during upload, now we can use file-paths
    with TemporaryDirectory() as temp_dir:
        if use_local:
            repo_root = fetch_repo_root()
            files_root = repo_root.parent
            publishable_paths = fetch_local_as_publishable(repo_root)
        else:
            files_root = Path(temp_dir)
            publishable_paths = extract_and_fetch_paths(github_url, temp_dir)

        if schema_base_url is not None:
            replace_schema_base_url(temp_dir, schema_base_url)
        index_file_obj = get_index_file_obj(publishable_paths)

        with SwiftService() as swift:
            try:
                uploads = [
                    SwiftUploadObject(index_file_obj, object_name="datasets/index.json")
                ]
                for path_parts in publishable_paths:
                    object_name = create_object_name(path_parts)
                    uploads.append(
                        SwiftUploadObject(
                            str(files_root / "/".join(path_parts)),
                            object_name=object_name,
                            options={"header": ["content-type:application/json"]},
                        )
                    )
                for r in swift.upload(f"schemas-{dp_env}", uploads):
                    if not r["success"]:
                        logger.error(r["error"])

            except SwiftError as e:
                logger.exception(e.value)


if __name__ == "__main__":
    main()
