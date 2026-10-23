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
| **`runsite.email`** | Send transactional email, check delivery status, verify delivery webhooks | 🚧 in progress |
| **`runsite.storage`** | S3-compatible object storage: buckets, files, access keys | 🚧 in progress |
| **`runsite.secrets`** | Typed reads of configuration and secrets, manage service variables | 🚧 in progress |
| **`runsite.auth`** | End-user authentication for your apps | 🔜 coming soon |

Every client comes in a sync and an async flavour, raises typed errors and retries
transient failures.

## Install

```bash
pip install runsite-sdk              # email + secrets
pip install "runsite-sdk[storage]"   # + object storage (boto3)
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

# Email
runsite.email.send(
    to="user@example.com",
    subject="Welcome aboard",
    html="<p>Thanks for signing up!</p>",
)

# Secrets and configuration
database_url = runsite.secrets.get("DATABASE_URL")

# Object storage
bucket = runsite.storage.bucket()
bucket.upload("avatars/42.png", image_bytes)
```

Module-level shortcuts use a default client built from the environment. For tests,
multiple accounts or async code, create clients explicitly:

```python
from runsite import AsyncEmail

email = AsyncEmail(api_key="...")
await email.send(to="user@example.com", subject="Hi", text="Hello!")
```

## Configuration

| Variable | Used by | Set by |
|---|---|---|
| `EMAIL_API_KEY`, `EMAIL_API_URL` | `email` | the platform, when an email service is connected |
| `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_ENDPOINT_URL`, `S3_BUCKET`, `S3_REGION` | `storage` | the platform, when a bucket is connected |
| `RUNSITE_API_KEY` | managing secrets and buckets | you (`ak_live_...` from the dashboard) |

## Roadmap

- [ ] Email: send, status, webhook verification
- [ ] Secrets: typed reads, set and delete service variables
- [ ] Storage: buckets, objects, access keys
- [ ] First release on PyPI
- [ ] Auth: end-user authentication

## Related

- **Runsite CLI:** [github.com/runsite-platform/runsite-cli](https://github.com/runsite-platform/runsite-cli)
- **Public API:** [docs.runsite.app/api-reference/public-api](https://docs.runsite.app/api-reference/public-api/)
- **All docs:** [docs.runsite.app](https://docs.runsite.app)
- **Dashboard:** [runsite.app](https://runsite.app)

## License

MIT — see [LICENSE](https://github.com/runsite-platform/runsite-python/blob/main/LICENSE).
