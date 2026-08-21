# Plug-photo storage — driver-submitted charger photos on a public bucket

*Infra runbook for the "photos & reviews for public plugs" feature. Code:
[`backend/services/photo_publish.py`](../../backend/services/photo_publish.py)
(upload/delete) and `backend/routers/plugs.py` (`POST /api/plugs/{id}/photos`).*

Driver-submitted charger photos are stored in a public-read GCS bucket and
served to the discovery map by their `https://storage.googleapis.com/...` URL —
exactly the shape [`ota_image_publishing.md`](ota_image_publishing.md) uses for
firmware, and the same keyless mechanism (the VM's own compute service account
mints tokens from the instance metadata server; no key file, no `gcloud` on the
box, no `google-cloud-storage`/`boto3` dependency).

Photos are **held for approval**: an upload lands `pending` (invisible on the
public map) until a CPO/admin approves it (CPO console → **Plug photos**, or
`POST /api/cpo/plug-photos/{id}/approve`). Rejecting a photo deletes the stored
object, so the service needs delete as well as create.

## INFRA PREREQUISITE (apply once, BEFORE deploying the feature)

1. **Create the bucket** (public-read, uniform bucket-level access):

   ```bash
   gcloud storage buckets create gs://amphive-plug-photos \
       --location=us-west1 --uniform-bucket-level-access
   gcloud storage buckets add-iam-policy-binding gs://amphive-plug-photos \
       --member=allUsers --role=roles/storage.objectViewer
   ```

2. **Grant the compute service account create + delete** on it — unlike
   firmware (which only needs `objectCreator`), rejecting a photo must delete
   the object, so grant `objectAdmin`:

   ```bash
   gcloud storage buckets add-iam-policy-binding gs://amphive-plug-photos \
       --member=serviceAccount:930756667383-compute@developer.gserviceaccount.com \
       --role=roles/storage.objectAdmin
   ```

   The VM's `devstorage.read_write` access scope is already set (it backs the
   firmware upload + DB backups).

Without step 2 the upload endpoint returns **502 "Failed to store the photo"**
(the GCS POST 403s). The bucket name is overridable via the
`PLUG_PHOTO_GCS_BUCKET` env var (default `amphive-plug-photos`).

## Notes

- **No multipart / no new dependency.** The upload endpoint reads the raw
  request body (same convention as the admin firmware upload) — the frontend
  sends the file via `api.upload` as `application/octet-stream`. The backend
  sniffs the leading magic bytes and accepts only JPEG/PNG/WebP (≤ 8 MiB),
  rejecting anything else with a 400 before spending a network round-trip.
- **Object layout:** `plug-<plug_id>/<uuid>.<ext>`. The GCS object key is
  stored on the `plug_photos` row (`object_name`) so a reject can delete it.
- **Privacy:** only publicly-visible plugs (ungrouped or public-group) expose
  photos to anonymous callers, mirroring the public map's plug filter.
