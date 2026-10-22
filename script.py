"""Copy matching Google Drive archives into EcoTaxa, import them, then predict.

Install (pip or conda):
google-api-python-client
google-auth
requests

To access to Google Drive:
1. go to https://console.cloud.google.com/
2. clik on the project selector at the top left, create a new project and switch to it
3. go to https://console.cloud.google.com/apis/ and Click "Enable APIs and Services", search for "Google Drive API" and enable it
4. go to https://console.cloud.google.com/apis/credentials, click "Create Credentials"
5. select "Service account", give it a name, click "Create and Continue"
6. click on the created service account, go to the "Keys" tab, click "Add Key" and select "Create new key", select JSON and click "Create". This downloads a JSON file with the service account credentials. Keep it safe.
7. set the environment variable GOOGLE_APPLICATION_CREDENTIALS to the path of the downloaded JSON file, e.g.:
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/service-account.json
8. share the Google Drive folder with the service account email address (found in the JSON file under "client_email" or in the Google Cloud Console) with at least "Viewer" access.

To access EcoTaxa:
1. create an EcoTaxa account at https://ecotaxa.obs-vlfr.fr
2. set the environment variables ECOTAXA_USERNAME and ECOTAXA_PASSWORD to your EcoTaxa username and password, e.g.:
    export ECOTAXA_USERNAME=your_username (your email address)
    export ECOTAXA_PASSWORD=your_password
"""

# Manual configuration
google_drive_folder_id = ""
# = identification part after the "https://drive.google.com/drive/folders/" portion of the URL
ecotaxa_project_id = 0


## You should not have to touch anything below this line ----

import logging
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import google.auth
import requests
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload


ECOTAXA_API_URL = "https://ecotaxa.obs-vlfr.fr/api"
PREDICTION_SOURCE_PROJECT_ID = 21194
TUS_CHUNK_SIZE = 8 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 60
JOB_TIMEOUT_SECONDS = 6 * 60 * 60
JOB_POLL_INTERVAL_SECONDS = 10
DRIVE_SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]

LOGGER = logging.getLogger("ecotaxa_drive_import")
PREDICTION_FEATURES = ['fre.%area', 'fre.angle', 'fre.area', 'fre.area_exc', 'fre.circ.', 'fre.circex', 'fre.convex_area', 'fre.eccentricity', 'fre.elongation', 'fre.equivalent_diameter', 'fre.euler_number', 'fre.extent', 'fre.height', 'fre.label', 'fre.local_centroid_col', 'fre.local_centroid_row', 'fre.major', 'fre.meanhue', 'fre.meansaturation', 'fre.meanvalue', 'fre.minor', 'fre.perim.', 'fre.perimareaexc', 'fre.perimmajor', 'fre.solidity', 'fre.stdhue', 'fre.stdsaturation', 'fre.stdvalue', 'fre.width']
PREDICTION_CATEGORIES = [84963, 25828, 28257, 94603, 92767, 28296, 233, 92831, 31203, 93382, 92600, 31853, 28244, 85198, 28232, 92144, 31221, 11921, 28249, 31218, 92598, 28156, 92617, 17042, 31699, 28277, 85008, 85123, 13379, 92214, 95657, 85023, 55900, 18597, 17608, 31192, 31663, 31677, 31229, 95955, 92843, 84965, 12905, 31223, 45071, 31193, 55930, 31189, 11758, 54903, 13378, 31666, 78426, 92151]
PREDICTION_MAPPING = {'85008': 84963}


class EcoTaxaSession(requests.Session):
    """Authenticated session that rotates EcoTaxa access and refresh tokens."""

    def __init__(self, token_pair: dict[str, Any]) -> None:
        super().__init__()
        self._refresh_token = str(token_pair["refresh_token"])
        self._set_access_token(token_pair)

    def _set_access_token(self, token_pair: dict[str, Any]) -> None:
        self.headers["Authorization"] = f"Bearer {token_pair['access_token']}"
        lifetime = float(token_pair["expires_in"])
        self._access_token_expires_at = time.monotonic() + max(0, lifetime - 60)

    def _refresh_access_token(self) -> None:
        response = super().request(
            "POST",
            f"{ECOTAXA_API_URL}/token/refresh",
            json={"refresh_token": self._refresh_token},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        token_pair = response.json()
        # EcoTaxa refresh tokens are single-use, so retain each replacement.
        self._refresh_token = str(token_pair["refresh_token"])
        self._set_access_token(token_pair)

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        if url.startswith(ECOTAXA_API_URL) and time.monotonic() >= self._access_token_expires_at:
            self._refresh_access_token()
        return super().request(method, url, **kwargs)


def ecotaxa_session() -> EcoTaxaSession:
    """Authenticate through EcoTaxa's token API without interactive prompts."""
    username = os.environ.get("ECOTAXA_USERNAME")
    password = os.environ.get("ECOTAXA_PASSWORD")
    if not username or not password:
        raise RuntimeError("Set both ECOTAXA_USERNAME and ECOTAXA_PASSWORD")
    response = requests.post(
        f"{ECOTAXA_API_URL}/token",
        data={
            "grant_type": "password",
            "username": username,
            "password": password,
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    token_pair = response.json()
    return EcoTaxaSession(token_pair)


def get_json(
    session: requests.Session, path: str, *, params: dict[str, Any] | None = None
) -> Any:
    response = session.get(
        f"{ECOTAXA_API_URL}{path}",
        params=params,
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    return response.json()


def list_project_sample_ids(session: requests.Session) -> set[str]:
    samples = get_json(
        session,
        "/samples/search",
        params={"project_ids": str(ecotaxa_project_id), "id_pattern": "*"},
    )
    if not isinstance(samples, list):
        raise RuntimeError("EcoTaxa samples/search returned an unexpected response")
    return {
        str(sample["orig_id"])
        for sample in samples
        if isinstance(sample, dict) and sample.get("orig_id")
    }


def list_ecotaxa_zip_on_drive(
    drive_service: Any, folder_id: str
) -> list[dict[str, str]]:
    """List exact ecotaxa_<orig_id>.zip candidates in the configured Drive folder."""
    files: list[dict[str, str]] = []
    page_token: str | None = None
    query = (
        f"'{folder_id}' in parents and trashed = false "
        "and mimeType != 'application/vnd.google-apps.folder' "
        "and name contains 'ecotaxa_'"
    )
    while True:
        result = (
            drive_service.files()
            .list(
                q=query,
                fields="nextPageToken, files(id, name)",
                pageToken=page_token,
                pageSize=1000,
                # supportsAllDrives=True,
                # includeItemsFromAllDrives=True,
            )
            .execute()
        )
        for item in result.get("files", []):
            name = item.get("name", "")
            if name.startswith("ecotaxa_") and name.endswith(".zip"):
                orig_id = name[len("ecotaxa_") : -len(".zip")]
                if orig_id:
                    files.append({"id": item["id"], "name": name, "orig_id": orig_id})
        page_token = result.get("nextPageToken")
        if not page_token:
            return files


def download_drive_file(drive_service: Any, file_id: str, destination: Path) -> None:
    request = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True)
    with destination.open("wb") as output:
        downloader = MediaIoBaseDownload(output, request, chunksize=TUS_CHUNK_SIZE)
        done = False
        while not done:
            _, done = downloader.next_chunk()


def upload_file(session: requests.Session, archive: Path) -> str:
    """Upload an archive through EcoTaxa's multipart user-file endpoint."""
    with archive.open("rb") as source:
        response = session.post(
            f"{ECOTAXA_API_URL}/user_files/",
            files={"file": (archive.name, source, "application/zip")},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
    response.raise_for_status()
    server_path = response.json()
    if not isinstance(server_path, str) or not server_path:
        raise RuntimeError(
            f"EcoTaxa returned an invalid server path after uploading {archive.name}: "
            f"{server_path!r}"
        )
    # return only the portion of the server path that EcoTaxa expects for import, stripping the user-specific prefix and the .zip suffix
    server_path = re.sub(
        r"^/home/ecotaxa/ecotaxa/eco_users_files/ecotaxa_user\.\d+/",
        "",
        server_path,
    )
    server_path = re.sub(r"\.zip$", "", server_path)
    return server_path


def import_file(
    session: requests.Session, server_path: str, archive_name: str
) -> int:
    response = session.post(
        f"{ECOTAXA_API_URL}/file_import/{ecotaxa_project_id}",
        json={
            "source_path": server_path,
            "skip_loaded_files": False,
            "skip_existing_objects": True,
            "update_mode": "",
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    result = response.json()
    job_id = result.get("job_id", 0)
    if not isinstance(job_id, int) or job_id <= 0:
        raise RuntimeError(f"EcoTaxa did not create an import job for {archive_name}: {result}")
    return job_id


def wait_for_job(session: requests.Session, job_id: int) -> None:
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        job = get_json(session, f"/jobs/{job_id}/")
        state = job.get("state")
        LOGGER.info(
            "EcoTaxa job %s: state=%s progress=%s%%",
            job_id,
            state,
            job.get("progress_pct", 0),
        )
        if state == "F":
            return
        if state in {"E", "A"}:
            raise RuntimeError(f"EcoTaxa job {job_id} failed or needs attention: {job}")
        time.sleep(JOB_POLL_INTERVAL_SECONDS)
    raise TimeoutError(f"EcoTaxa job {job_id} did not finish before the timeout")


def start_prediction(
    session: requests.Session, categories: list[int], features: list[str]
) -> int:
    response = session.post(
        f"{ECOTAXA_API_URL}/object_set/predict",
        json={
            "filters": {"statusfilter": "NV"},
            "request": {
                "project_id": ecotaxa_project_id,
                "source_project_ids": [PREDICTION_SOURCE_PROJECT_ID],
                "categories": PREDICTION_CATEGORIES,
                "features": PREDICTION_FEATURES,
                "learning_limit": 5000,
                "pre_mapping": PREDICTION_MAPPING,
            },
        },
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    result = response.json()
    errors = result.get("errors", [])
    job_id = result.get("job_id", 0)
    if errors or not isinstance(job_id, int) or job_id <= 0:
        raise RuntimeError(f"EcoTaxa prediction could not be started: {result}")
    return job_id


def main() -> None:
    # Configure logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    # Validate user configuration
    if not google_drive_folder_id.strip():
        raise ValueError("Set google_drive_folder_id to the Google Drive folder ID")
    if ecotaxa_project_id <= 0:
        raise ValueError("Set ecotaxa_project_id to the destination EcoTaxa project ID")

    # Authenticate to Google Drive and EcoTaxa
    credentials, _ = google.auth.default(scopes=DRIVE_SCOPES)
    drive_service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    session = ecotaxa_session()

    existing_ecotaxa_samples = list_project_sample_ids(session)
    new_ecotaxa_zip_files = [
        ecotaxa_zip_file
        for ecotaxa_zip_file in list_ecotaxa_zip_on_drive(drive_service, google_drive_folder_id)
        if ecotaxa_zip_file["orig_id"] not in existing_ecotaxa_samples
    ]
    LOGGER.info(f"Found {len(new_ecotaxa_zip_files)} Drive archive(s) for samples not yet in EcoTaxa")

    temp_dir = tempfile.TemporaryDirectory(prefix="ecotaxa-import-")
    with temp_dir:
        temp_dir_path = Path(temp_dir.name)
        for zip_info in new_ecotaxa_zip_files:
            zip_file = temp_dir_path / zip_info["name"]
            LOGGER.info("Downloading %s", zip_file.name)
            download_drive_file(drive_service, zip_info["id"], zip_file)
            server_path = upload_file(session, zip_file)
            job_id = import_file(session, server_path, zip_file.name)
            wait_for_job(session, job_id)
            existing_ecotaxa_samples.add(zip_info["orig_id"])

    LOGGER.info(
        f"Starting prediction for non-validated objects with {len(categories)} categories and {len(features)} features",
    )
    prediction_job_id = start_prediction(session, categories, features)
    wait_for_job(session, prediction_job_id)
    LOGGER.info("Import and prediction workflow completed")


if __name__ == "__main__":
    main()