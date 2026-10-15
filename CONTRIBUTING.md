# Contributing to TeleDrive

> **Audience:** maintainers and external contributors. This fork includes a single-container WebUI/REST/S3 service, a Telegram uploader and an Astro marketing site.

## 1. Before changing code

- Open a focused issue or describe the intended change in a pull request.
- Preserve the shared SQL/Telegram storage contract: REST, S3 and durable local-file jobs must not diverge into independent object catalogs.
- Keep Telegram physical deletion, legacy migration and production acceptance behind explicit, reviewed safety gates.
- Never commit `.env`, Telethon sessions, API hashes, Bot tokens, login codes, 2FA passwords, SQL credentials, original channel data or logs containing secrets.

## 2. Local development and validation

~~~bash
python -m pip install -r requirements.txt pytest
python -m pytest -q
python -m compileall -q teledrive web app tests
docker compose config --quiet
~~~

Run the dedicated [Docker fault-injection acceptance](docs/CI_WORKFLOWS.md) when changing upload durability, SQL state, scheduler behavior, recovery or container wiring:

~~~bash
bash scripts/run_docker_fault_injection.sh
~~~

For the command-line uploader, use the existing test suite and the [Docker fault-injection acceptance](docs/CI_WORKFLOWS.md) above.

Live Telegram tests require the private test channel and secrets described in the [CI and Test Guide](docs/CI_WORKFLOWS.md); never point the test workflow at production media.

## 3. Documentation standard

- Write all public Markdown documentation in **English**.
- Use one `#` title per file, a short purpose statement, and an ordered set of sections.
- Put user installation and examples in [README](README.md); test details in [CI and Test Guide](docs/CI_WORKFLOWS.md); status/scope/production approval in [Project Status](docs/IMPLEMENTATION_STATUS.md); backup procedures in [Backup and Restore](docs/BACKUP_RESTORE.md).
- Keep [TODO.md](TODO.md) to actionable checkboxes; record release notes on [GitHub Releases](https://github.com/babico/TeleDrive/releases), not in duplicate versioned Markdown files.
- Update references when renaming files. Ensure relative Markdown links resolve and do not assert that a CI skip equals a passing live test.

## 4. Review and security

Explain what changed, how it was tested, and any unsupported or unverified behavior. Keep new tests named for the **behavior** they cover, not roadmap phases such as P0/P1/P2.

For suspected vulnerabilities, use [Security Policy](docs/SECURITY.md) and disclose privately. Do not post exploit details or secrets in public issues.
