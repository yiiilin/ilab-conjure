# iLab CONJURE WebUI API reference

Default local Base URL: `http://127.0.0.1:8787`

A reverse proxy may add HTTP Basic Auth. The CLI can read credentials from a user-owned mode-600 netrc file. Credentials and deployment URLs must remain outside the repository.

## Read-only endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/health` | Application, authentication source, and worker status |
| GET | `/api/generation-catalog` | Available models, Providers, and bindings |
| GET | `/api/queue` | Waiting/running queue state |
| GET | `/api/tasks/recent?limit=N` | Recent tasks, maximum 500 |
| GET | `/api/tasks/{task_id}` | Full task metadata and output URLs |
| GET | `/api/tasks/{task_id}/outputs.zip` | Download task outputs as ZIP |
| GET | `/api/gallery` | Reference gallery |
| GET | output URL from task metadata | Download one generated image |

## Paid submission endpoints

`POST /api/generate` and `POST /api/edit` accept `multipart/form-data` and return an enqueued task object. Common fields include `prompt`, model/routing fields, size or ratio, quality, output format, output count, and prompt fidelity. Discover Provider and binding IDs from `/api/generation-catalog`; do not hardcode deployment-specific values.

Edit requests add repeated `images`, optional `mask`, optional `input_fidelity`, and optional JSON-string `focused_inpainting`. The enqueue response nests the ID under `task.task_id`.

## Mutation endpoints

| Method | Path | Effect |
|---|---|---|
| DELETE | `/api/queue/{task_id}` | Cancel/remove a queued task |
| POST | `/api/tasks/{task_id}/retry-failed` | Retry failed slots; may consume quota |
| DELETE | `/api/tasks/{task_id}` | Permanently delete task and outputs |

Terminal task states are `completed`, `failed`, `partial_failed`, and `cancelled`/`canceled`. Enqueue success is not generation success; poll task detail until a terminal state.
