---
name: ilab-conjure-operations
description: "Operate iLab CONJURE WebUI safely through its HTTP API."
version: 1.0.0
author: iLab CONJURE contributors
license: AGPL-3.0-only
metadata:
  hermes:
    tags: [image-generation, ilab-conjure, task-history, downloads]
---

# iLab CONJURE Operations

## When to Use

Use this Skill to operate an iLab CONJURE WebUI deployment through its HTTP API. The bundled CLI uses only the Python standard library and contains no deployment URL, username, password, API key, Provider mapping, task history, or generated media.

Use it for health, Provider, queue, gallery, task, and output inspection; generation/edit submission; or explicitly authorized task cancellation, retry, and deletion.

## Procedure

1. Make the Skill available to Hermes by copying it or configuring the repository's `skills/` directory as an external Skill directory.
2. Configure the deployment URL and authentication outside the repository.
3. Run `health` and `providers` before operations that depend on deployment state.
4. Use `--dry-run` before paid generation/edit requests.
5. Require explicit user intent and `--yes` for paid or mutating actions.
6. Verify task terminal state and downloaded artifacts before reporting completion.

### Install

From a cloned repository:

```bash
mkdir -p "$HOME/.hermes/skills/creative"
cp -R skills/ilab-conjure-operations "$HOME/.hermes/skills/creative/"
```

Alternatively, point Hermes at the checked-out repository without copying it:

```yaml
skills:
  external_dirs:
    - ${ILAB_CONJURE_REPO}/skills
```

Run directly from the repository:

```bash
python3 skills/ilab-conjure-operations/scripts/ilab_conjure_ops.py --help
```

### Connection configuration

For an unauthenticated local deployment:

```bash
python3 skills/ilab-conjure-operations/scripts/ilab_conjure_ops.py \
  --base-url http://127.0.0.1:8787 --no-auth health
```

For a deployment protected by HTTP Basic Auth, keep credentials outside the repository in a mode-600 netrc file:

```text
machine conjure.example
  login example-user
  password example-password
```

```bash
chmod 600 "$HOME/.netrc"
export ILAB_CONJURE_URL="https://conjure.example"
python3 skills/ilab-conjure-operations/scripts/ilab_conjure_ops.py providers
```

Supported environment variables:

- `ILAB_CONJURE_URL`
- `ILAB_CONJURE_NETRC`
- `ILAB_CONJURE_OUTPUT_DIR`

Explicit CLI options override defaults. Never commit a populated netrc file, `.env`, API key, Provider credential, task export, prompt history, input image, or generated output.

### Safety policy

1. `health`, `providers`, `queue`, `gallery`, `tasks`, `task`, `latest`, and downloads are read-only.
2. `generate`, `edit`, and `clone` may consume paid quota; they require `--yes`. Use `--dry-run` first.
3. `cancel`, `retry`, and `delete` mutate state; they require `--yes` and explicit user intent.
4. Discover Provider IDs with `providers`; never assume a deployment-specific Provider name or route.
5. JSON output recursively redacts common password, token, key, authorization, cookie, and credential fields.
6. Treat enqueue success as pending work. When a finished result is required, use `--wait` and verify terminal task state.

### Read-only examples

```bash
CLI="python3 skills/ilab-conjure-operations/scripts/ilab_conjure_ops.py"
$CLI health
$CLI providers
$CLI queue
$CLI tasks --limit 20
$CLI latest
$CLI task <TASK_ID>
$CLI download <TASK_ID> --index 1 --output-dir ./downloads
```

Add `--json` before the command for complete redacted JSON.

### Paid generation and edit

Always discover the exact Provider ID and dry-run first:

```bash
$CLI providers
$CLI generate \
  --prompt-file ./prompt.txt \
  --provider <PROVIDER_ID> \
  --model gpt-image-2 \
  --size 1024x1536 \
  --quality low \
  --count 1 \
  --format png \
  --dry-run
```

After explicit approval, replace `--dry-run` with:

```text
--yes --wait --download --output-dir ./downloads
```

Edit example:

```bash
$CLI edit \
  --prompt-file ./edit-prompt.txt \
  --image ./input.png \
  --mask ./mask.png \
  --provider <PROVIDER_ID> \
  --dry-run
```

### Task mutations

Only with explicit authorization:

```bash
$CLI cancel <TASK_ID> --yes
$CLI retry <TASK_ID> --yes --wait
$CLI delete <TASK_ID> --yes
```

## Pitfalls

- Do not copy a real netrc, `.env`, deployment URL, Provider mapping, task export, prompt history, or generated media into the repository.
- `--no-auth` is intended for trusted local deployments; do not use it to work around a remote access policy.
- Provider IDs and model bindings are deployment-specific. Discover them with `providers` immediately before paid work.
- An enqueue response is not a completed generation. Poll until a terminal task status.
- Download URLs and output indexes come from task metadata; do not construct them from assumptions.

## Verification

For downloads report task ID, output index, local path, MIME type, byte size, and SHA-256. For paid work confirm a terminal task status before reporting completion. See `references/api.md` for the endpoint contract.
