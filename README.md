<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/runsite-platform/runsite-python/main/assets/logo-runsite-dark.svg">
    <img src="https://raw.githubusercontent.com/runsite-platform/runsite-python/main/assets/logo-runsite.svg" alt="Runsite" width="220" />
  </picture>
</p>

<h1 align="center">Runsite Python SDK</h1>

<p align="center">
  Use Runsite services from your Python code — transactional email, object storage,
  configuration and secrets, with end-user auth coming soon.
</p>

<p align="center">
  <a href="https://runsite.app">runsite.app</a> ·
  <a href="https://docs.runsite.app">Documentation</a> ·
  <a href="https://docs.runsite.app/api-reference/public-api/">API Reference</a> ·
  <a href="https://github.com/runsite-platform/runsite-cli">Runsite CLI</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/status-early%20development-8B5CF6" alt="Status" />
  <img src="https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white" alt="Python 3.10+" />
  <img src="https://img.shields.io/badge/typed-pydantic%20v2-E92063" alt="Typed" />
  <img src="https://img.shields.io/badge/sync%20%26%20async-supported-4169E1" alt="Sync and async" />
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT" />
</p>

---

> [!NOTE]
> The SDK is in early development. The API below is the planned shape and may change
> before the first release. Versions below `0.1.0` on PyPI are placeholders that reserve
> the package name and contain no functionality yet.

## What is the Runsite Python SDK?

`runsite` is a typed Python library that your application imports to use
[Runsite](https://runsite.app) services at runtime, the same way `boto3` is used for AWS.
When your code runs on Runsite, the clients configure themselves from environment
variables the platform sets for your service, so there is nothing to wire up.

## Features

| Module | What it does | Status |
|---|---|---|
| **`runsite.email`** | Send transactional email, check delivery status, verify delivery webhooks | ✅ 0.1 |
| **`runsite.storage`** | S3-compatible object storage: buckets, files, access keys | ✅ 0.1 |
| **`runsite.secrets`** | Typed reads of configuration and secrets, manage service variables | ✅ 0.1 |
| **`runsite.auth`** | End-user authentication for your apps | 🔜 coming soon |

Every client comes in a sync and an async flavour, raises typed errors and retries
transient failures of safe requests.

## Install

```bash
pip install runsite-sdk              # email + secrets
pip install "runsite-sdk[storage]"   # + object storage (boto3 >= 1.36)
```

or with [uv](https://docs.astral.sh/uv/):

```bash
uv add runsite-sdk
uv add "runsite-sdk[storage]"
```

Requires Python 3.10+. The package is installed as `runsite-sdk` and imported as `runsite`.

## Quick start

```python
import runsite

runsite.email.send(
    from_="Shop <orders@shop.com>",
    to="user@example.com",
    subject="Welcome aboard",
    html="<p>Thanks for signing up!</p>",
)

database_url = runsite.secrets.database_url()

bucket = runsite.storage.bucket()
bucket.upload("avatars/42.png", image_bytes)
```

`runsite.email`, `runsite.secrets` and `runsite.storage` are default clients built from the
environment. They read nothing until first used, so `import runsite` works anywhere. For tests,
several accounts or async code, create clients yourself:

```python
from runsite import AsyncEmail

async with AsyncEmail(api_key="...") as email:
    await email.send(from_="hi@shop.com", to="user@example.com", subject="Hi", text="Hello!")
```

## Configuration

| Variable | Used by | Set by |
|---|---|---|
| `EMAIL_API_KEY` | email | Runsite, when an email service is connected to the web service |
| `EMAIL_API_URL` | email | Runsite; the full endpoint, default `https://api.runsite.app/emails` |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_REGION` | storage files | Runsite, when a bucket is connected |
| `RUNSITE_SERVICE_ID` | secrets writes (default target service) | Runsite, in every service container |
| `DATABASE_URL`, `REDIS_URL`, ... | secrets reads | Runsite, when a database or cache is connected |
| `RUNSITE_API_KEY` | secrets writes, storage buckets and keys | you: an `ak_live_...` key from the dashboard (Settings → API keys) |
| `RUNSITE_BASE_URL` | same | optional, default `https://api.runsite.app` |

Calls that use `RUNSITE_API_KEY` act on the active workspace of the key's owner. Every client
also accepts its settings as constructor arguments, which win over the environment.
To read variables from a mapping instead of `os.environ` (handy in tests), pass `environ={...}`
to any client.

## Email

```python
import runsite

sent = runsite.email.send(
    from_="Shop <orders@shop.com>",
    to=["client@example.com"],
    cc="manager@shop.com",
    subject="Order #42",
    html="<p>Thanks!</p>",
    text="Thanks!",
    reply_to="support@shop.com",
)
print(sent.id, sent.status)  # status is always "sent" on success

record = runsite.email.get(sent.id)
print(record.status)  # queued, sent, failed, delivered, bounced or complained
```

- **Sandbox mode.** Until the sender domain is verified, `sent.is_sandbox` is `True`: the email goes
  out from the shared sandbox address (`sent.from_` shows the effective sender), `reply_to`
  defaults to your original `from_`, and every recipient must be verified first, otherwise
  `BadRequestError` (400).
- **Timeouts.** The email clients wait up to 60 seconds. `send` is never retried once the request
  left, so an email is never sent twice. `APITimeoutError` from `send` means the email may have
  been sent: check with `get` or a webhook before sending again.
- **Errors.** `EmailQuotaExceededError` (402, details in `.quota`), `RateLimitError` (429, the daily
  sending limit; there is no `Retry-After`, and an outage of the rate limiter also returns 429),
  `UpstreamError` (502) and `ServiceUnavailableError` (503, with `.retry_after`).
- At most 50 recipients across `to`, `cc` and `bcc`. The SDK does not check the key prefix: both
  `re_` and `rs_eml_` keys work.
- `AsyncEmail` has the same methods with `await`.

### Webhooks

```python
from fastapi import FastAPI, HTTPException, Request

import runsite

app = FastAPI()
WEBHOOK_SECRET = runsite.secrets.get("EMAIL_WEBHOOK_SECRET")


@app.post("/webhooks/email")
async def email_webhook(request: Request) -> dict[str, bool]:
    body = await request.body()  # the raw body; re-serialised JSON will not verify
    try:
        event = runsite.verify_webhook(body, request.headers, secret=WEBHOOK_SECRET)
    except runsite.WebhookVerificationError:
        raise HTTPException(status_code=400) from None
    if event.event == "bounced":
        mark_address_bounced(event.recipient)
    return {"ok": True}
```

- Deliveries are at-least-once and have no delivery id yet, so make the handler idempotent, for
  example by keying on `(event.email_id, event.event)`.
- The webhook secret is shown once, when the webhook is created in the dashboard, and cannot be
  rotated: delete the webhook and create a new one.
- `event.recipient` is the first `to` address only. `event.data` holds the whole body; keys other
  than `event`, `email_id`, `recipient` and `service_id` are provider-specific and may change.
- Unknown event names are accepted, not rejected. `runsite.WEBHOOK_EVENTS` lists the known ones.
- Only the `X-Runsite-Signature` and `X-Runsite-Timestamp` headers are read; the timestamp must
  be within 5 minutes (`tolerance=` changes that).

## Secrets

```python
import runsite

secrets = runsite.secrets
secrets.require("DATABASE_URL", "STRIPE_KEY")  # fail at startup, naming every missing key

workers = secrets.get_int("WORKERS", default=4)
debug = secrets.get_bool("DEBUG", default=False)  # true/false, 1/0, yes/no, on/off
hosts = secrets.get_list("ALLOWED_HOSTS", default=[])  # comma-separated
stripe_key = secrets.secret("STRIPE_KEY")  # prints as ********
headers = {"Authorization": f"Bearer {stripe_key.reveal()}"}
database_url = secrets.database_url()
```

An empty string counts as missing. A value that cannot be converted raises `InvalidSecretError`
without echoing the value; a missing key without a default raises `MissingSecretError`.

### Changing variables

```python
runsite.secrets.set("FEATURE_FLAGS", "beta,new-checkout", secret=False)
runsite.secrets.set_many({"SMTP_HOST": "mail.example.com", "SMTP_PASSWORD": "..."})
runsite.secrets.delete("OLD_TOKEN")

result = runsite.secrets.apply()
print("restarting" if result.restarting else f"applies on next start ({result.status})")
```

- `secret=None` (the default) keeps the stored `is_secret` flag of an existing variable and marks
  a new one as secret; `True` or `False` sets the flag. Keys and values are validated before any
  request. Setting a key that a connection injects (for example `DATABASE_URL`) overrides the
  injected value.
- **Why `apply()`.** Writes are stored at once but reach the running container only after
  `apply()` or the next deploy. `apply()` returns as soon as a rolling restart is queued
  (`result.restarting`). A service that is not running returns its status, and the variables apply
  on its next start. It raises `ConflictError` (409) when the service is being deleted or another
  start, stop, restart or deploy is running, and `BadRequestError` (400) when the service is blocked.
- **Permissions.** `list()` needs a `read`-scope API key. `set`, `set_many`, `delete` and `apply`
  need the `write` scope, and the key's owner must be an ADMIN of the workspace. Writes are limited
  to 30 per minute; listing is not.
- The target service is `RUNSITE_SERVICE_ID`, or pass `service_id=` to any write.

## Storage

Needs the `storage` extra.

```python
from pathlib import Path

import runsite

bucket = runsite.storage.bucket()  # S3_BUCKET and the injected S3_* keys
bucket.upload("reports/2026.pdf", Path("report.pdf"))  # a str or Path source is a file path
bucket.upload("notes/hello.txt", b"hello", content_type="text/plain")

data = bucket.download("notes/hello.txt")
bucket.download_to("reports/2026.pdf", "/tmp/report.pdf")
for item in bucket.list(prefix="reports/"):
    print(item.key, item.size)

bucket.copy("reports/2026.pdf", "archive/2026.pdf")  # within one bucket, up to 5 GB
url = bucket.presigned_url("reports/2026.pdf")
bucket.delete_many(["notes/hello.txt", "archive/2026.pdf"])
```

- Credentials come from `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY` and `S3_ENDPOINT_URL`, or from the
  `AWS_*` trio only when `AWS_ENDPOINT_URL_S3` is set. AWS keys are never combined with
  `S3_ENDPOINT_URL`.
- Pass `bytes` to upload text: a `str` source is read as a file path. Uploads above 8 MiB go in
  16 MiB parts, so one object can be up to about 30 GB.
- Presigned URLs work for about 15 minutes after signing, whatever `expires` says: the server
  only checks clock skew. Larger values are capped at 900 seconds with a warning.
- `bucket.public_url(key)` gives the anonymous URL of an object in a public bucket.
- Credentials injected by a dashboard connection cannot delete: `delete`, `delete_many` and
  aborting a multipart upload raise `StorageAccessDeniedError`. For cleanup jobs create a key with
  `delete=True` (below) and pass it to `storage.bucket(access_key=key)`.

### Buckets and access keys

These calls use `RUNSITE_API_KEY`.

```python
storage = runsite.storage
info = storage.create_bucket("shop-avatars", public=True)
storage.update_bucket("shop-avatars", public=False)

key = storage.create_key(bucket="shop-avatars", delete=True, name="cleanup-job")
cleanup_bucket = storage.bucket("shop-avatars", access_key=key)
print(key.secret_key.reveal())  # shown only in this response

for access_key in storage.list_keys():
    print(access_key.access_key, access_key.permissions)
storage.delete_key(key.id)
storage.delete_bucket("shop-avatars", confirm="shop-avatars")
```

- `create_key(bucket=...)` needs a `write`-scope API key; bucket keys can always read.
  `create_key()` without `bucket=` makes a workspace-wide key that can create and delete buckets
  over S3, so it needs an `admin`-scope API key and the workspace ADMIN role.
- `delete_bucket` and `delete_key` need an `admin`-scope API key. A deleted key may keep working for
  about 5 minutes. `delete_bucket` empties the bucket first and blocks until it is done; the SDK
  waits at least 120 seconds.
- Bucket names are global: a 409 on `create_bucket` can mean another account owns the name.
  Reaching the plan's bucket quota returns 400.
- `AsyncStorage` has the same bucket and key methods with `await`. Its `bucket()` returns an
  `AsyncBucket` with the same file methods; they run boto3 in a worker thread, so the event loop
  never blocks. `presigned_url` and `public_url` stay synchronous, and `.sync` gives the plain `Bucket`.
- Current limits: no browser CORS yet. If you build your own boto3 client against Runsite, set
  `request_checksum_calculation` and `response_checksum_validation` to `"when_required"`, as the
  SDK does.

## Error handling

Every error derives from `runsite.RunsiteError`:

| Error | When |
|---|---|
| `APIStatusError` | the API answered with an error status; subclasses below carry `.status_code`, `.detail`, `.code` and `.retry_after` |
| `BadRequestError`, `AuthenticationError`, `EmailQuotaExceededError`, `PermissionDeniedError`, `NotFoundError`, `ConflictError`, `UnprocessableEntityError`, `RateLimitError` | 400, 401, 402, 403, 404, 409, 422, 429 |
| `InternalServerError`, `UpstreamError`, `ServiceUnavailableError` | 5xx, 502, 503 |
| `APIConnectionError`, `APITimeoutError` | no usable response |
| `StorageError`, `ObjectNotFoundError`, `StorageAccessDeniedError` | object storage calls |
| `ConfigurationError` | a required setting is missing or invalid |
| `MissingSecretError`, `InvalidSecretError` | typed secret reads |
| `WebhookVerificationError` | a webhook did not verify |

```python
try:
    runsite.secrets.apply()
except runsite.RateLimitError as error:
    retry_in = error.retry_after or 60
except runsite.RunsiteError as error:
    log.warning("Runsite call failed: %s", error)
```

GET requests are retried on 429, 502, 503, 504 and network errors (up to `max_retries=2`, honouring
`Retry-After`). Writes are retried only when the request never reached the server.

## Roadmap

- [x] Email: send, status, webhook verification
- [x] Secrets: typed reads, set and delete service variables
- [x] Storage: buckets, objects, access keys
- [ ] First release on PyPI
- [ ] Auth: end-user authentication

## Related

- **Runsite CLI:** [github.com/runsite-platform/runsite-cli](https://github.com/runsite-platform/runsite-cli)
- **Public API:** [docs.runsite.app/api-reference/public-api](https://docs.runsite.app/api-reference/public-api/)
- **All docs:** [docs.runsite.app](https://docs.runsite.app)
- **Dashboard:** [runsite.app](https://runsite.app)

## License

MIT — see [LICENSE](https://github.com/runsite-platform/runsite-python/blob/main/LICENSE).
