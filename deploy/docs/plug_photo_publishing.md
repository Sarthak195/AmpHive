# Plug-photo storage: driver-submitted charger photos on a private bucket

*Infra runbook for the "photos & reviews for public plugs" feature. Code:
[`backend/services/photo_publish.py`](../../backend/services/photo_publish.py)
(upload, download, delete) and `backend/routers/plugs.py`
(`POST /api/plugs/{id}/photos`, `GET /api/plugs/{id}/photos/{photo_id}/image`).*

Driver-submitted charger photos are stored in a **private** GCS bucket. The
bucket has no public binding and must never get one (no `allUsers`
`objectViewer`): a pending photo must not be reachable before a CPO approves
it. The backend serves the bytes itself from
`GET /api/plugs/{id}/photos/{photo_id}/image`, using the same keyless mechanism
as the firmware upload (the VM's own compute service account mints tokens from
the instance metadata server; no key file, no `gcloud` on the box, no
`google-cloud-storage`/`boto3` dependency). Every photo URL in the API
(`url`, `thumbnail_url`) is that relative `/api/...` route.

Photos are **held for approval**: an upload lands `pending` until a CPO/admin
approves it (CPO console, **Plug photos**, or
`POST /api/cpo/plug-photos/{id}/approve`). The image route enforces it:

| Status | Who gets the bytes | Cache-Control |
| --- | --- | --- |
| `approved` | anyone | `public, max-age=86400` |
| `pending`, `rejected` | a CPO of the plug's tenant, or an admin | `private, no-store` |

Everyone else gets a 404. Rejecting a photo (allowed for pending and approved
photos, and repeatable to retry a failed delete) deletes the stored object, so
the service needs read, create and delete.

## INFRA PREREQUISITE (apply once, BEFORE deploying the feature)

Since the September 2026 GCP migration the bucket is
**`gs://amphive-plug-photos-2`** (asia-south1, project
`project-6e290ffd-5954-4d48-aa9`, compute SA
`626015685685-compute@developer.gserviceaccount.com`). The code default is still
the pre-migration name `amphive-plug-photos`, so **prod must set the env var**
(step 3). State as read on 2026-11-15: the bucket exists with uniform
bucket-level access and no public binding, and the compute SA already holds
`roles/storage.objectAdmin`. So no IAM change is needed; only step 3 is left.

1. **Create the bucket** (skip if it already exists). Keep it private:

   ```bash
   P=project-6e290ffd-5954-4d48-aa9
   # asia-south1 (Mumbai), close to the users. No allUsers binding, ever.
   gcloud storage buckets create gs://amphive-plug-photos-2 --project $P        --location=asia-south1 --uniform-bucket-level-access        --public-access-prevention
   ```

2. **Grant the compute service account read + create + delete** on it.
   Unlike firmware (which only needs `objectCreator`), the backend reads photos
   back for the image route and deletes rejected ones, so grant `objectAdmin`:

   ```bash
   gcloud storage buckets add-iam-policy-binding gs://amphive-plug-photos-2 --project $P        --member=serviceAccount:626015685685-compute@developer.gserviceaccount.com        --role=roles/storage.objectAdmin
   ```

   The VM's `devstorage.read_write` access scope is already set (it backs the
   firmware upload + DB backups).

3. **Point the backend at the bucket** in the VM's `.env`, then recreate the
   backend container: `PLUG_PHOTO_GCS_BUCKET=amphive-plug-photos-2`.

Without step 2 the upload endpoint returns **502 "Failed to store the photo"**
(the GCS POST 403s) and the image route returns 502. Without step 3 both target
the non-existent old bucket and also 502.

## Notes

- **No multipart, no new dependency.** The upload endpoint reads the raw
  request body (same convention as the admin firmware upload); the frontend
  sends the file via `api.upload` as `application/octet-stream`. The backend
  sniffs the leading magic bytes and accepts only JPEG/PNG/WebP (up to 8 MiB),
  rejecting anything else with a 400 before spending a network round-trip.
- **Upload size.** nginx allows 9m on `/api/plugs/<id>/photos` (a location
  nested in the `/api/` block of `frontend/nginx.conf`) and
  `backend/main.py` allows 8 MiB on that POST path; every other route keeps the
  1 MiB cap. The deploy must ship the new `nginx.conf`, not only the built
  frontend assets.
- **Object layout:** `plug-<plug_id>/<uuid>.<ext>`. The GCS object key is
  stored on the `plug_photos` row (`object_name`) so a reject can delete it; the
  row's `url` holds the `gs://` URI for reference and is never sent to clients.
  The image route derives the content type from the extension, which was chosen
  from the sniffed type at upload.
- **Privacy:** only publicly-visible plugs (ungrouped or public-group) list
  photos to anonymous callers, mirroring the public map's plug filter. The image
  route itself serves any APPROVED photo by id (an `<img>` cannot send the
  Bearer token), so an approved photo of a private-group plug is fetchable by
  anyone who has its URL.
- **Takedown lag:** an approved image may stay in a browser cache for up to a
  day after a CPO rejects it (`max-age=86400`); the object itself is deleted at
  once.
