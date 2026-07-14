# AIRS FastAPI Frontend Integration Specification

Source reviewed: route handlers in `app/api/routes`, Pydantic schemas in `app/schemas`, auth/RBAC middleware in `app/middleware`, domain services in `app/services`, enums/models in `app/models` and `app/enums`, and exception handlers.

Base URL: `http://localhost:8002`

API prefix: `/airs`

## Shared Integration Rules

### Authentication

All endpoints require `Authorization: Bearer <JWT>` except:

- `GET /health`
- `GET /docs`
- `GET /redoc`
- `GET /openapi.json`

JWT middleware validates:

- Authorization header must start with `Bearer `.
- JWT header must be well formed.
- JWT must validate against JWKS using `RS256`.
- If issuer is configured, issuer must match.
- Expired tokens are rejected.
- Token must include `user_id` for RBAC dependencies.

JWT middleware error shape:

```json
{
  "status_code": 401,
  "message": "Missing or invalid Authorization header"
}
```

Application HTTP error shape:

```json
{
  "success": false,
  "message": "Error message",
  "data": null
}
```

FastAPI request validation error shape:

```json
{
  "detail": [
    {
      "type": "string_too_short",
      "loc": ["body", "name"],
      "msg": "String should have at least 1 character",
      "input": ""
    }
  ]
}
```

Standard success wrapper:

```json
{
  "success": true,
  "message": "Success message",
  "data": {}
}
```

### Roles

Allowed JWT role values:

- `HR_ADMIN`
- `RECRUITER`
- `HIRING_MANAGER`

RBAC failure:

```json
{
  "success": false,
  "message": "Access denied. Required: HR_ADMIN",
  "data": null
}
```

### Common Field Types

- `UUID`: string UUID, for example `74e3a219-f35f-41c7-9c8a-9f0bd09c3c77`.
- `datetime`: ISO-8601 string, preferably timezone-aware, for example `2026-07-20T10:30:00Z`.
- `Decimal`: send as JSON number or string. Frontend should keep two decimal places for scoring weights.
- Boolean query params: use `true` / `false`.

### File Downloads and Exports

Download/export endpoints return raw file bytes, not `APIResponse`.

Frontend must:

- Set `responseType: "blob"` in Axios.
- Read `Content-Disposition` for filename.
- Handle `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, `application/pdf`, and DOCX MIME types.
- Expose `Content-Disposition` is configured in CORS.

### Universal Error Cases

Every secured endpoint can return:

- `401`: missing, malformed, expired, invalid issuer, invalid signature, or unavailable authentication service.
- `403`: JWT role not allowed by route dependency.
- `422`: FastAPI schema/path/query validation failure.
- `500`: unhandled server, database, Celery, storage, or downstream AI error.

No explicit `429` throttling exists in the inspected backend.

---

# Health and RBAC Test Endpoints

## 1. Health Check

### Endpoint Information

- Endpoint Name: Health
- HTTP Method: `GET`
- URL: `/health`
- Purpose: Check service availability.
- Authentication Required: No
- Required Permission: None

### Request Details

- Content Type: None
- Request Type: none
- Path Parameters: none
- Query Parameters: none
- Request Body: none

### Mandatory Validations

- None.

### Processing Flow

Receive request -> return static service status.

### Success Response

Status: `200`

```json
{
  "status": "ok",
  "service": "AIRS"
}
```

### Error Responses

- `500`: unexpected server failure.

### Frontend Requirements

- Use for uptime checks only.
- Do not send auth header unless globally configured.

### API Integration Example

```ts
const { data } = await axios.get("/health");
```

### Edge Cases

- Backend unavailable/network timeout.

### Dependency Information

- None.

### UI Mapping

- Usually not user-facing.

### Integration Checklist

- [ ] No auth dependency
- [ ] Timeout handling
- [ ] Health indicator mapping

## 2. Who Am I

### Endpoint Information

- Endpoint Name: Who Am I
- HTTP Method: `GET`
- URL: `/test/me`
- Purpose: Return decoded JWT payload attached by middleware.
- Authentication Required: Yes
- Required Permission: Any authenticated token

### Request Details

- Content Type: None
- Request Type: header only
- Header: `Authorization: Bearer <JWT>` required

### Mandatory Validations

- JWT middleware validation.

### Processing Flow

JWT middleware -> route reads `request.state.token_payload` -> response.

### Success Response

Status: `200`

```json
{
  "token_payload": {
    "user_id": "user-123",
    "email": "hr@example.com",
    "roles": ["HR_ADMIN"]
  },
  "message": "Token is valid"
}
```

### Error Responses

- `401`: JWT missing/invalid.

### Frontend Requirements

- Use only for debugging/session diagnostics.

### API Integration Example

```ts
const { data } = await axios.get("/test/me", authConfig);
```

### Edge Cases

- Token valid but missing optional claims like `email`.

### Dependency Information

- Must login first.

### UI Mapping

- Not intended for production UI.

### Integration Checklist

- [ ] Authorization header
- [ ] Debug-only usage

## 3. Any Role Test

### Endpoint Information

- Endpoint Name: Any Role Test
- HTTP Method: `GET`
- URL: `/test/any-role`
- Purpose: Return normalized user identity from token.
- Authentication Required: Yes
- Required Permission: Any authenticated token

### Request Details

- Header: `Authorization: Bearer <JWT>` required

### Mandatory Validations

- JWT valid.
- Token must contain `user_id`.
- Local shadow user is provisioned or re-keyed if needed.

### Processing Flow

JWT middleware -> `require_roles()` -> local user ensure -> response.

### Success Response

Status: `200`

```json
{
  "user_id": "user-123",
  "email": "hr@example.com",
  "roles": ["HR_ADMIN"]
}
```

### Error Responses

- `401`: token missing/invalid or token missing `user_id`.
- `500`: local user provisioning/database error.

### Frontend Requirements

- Use only for debugging auth setup.

### API Integration Example

```ts
const { data } = await axios.get("/test/any-role", authConfig);
```

### Edge Cases

- `roles` claim can be a string or array; backend normalizes to array.

### Dependency Information

- Must login first.

### UI Mapping

- Not intended for production UI.

### Integration Checklist

- [ ] Authorization header
- [ ] Display roles safely

---

# Job Descriptions

## 4. Create Job Description

### Endpoint Information

- Endpoint Name: Create Job Description
- HTTP Method: `POST`
- URL: `/airs/job-descriptions`
- Purpose: Submit text JD for asynchronous processing.
- Authentication Required: Yes
- Required Permission: Effective route user dependency allows `HR_ADMIN` or `RECRUITER`; decorator dependency also includes `HR_ADMIN`.

### Request Details

- Content Type: `application/json`
- Request Type: JSON body

Request Body:

| Field | Data Type | Required | Default | Allowed Values / Enum | Validation Rules | Min | Max | Regex | Nullable | Example |
|---|---:|---|---|---|---|---:|---:|---|---|---|
| `title` | string | Yes | none | any | Pydantic field length | 1 | 255 | none | No | `"Senior Python Developer"` |
| `raw_text` | string | Yes | none | any | Pydantic min length; duplicate content hash check | 1 | none | none | Schema type allows null but field is required; send non-null | `"We need FastAPI..."` |
| `jurisdiction` | string | Yes | none | backend has enum constants `GLOBAL`, `EU`, `US`, `IN`, but schema accepts any string | none | none | none | none | No | `"IN"` |
| `min_experience_years` | number | No | `null` | any float | no non-negative validation in request schema | none | none | none | Yes | `5` |
| `education_criteria.degree` | string | No | `null` | any | none | none | none | none | Yes | `"B.Tech"` |
| `education_criteria.field` | string | No | `null` | any | none | none | none | none | Yes | `"Computer Science"` |

### Mandatory Validations

- Schema validation: `title` required, length 1-255.
- Schema validation: `raw_text` required, min length 1.
- Service/route validation: content hash duplicate check against existing JD. Duplicate raises `409`.
- RBAC validation: `HR_ADMIN` / `RECRUITER` route user dependency, with an additional `HR_ADMIN` decorator dependency.
-noticeperoid string of number

### Processing Flow

Receive JSON -> JWT/RBAC -> Pydantic validation -> hash `raw_text` -> duplicate check -> create `task_id` -> mark validation stage -> enqueue `process_jd_document` Celery task -> return `202`.

### Success Response

Status: `202`

```json
{
  "success": true,
  "message": "Job Description submitted for processing.",
  "data": {
    "task_id": "74e3a219-f35f-41c7-9c8a-9f0bd09c3c77",
    "status": "QUEUED"
  }
}
```

Fields:

- `task_id`: Celery/document processing task UUID. Frontend polls processing status with it.
- `status`: initial status, currently `QUEUED`.

### Error Responses

- `401`: missing/invalid JWT.
- `403`: insufficient role.
- `409`: duplicate JD. Response data includes `existing_jd_id`, `title`, `version_number`.
- `422`: schema validation failure.
- `500`: Celery/database/hash/stage tracking failure.

### Frontend Requirements

- Disable submit until `title`, `raw_text`, and `jurisdiction` are non-empty.
- Trim visible text fields client-side, but send exact JD text.
- After `202`, show queued state and poll `/processing-status/{task_id}`.
- On `409`, offer "View Existing" and "Create New Version" UI.

### API Integration Example

```ts
await axios.post("/airs/job-descriptions", {
  title: "Senior Python Developer",
  raw_text: "Role responsibilities...",
  jurisdiction: "IN",
  min_experience_years: 5,
  education_criteria: { degree: "B.Tech", field: "Computer Science" }
}, authConfig);
```

React Query:

```ts
const createJD = useMutation({
  mutationFn: (payload: CreateJDPayload) =>
    axios.post("/airs/job-descriptions", payload, authConfig).then(r => r.data)
});
```

### Edge Cases

- Duplicate text with different title.
- Async task accepted but later fails.
- Negative experience is accepted by current schema; frontend should block it.
- `raw_text: null` may pass type shape but will fail downstream hashing; frontend should not send null.

### Dependency Information

- User must be authenticated.
- Celery worker/storage/DB must be available for full processing.

### UI Mapping

- `title` -> Text input
- `raw_text` -> Large textarea
- `jurisdiction` -> Select (`GLOBAL`, `EU`, `US`, `IN` recommended)
- `min_experience_years` -> Number input
- `education_criteria.degree` -> Text/select input
- `education_criteria.field` -> Text input

### Integration Checklist

- [ ] Authorization header
- [ ] Required fields
- [ ] Duplicate handling
- [ ] Processing polling
- [ ] Success toast
- [ ] Error toast
- [ ] Disable while submitting

## 5. Search Job Descriptions

### Endpoint Information

- Endpoint Name: Search Job Descriptions
- HTTP Method: `GET`
- URL: `/airs/job-descriptions`
- Purpose: Search/filter/paginate JD list.
- Authentication Required: Yes
- Required Permission: `HR_ADMIN`

### Request Details

- Request Type: query params

Query Parameters:

| Field | Data Type | Required | Default | Allowed Values / Enum | Validation Rules | Min | Max | Nullable | Example |
|---|---:|---|---|---|---|---:|---:|---|---|
| `search` | string | No | `null` | any | repository search | none | none | Yes | `"python"` |
| `jurisdiction` | string | No | `null` | any | repository filter | none | none | Yes | `"IN"` |
| `active` | boolean | No | `true` | `true`, `false` | repository filter | none | none | Yes | `true` |
| `source_format` | string | No | `null` | `TEXT`, `PDF`, `DOCX` recommended | repository filter | none | none | Yes | `"PDF"` |
| `page` | integer | No | `1` | any | `ge=1` | 1 | none | No | `1` |
| `size` | integer | No | `10` | any | `ge=1`, `le=100` | 1 | 100 | No | `20` |
| `sort_by` | string | No | `created_at` | repository-dependent | no schema enum | none | none | No | `"created_at"` |
| `order` | string | No | `desc` | `asc`, `desc` recommended | no schema enum | none | none | No | `"desc"` |

### Mandatory Validations

- Query validation: `page >= 1`, `1 <= size <= 100`.
- RBAC: `HR_ADMIN`.

### Processing Flow

JWT/RBAC -> query parsing -> build `JDSearchRequest` -> service repository search -> return paginated result.

### Success Response

Status: `200`

```json
{
  "success": true,
  "message": "Job Descriptions searched successfully.",
  "data": {
    "total": 42,
    "page": 1,
    "size": 10,
    "items": [
      {
        "id": "uuid",
        "job_id": "JOB_1001",
        "title": "Senior Python Developer",
        "version_number": 1,
        "jurisdiction": "IN",
        "source_format": "TEXT",
        "is_verified": "VERIFIED",
        "created_by": "user-123",
        "created_at": "2026-07-14T07:10:00Z"
      }
    ]
  }
}
```

### Error Responses

- `401`, `403`, `422`, `500`.

### Frontend Requirements

- Debounce `search`.
- Reset `page` to `1` when filters change.
- Render empty list state when `items.length === 0`.
- Use backend pagination, not client-only filtering.

### API Integration Example

```ts
const { data } = await axios.get("/airs/job-descriptions", {
  ...authConfig,
  params: { search, active: true, page: 1, size: 20, sort_by: "created_at", order: "desc" }
});
```

### Edge Cases

- Empty result.
- Invalid page/size.
- Unknown `sort_by` may fail in repository depending implementation.

### Dependency Information

- Must login as HR admin.

### UI Mapping

- `search` -> Search box
- `jurisdiction` -> Dropdown
- `active` -> Toggle
- `source_format` -> Dropdown
- `page`, `size` -> Pagination controls
- `sort_by`, `order` -> Sort controls

### Integration Checklist

- [ ] Authorization header
- [ ] Filter state
- [ ] Pagination
- [ ] Sorting
- [ ] Empty state
- [ ] Loading state

## 6. Create Job Description From File

### Endpoint Information

- Endpoint Name: Create Job Description From File
- HTTP Method: `POST`
- URL: `/airs/job-descriptions/from-file`
- Purpose: Upload PDF/DOCX JD and queue asynchronous processing.
- Authentication Required: Yes
- Required Permission: Effective route user dependency allows `HR_ADMIN` or `RECRUITER`; decorator dependency also includes `HR_ADMIN`.

### Request Details

- Content Type: `multipart/form-data`
- Request Type: form fields + uploaded file

Form Fields:

| Field | Type | Required | Default | Validation | Min | Max | Nullable | Example |
|---|---:|---|---|---|---:|---:|---|---|
| `title` | string | Yes | none | `min_length=1`, `max_length=255` | 1 | 255 | No | `"Backend Engineer"` |
| `jurisdiction` | string | Yes | none | none | none | none | No | `"US"` |
| `min_experience_years` | number | No | `null` | none | none | none | Yes | `4` |
| `education_degree` | string | No | `null` | none | none | none | Yes | `"BS"` |
| `education_field` | string | No | `null` | none | none | none | Yes | `"Computer Science"` |

Uploaded Files:

| Field | Type | Required | Allowed | Validation | Example |
|---|---:|---|---|---|---|
| `file` | file | Yes | `.pdf`, `.docx` | extension must be pdf/docx; MIME must match extension if supplied | `jd.pdf` |

Allowed MIME:

- `.pdf`: `application/pdf`
- `.docx`: `application/vnd.openxmlformats-officedocument.wordprocessingml.document`

### Mandatory Validations

- Schema/Form validation: required fields and title length.
- Service validation: extension must be PDF/DOCX.
- Service validation: content type must match extension when content type is present.
- Service validation: file is stored before response.
- No explicit max upload size found.

### Processing Flow

JWT/RBAC -> form parse -> validation stage validates file type -> storage stage uploads file -> queue Celery `process_jd_document` -> return `202`.

### Success Response

Status: `202`

```json
{
  "success": true,
  "message": "Job Description document submitted for processing.",
  "data": {
    "task_id": "uuid",
    "status": "QUEUED"
  }
}
```

### Error Responses

- `400`: unsupported file type or MIME mismatch.
- `401`, `403`, `422`, `500`.

### Frontend Requirements

- Accept only `.pdf,.docx`.
- Send `FormData`; do not set JSON content type manually.
- Show upload/progress state.
- Poll processing status after `202`.
- Client-side block very large files if product sets a size limit; backend currently does not.

### API Integration Example

```ts
const form = new FormData();
form.append("title", title);
form.append("jurisdiction", jurisdiction);
form.append("file", file);

await axios.post("/airs/job-descriptions/from-file", form, authConfig);
```

### Edge Cases

- Browser sends empty MIME type; backend only rejects mismatch if MIME exists.
- File extension missing.
- Storage succeeds but async extraction later fails.

### Dependency Information

- Must login.
- Supabase/storage and Celery must be available.

### UI Mapping

- `title` -> Text input
- `jurisdiction` -> Select
- `min_experience_years` -> Number input
- `education_degree` -> Text/select input
- `education_field` -> Text input
- `file` -> File upload

### Integration Checklist

- [ ] Authorization header
- [ ] Multipart request
- [ ] File type validation
- [ ] Upload loading state
- [ ] Processing polling
- [ ] Error toast

## 7. Get JD Processing Status

### Endpoint Information

- Endpoint Name: Get JD Processing Status
- HTTP Method: `GET`
- URL: `/airs/job-descriptions/processing-status/{task_id}`
- Purpose: Poll async JD pipeline progress.
- Authentication Required: Yes
- Required Permission: Effective route user dependency allows `HR_ADMIN`, `RECRUITER`, `HIRING_MANAGER`; decorator dependency also includes `HR_ADMIN`.

### Request Details

Path Parameters:

| Field | Type | Required | Validation | Example |
|---|---:|---|---|---|
| `task_id` | UUID | Yes | valid UUID | `"74e3a219-f35f-41c7-9c8a-9f0bd09c3c77"` |

### Mandatory Validations

- Path validation: valid UUID.
- Service validation: task must exist, else `404`.

### Processing Flow

JWT/RBAC -> UUID parse -> query stage execution logs -> return task and stage statuses.

### Success Response

Status: `200`

```json
{
  "success": true,
  "message": "Processing status retrieved successfully.",
  "data": {
    "task_id": "uuid",
    "overall_status": "RUNNING",
    "current_stage": "AI_EXTRACTION",
    "stages": [
      {
        "stage": "VALIDATION",
        "status": "SUCCESS",
        "error_message": null,
        "duration_ms": 42
      }
    ],
    "jd_id": null,
    "error_message": null
  }
}
```

Allowed stage values: `VALIDATION`, `STORAGE`, `TEXT_EXTRACTION`, `TEXT_CLEANING`, `AI_EXTRACTION`, `JSON_VALIDATION`, `SKILL_NORMALIZATION`, `EMBEDDING_GENERATION`, `PERSISTENCE`.

Allowed status values: `PENDING`, `RUNNING`, `SUCCESS`, `FAILED`, `SKIPPED`.

### Error Responses

- `404`: no processing task found.
- `401`, `403`, `422`, `500`.

### Frontend Requirements

- Poll every 2-5 seconds until terminal state.
- Stop polling on success/failure/dead terminal status.
- Show per-stage progress and error message.
- Navigate to `jd_id` once available.

### API Integration Example

```ts
const statusQuery = useQuery({
  queryKey: ["jd-processing", taskId],
  queryFn: () => axios.get(`/airs/job-descriptions/processing-status/${taskId}`, authConfig).then(r => r.data),
  refetchInterval: data => data?.data?.overall_status === "SUCCESS" ? false : 3000
});
```

### Edge Cases

- Task accepted but not yet visible in stage table.
- Partial stage failure.
- `jd_id` remains null until persistence completes.

### Dependency Information

- Requires `task_id` from create/update endpoints.

### UI Mapping

- `overall_status` -> Badge
- `current_stage` -> Progress stepper
- `stages` -> Timeline/table
- `error_message` -> Error panel

### Integration Checklist

- [ ] Authorization header
- [ ] Polling interval
- [ ] Terminal state handling
- [ ] Error stage display

## 8. Export Job Descriptions

### Endpoint Information

- Endpoint Name: Export Job Descriptions
- HTTP Method: `GET`
- URL: `/airs/job-descriptions/export`
- Purpose: Export filtered JD list.
- Authentication Required: Yes
- Required Permission: `HR_ADMIN`

### Request Details

Query Parameters:

| Field | Type | Required | Default | Validation | Example |
|---|---:|---|---|---|---|
| `search` | string | No | `null` | none | `"python"` |
| `jurisdiction` | string | No | `null` | none | `"IN"` |
| `active` | boolean | No | `true` | none | `true` |
| `source_format` | string | No | `null` | none | `"TEXT"` |
| `sort_by` | string | No | `created_at` | none | `"created_at"` |
| `order` | string | No | `desc` | none | `"desc"` |

### Mandatory Validations

- RBAC: `HR_ADMIN`.
- Repository/export service may validate sort/filter compatibility.

### Processing Flow

JWT/RBAC -> build search request -> service exports list -> streaming/binary response.

### Success Response

Status: `200`

Headers include:

- `Content-Disposition: attachment; filename="..."`

Body: file bytes.

### Error Responses

- `401`, `403`, `422`, `500`.

### Frontend Requirements

- Use blob handling.
- Preserve current list filters in export params.
- Show export loading state.

### API Integration Example

```ts
const res = await axios.get("/airs/job-descriptions/export", {
  ...authConfig,
  params: filters,
  responseType: "blob"
});
```

### Edge Cases

- Empty export should still download a file if backend supports it.
- Filename may vary by service implementation.

### Dependency Information

- Must login as HR admin.

### UI Mapping

- Export button on JD list.

### Integration Checklist

- [ ] Authorization header
- [ ] Blob response
- [ ] Filename extraction
- [ ] Filter forwarding

## 9. Export Single Job Description

### Endpoint Information

- Endpoint Name: Export Single Job Description
- HTTP Method: `GET`
- URL: `/airs/job-descriptions/{jd_id}/export`
- Purpose: Export one JD.
- Authentication Required: Yes
- Required Permission: `HR_ADMIN`

### Request Details

Path Parameters:

| Field | Type | Required | Validation | Example |
|---|---:|---|---|---|
| `jd_id` | UUID | Yes | valid UUID | `"uuid"` |

### Mandatory Validations

- UUID path validation.
- Service validates JD exists.

### Processing Flow

JWT/RBAC -> fetch JD -> export file -> binary response.

### Success Response

Status: `200`, binary body with download headers.

### Error Responses

- `404`: JD not found.
- `401`, `403`, `422`, `500`.

### Frontend Requirements

- Use blob handling.
- Trigger from JD details/list row actions.

### API Integration Example

```ts
await axios.get(`/airs/job-descriptions/${jdId}/export`, {
  ...authConfig,
  responseType: "blob"
});
```

### Edge Cases

- JD deleted/deactivated after list loaded.

### Dependency Information

- Requires JD id.

### UI Mapping

- Row action: Export.

### Integration Checklist

- [ ] Authorization header
- [ ] Blob handling
- [ ] 404 handling

## 10. Get All Active JDs

### Endpoint Information

- Endpoint Name: Get All Active JDs
- HTTP Method: `GET`
- URL: `/airs/job-descriptions/all-active-jds`
- Purpose: Return all active JD versions, usually for dropdowns.
- Authentication Required: Yes
- Required Permission: Effective route user dependency allows `HR_ADMIN`, `RECRUITER`, `HIRING_MANAGER`; decorator dependency also includes `HR_ADMIN`.

### Request Details

- No params/body.

### Mandatory Validations

- RBAC.
- Service filters `is_active_version=True`.

### Processing Flow

JWT/RBAC -> repository active JD lookup -> response.

### Success Response

Status: `200`

```json
{
  "success": true,
  "message": "Active Job Descriptions retrieved successfully.",
  "data": [
    {
      "id": "uuid",
      "job_id": "JOB_1001",
      "title": "Backend Engineer",
      "raw_text": "...",
      "extracted_json": {},
      "required_skills": {},
      "min_experience_years": 5,
      "notice_period": null,
      "education_criteria": {},
      "source_format": "TEXT",
      "jurisdiction": "IN",
      "version_number": 1,
      "is_active_version": true,
      "is_verified": "VERIFIED",
      "created_by": "user-123",
      "created_at": "2026-07-14T07:10:00Z",
      "updated_at": null
    }
  ]
}
```

### Error Responses

- `401`, `403`, `500`.

### Frontend Requirements

- Cache briefly for dropdowns.
- Use `title`, `job_id`, `version_number` as display labels.

### API Integration Example

```ts
const { data } = await axios.get("/airs/job-descriptions/all-active-jds", authConfig);
```

### Edge Cases

- Empty active JD list.
- Large list; backend has no pagination here.

### Dependency Information

- Needed before creating campaigns.

### UI Mapping

- JD selector dropdown.

### Integration Checklist

- [ ] Authorization header
- [ ] Empty dropdown state
- [ ] Cache invalidation after JD create/update/delete

## 11. Get Job Description By ID

### Endpoint Information

- Endpoint Name: Get Job Description By ID
- HTTP Method: `GET`
- URL: `/airs/job-descriptions/{jd_id}`
- Purpose: Retrieve full JD detail.
- Authentication Required: Yes
- Required Permission: Effective route user dependency allows `HR_ADMIN`, `RECRUITER`, `HIRING_MANAGER`; decorator dependency also includes `HR_ADMIN`.

### Request Details

Path Parameters:

| Field | Type | Required | Validation | Example |
|---|---:|---|---|---|
| `jd_id` | string | Yes | service expects existing id; route does not enforce UUID | `"uuid"` |

### Mandatory Validations

- Service validation: JD must exist.

### Processing Flow

JWT/RBAC -> service lookup -> map model to response -> return.

### Success Response

Same `GetJDResponse` structure as endpoint 10, wrapped in `APIResponse`.

### Error Responses

- `404`: `Job Description with ID {jd_id} not found.`
- `401`, `403`, `500`.

### Frontend Requirements

- Show full JD text and extracted fields.
- Handle deleted/missing JD from stale links.

### API Integration Example

```ts
const { data } = await axios.get(`/airs/job-descriptions/${jdId}`, authConfig);
```

### Edge Cases

- Non-UUID string reaches service and may return 404/DB error depending repository.

### Dependency Information

- Requires JD id from list/search/status response.

### UI Mapping

- `title` -> Header/input
- `raw_text` -> Read-only text area/editor
- `required_skills` -> Skill chips
- `is_verified` -> Badge
- `source_format` -> Badge

### Integration Checklist

- [ ] Authorization header
- [ ] 404 route state
- [ ] Null extracted fields handling

## 12. Update Job Description

### Endpoint Information

- Endpoint Name: Update Job Description
- HTTP Method: `PUT`
- URL: `/airs/job-descriptions/{jd_id}`
- Purpose: Update JD metadata or queue reprocessing if raw text changes.
- Authentication Required: Yes
- Required Permission: `HR_ADMIN`

### Request Details

- Content Type: `application/json`

Path Parameters:

| Field | Type | Required | Validation | Example |
|---|---:|---|---|---|
| `jd_id` | UUID | Yes | valid UUID | `"uuid"` |

Request Body:

| Field | Type | Required | Default | Validation | Min | Max | Nullable | Example |
|---|---:|---|---|---|---:|---:|---|---|
| `title` | string | Yes by schema default marker, but type optional | none | `max_length=255` | none | 255 | Yes | `"Updated Backend Engineer"` |
| `raw_text` | string | No | `null` | duplicate check if present | none | none | Yes | `"Updated JD text"` |
| `jurisdiction` | string | Yes | none | none | none | none | No | `"IN"` |
| `min_experience_years` | number | No | `null` | none | none | none | Yes | `6` |
| `education_criteria.degree` | string | No | `null` | none | none | none | Yes | `"B.Tech"` |
| `education_criteria.field` | string | No | `null` | none | none | none | Yes | `"CS"` |

### Mandatory Validations

- Path UUID validation.
- Service validation: JD must exist.
- Service validation: JD must be active version.
- Service validation: JD must not have active hiring campaign.
- Service validation: if `raw_text` is sent, duplicate check excludes current lineage.
- If `raw_text` changes, async reprocessing is queued and response status becomes `202`.

### Processing Flow

JWT/RBAC -> schema validation -> fetch existing JD -> active/campaign checks -> duplicate check -> if content changed queue reprocess -> else update metadata synchronously.

### Success Response

Metadata-only status: `200`

```json
{
  "success": true,
  "message": "Job Description updated successfully.",
  "data": {
    "id": "uuid",
    "title": "Updated Backend Engineer",
    "version_number": 1,
    "updated_by": "user-123"
  }
}
```

Reprocess status: `202`

```json
{
  "success": true,
  "message": "Job Description update submitted for reprocessing.",
  "data": {
    "task_id": "uuid",
    "status": "QUEUED"
  }
}
```

### Error Responses

- `400`: cannot update inactive version.
- `404`: JD not found.
- `409`: active campaign assigned or duplicate JD.
- `422`: schema validation.
- `401`, `403`, `500`.

### Frontend Requirements

- Disable edit when JD has active campaign if frontend knows that.
- If response is `202`, switch to processing status UI.
- If response is `200`, refresh detail/list immediately.
- Avoid sending `raw_text` unless user edited content.

### API Integration Example

```ts
await axios.put(`/airs/job-descriptions/${jdId}`, {
  title,
  raw_text: changedRawText ?? null,
  jurisdiction,
  min_experience_years,
  education_criteria
}, authConfig);
```

### Edge Cases

- Metadata update on inactive JD.
- Concurrent campaign assignment before save.
- Raw text unchanged vs changed controls sync/async response shape.

### Dependency Information

- Requires existing active JD.

### UI Mapping

- Same fields as create JD.

### Integration Checklist

- [ ] Authorization header
- [ ] UUID path
- [ ] Handle both 200 and 202
- [ ] Duplicate conflict UI
- [ ] Refresh list/detail

## 13. Delete Job Description

### Endpoint Information

- Endpoint Name: Delete Job Description
- HTTP Method: `DELETE`
- URL: `/airs/job-descriptions/{jd_id}`
- Purpose: Deactivate a JD.
- Authentication Required: Yes
- Required Permission: `HR_ADMIN`

### Request Details

Path Parameters:

| Field | Type | Required | Validation | Example |
|---|---:|---|---|---|
| `jd_id` | UUID | Yes | valid UUID | `"uuid"` |

### Mandatory Validations

- Service validation: JD must exist.
- Service validation: JD must not already be inactive.
- Service validation: active campaign restrictions may apply through service/repository.

### Processing Flow

JWT/RBAC -> UUID parse -> service deactivates JD -> audit/repository -> response.

### Success Response

Status: `200`

```json
{
  "success": true,
  "message": "Job Description deactivated successfully.",
  "data": {
    "id": "uuid",
    "title": "Backend Engineer",
    "version_number": 1,
    "updated_by": "user-123"
  }
}
```

### Error Responses

- `400`: already inactive.
- `404`: JD not found.
- `409`: active campaign assigned.
- `401`, `403`, `422`, `500`.

### Frontend Requirements

- Use confirmation modal.
- Refresh list after success.
- Treat as soft delete/deactivation.

### API Integration Example

```ts
await axios.delete(`/airs/job-descriptions/${jdId}`, authConfig);
```

### Edge Cases

- Stale list row already deactivated.
- JD tied to campaign.

### Dependency Information

- Requires JD id.

### UI Mapping

- Row action: Deactivate/Delete.

### Integration Checklist

- [ ] Authorization header
- [ ] Confirmation modal
- [ ] Conflict handling
- [ ] Refresh list

## 14. Download Job Description File

### Endpoint Information

- Endpoint Name: Download Job Description File
- HTTP Method: `GET`
- URL: `/airs/job-descriptions/{jd_id}/download`
- Purpose: Download original JD document or generated DOCX for text JD.
- Authentication Required: Yes
- Required Permission: Decorator says `HR_ADMIN`; route has no user dependency beyond decorator.

### Request Details

Path Parameters:

| Field | Type | Required | Validation | Example |
|---|---:|---|---|---|
| `jd_id` | UUID | Yes | valid UUID | `"uuid"` |

### Mandatory Validations

- Service validation: JD exists.
- Service validation: if source is PDF/DOCX, stored file path must exist.

### Processing Flow

JWT/RBAC -> fetch JD -> if TEXT render DOCX -> else download storage object -> return bytes with attachment header.

### Success Response

Status: `200`

Headers:

- `Content-Disposition: attachment; filename="safe_title.docx|pdf"`
- `Content-Type`: PDF or DOCX MIME

Body: binary bytes.

### Error Responses

- `404`: JD not found or stored document missing.
- `401`, `403`, `422`, `500`.

### Frontend Requirements

- Use blob response.
- Do not parse as JSON.

### API Integration Example

```ts
await axios.get(`/airs/job-descriptions/${jdId}/download`, {
  ...authConfig,
  responseType: "blob"
});
```

### Edge Cases

- TEXT JD always downloads generated DOCX.
- Storage object missing even if JD exists.

### Dependency Information

- Requires JD id.

### UI Mapping

- Download button.

### Integration Checklist

- [ ] Authorization header
- [ ] Blob handling
- [ ] Filename extraction

## 15. Update Job Description From File

### Endpoint Information

- Endpoint Name: Update Job Description From File
- HTTP Method: `PUT`
- URL: `/airs/job-descriptions/{jd_id}/from-file`
- Purpose: Replace JD file and queue reprocessing.
- Authentication Required: Yes
- Required Permission: `HR_ADMIN`

### Request Details

- Content Type: `multipart/form-data`

Path Parameters:

| Field | Type | Required | Validation | Example |
|---|---:|---|---|---|
| `jd_id` | UUID | Yes | valid UUID | `"uuid"` |

Form fields and file are the same as Create From File.

### Mandatory Validations

- File extension/MIME validation.
- Existing JD must exist.
- Existing JD must be active.
- Existing JD must not have active campaign.
- Duplicate/new-version validations run in `JDService.update_jd`.
- New file always triggers async reprocessing.

### Processing Flow

JWT/RBAC -> form parse -> validate/store file -> build update request -> service update returns reprocess payload -> queue Celery -> return `202`.

### Success Response

Status: `202`

```json
{
  "success": true,
  "message": "Job Description update submitted for reprocessing.",
  "data": {
    "task_id": "uuid",
    "status": "QUEUED"
  }
}
```

### Error Responses

- `400`: unsupported file type, MIME mismatch, inactive JD.
- `404`: JD not found.
- `409`: active campaign assigned.
- `401`, `403`, `422`, `500`.

### Frontend Requirements

- Same upload handling as create-from-file.
- Always transition to processing state after success.

### API Integration Example

```ts
const form = new FormData();
form.append("title", title);
form.append("jurisdiction", jurisdiction);
form.append("file", file);
await axios.put(`/airs/job-descriptions/${jdId}/from-file`, form, authConfig);
```

### Edge Cases

- File uploads to storage but later reprocessing fails.
- Stale JD inactive.

### Dependency Information

- Requires existing active JD.

### UI Mapping

- Same as create-from-file.

### Integration Checklist

- [ ] Authorization header
- [ ] Multipart request
- [ ] Poll task status
- [ ] Error handling

---

# Campaigns

## 16. Create Campaign

### Endpoint Information

- Endpoint Name: Create Campaign
- HTTP Method: `POST`
- URL: `/airs/campaigns`
- Purpose: Create hiring campaign for an active JD.
- Authentication Required: Yes
- Required Permission: `HR_ADMIN`

### Request Details

- Content Type: `application/json`

Request Body:

| Field | Type | Required | Default | Validation | Min | Max | Nullable | Example |
|---|---:|---|---|---|---:|---:|---|---|
| `name` | string | Yes | none | trim cannot be empty; length | 1 | 255 | No | `"Q3 Backend Hiring"` |
| `jd_id` | UUID | Yes | none | valid UUID; JD must exist/active/open | none | none | No | `"uuid"` |
| `max_candidates` | integer | No | `null` | `gt=0`, `le=100000` | 1 | 100000 | Yes | `100` |
| `deadline` | datetime | No | `null` | must be future if sent | none | none | Yes | `"2026-08-01T00:00:00Z"` |
| `weight_deterministic` | decimal | No | `30.00` | total weights must equal `100.00` | none | none | No | `30.00` |
| `weight_semantic` | decimal | No | `40.00` | total weights must equal `100.00` | none | none | No | `40.00` |
| `weight_ai` | decimal | No | `30.00` | total weights must equal `100.00` | none | none | No | `30.00` |
| `semantic_threshold` | decimal | No | `0.6500` | no schema min/max | none | none | No | `0.65` |
| `ai_threshold` | decimal | No | `50.00` | no schema min/max | none | none | No | `50.00` |
| `hiring_manager_id` | string | Yes | none | none | none | none | No | `"user-hm"` |
| `recruiter_id` | string | Yes | none | none | none | none | No | `"user-rec"` |

### Mandatory Validations

- Schema: name non-empty after trim, length 1-255.
- Schema: `max_candidates > 0` and `<= 100000`.
- Service: scoring weights sum exactly `100.00`.
- Service: JD exists.
- Service: JD is active version.
- Service: JD is not closed.
- Service: campaign name unique per organization.
- Service: deadline must be future.

### Processing Flow

JWT/RBAC -> schema validation -> scoring validation -> JD validation -> duplicate campaign name validation -> create campaign -> audit log -> commit -> response.

### Success Response

Status: `201`

```json
{
  "success": true,
  "message": "Campaign created successfully",
  "data": {
    "id": "uuid",
    "name": "Q3 Backend Hiring",
    "status": "ACTIVE",
    "jd_title": "Backend Engineer",
    "jd_version": 1,
    "max_candidates": 100,
    "hiring_manager": "user-hm",
    "candidate_count": 0,
    "shortlisted_count": 0,
    "deadline": "2026-08-01T00:00:00Z",
    "created_at": "2026-07-14T07:10:00Z",
    "approaching_cap": false,
    "deadline_soon": false
  }
}
```

### Error Responses

- `409`: duplicate campaign name.
- `422`: invalid JD, invalid deadline, scoring weights not 100, schema errors.
- `401`, `403`, `500`.

### Frontend Requirements

- Must load active JDs before create.
- Validate weights sum to 100 before submit.
- Use future-only date picker.
- Disable submit until required fields are valid.

### API Integration Example

```ts
await axios.post("/airs/campaigns", {
  name,
  jd_id: jdId,
  max_candidates: 100,
  deadline,
  weight_deterministic: "30.00",
  weight_semantic: "40.00",
  weight_ai: "30.00",
  semantic_threshold: "0.6500",
  ai_threshold: "50.00",
  hiring_manager_id,
  recruiter_id
}, authConfig);
```

### Edge Cases

- JD becomes inactive after selection.
- Duplicate campaign name after client-side check.
- Decimal precision mismatch causing sum comparison failure.

### Dependency Information

- Must create/select active JD first.
- Must know hiring manager and recruiter IDs from identity/user source.

### UI Mapping

- `name` -> Text input
- `jd_id` -> JD dropdown
- `max_candidates` -> Number input
- `deadline` -> Date/time picker
- scoring fields -> Number inputs/sliders
- `hiring_manager_id`, `recruiter_id` -> User selectors

### Integration Checklist

- [ ] Authorization header
- [ ] Active JD dropdown
- [ ] Weight sum validation
- [ ] Future deadline validation
- [ ] Duplicate name handling
- [ ] Success toast and redirect

## 17. Get All Campaigns

### Endpoint Information

- Endpoint Name: Get All Campaigns
- HTTP Method: `GET`
- URL: `/airs/campaigns/all`
- Purpose: Retrieve campaign list with filters.
- Authentication Required: Yes
- Required Permission: `HR_ADMIN` or `HIRING_MANAGER`

### Request Details

Query Parameters:

| Field | Type | Required | Default | Allowed Values | Validation | Example |
|---|---:|---|---|---|---|---|
| `search` | string | No | `null` | any | none | `"backend"` |
| `status` | enum | No | `null` | `ACTIVE`, `PAUSED`, `CLOSED` | enum validation | `"ACTIVE"` |
| `hiring_manager_id` | string | No | `null` | any | none | `"user-hm"` |
| `jd_id` | UUID | No | `null` | UUID | UUID validation | `"uuid"` |
| `has_deadline` | boolean | No | `null` | true/false | none | `true` |
| `show_closed` | boolean | No | `false` | true/false | none | `false` |

### Mandatory Validations

- Query enum validation for `status`.
- UUID validation for `jd_id`.

### Processing Flow

JWT/RBAC -> build `CampaignFilterRequest` -> service search -> response.

### Success Response

Status: `200`, data is `CampaignResponse[]`.

### Error Responses

- `401`, `403`, `422`, `500`.

### Frontend Requirements

- Debounce search.
- Toggle closed campaigns explicitly.
- Render `approaching_cap` and `deadline_soon` warnings.

### API Integration Example

```ts
await axios.get("/airs/campaigns/all", {
  ...authConfig,
  params: { search, status: "ACTIVE", show_closed: false }
});
```

### Edge Cases

- Empty campaign list.
- Invalid status query.

### Dependency Information

- Must login.

### UI Mapping

- `search` -> Search box
- `status` -> Status tabs/dropdown
- `has_deadline` -> Checkbox
- `show_closed` -> Toggle

### Integration Checklist

- [ ] Authorization header
- [ ] Filters
- [ ] Empty state
- [ ] Warning badges

## 18. Get Campaigns For HR Admin

### Endpoint Information

- Endpoint Name: Get Campaigns For HR Admin
- HTTP Method: `GET`
- URL: `/airs/campaigns/hr_admin`
- Purpose: Retrieve campaigns for current HR admin.
- Authentication Required: Yes
- Required Permission: Route decorator `HR_ADMIN`; current-user dependency only normalizes token.

### Request Details

- Header: `Authorization` required.
- No params/body.

### Mandatory Validations

- Current token must include `user_id`.

### Processing Flow

JWT/RBAC -> current user -> service query by current user id -> response.

### Success Response

Status: `200`, data is `CampaignResponse[]`.

### Error Responses

- `401`, `403`, `500`.

### Frontend Requirements

- Use for "My Campaigns" HR admin view.

### API Integration Example

```ts
await axios.get("/airs/campaigns/hr_admin", authConfig);
```

### Edge Cases

- No campaigns for current admin.

### Dependency Information

- Must login as HR admin.

### UI Mapping

- Campaign list/table.

### Integration Checklist

- [ ] Authorization header
- [ ] Empty state

## 19. Get Campaigns For Hiring Manager

Same as endpoint 18, but:

- URL: `/airs/campaigns/hiring_manager`
- Required Permission: `HIRING_MANAGER`
- Service filters by current user as hiring manager.

Frontend should use it for hiring-manager campaign dashboard.

## 20. Update Campaign Status

### Endpoint Information

- Endpoint Name: Update Campaign Status
- HTTP Method: `PUT`
- URL: `/airs/campaigns/{campaign_id}/status/update`
- Purpose: Toggle campaign active/paused using legacy status update route.
- Authentication Required: Yes
- Required Permission: `HR_ADMIN`

### Request Details

Path Parameters:

| Field | Type | Required | Validation | Example |
|---|---:|---|---|---|
| `campaign_id` | UUID | Yes | valid UUID | `"uuid"` |

No body. Route passes `CampaignStatus.PAUSED`; service toggles active to paused or paused to active.

### Mandatory Validations

- Campaign exists.
- Closed campaign cannot change status.

### Processing Flow

JWT/RBAC -> fetch campaign -> toggle status -> return APIResponse with null data.

### Success Response

Status: `200`

```json
{
  "success": true,
  "message": "Campaign status updated successfully.",
  "data": null
}
```

### Error Responses

- `400`: cannot change status of closed campaign.
- `404`: campaign not found.
- `401`, `403`, `422`, `500`.

### Frontend Requirements

- Prefer `PATCH /airs/campaigns/{campaign_id}` with `status` for richer pause/resume flow.
- Refresh campaign after success.

### API Integration Example

```ts
await axios.put(`/airs/campaigns/${campaignId}/status/update`, null, authConfig);
```

### Edge Cases

- Route name says update to paused but implementation toggles.

### Dependency Information

- Requires existing campaign.

### UI Mapping

- Pause/resume button.

### Integration Checklist

- [ ] Authorization header
- [ ] Confirmation UI
- [ ] Refresh campaign

## 21. Pause Impact Summary

- Endpoint Name: Pause Impact Summary
- Method/URL: `GET /airs/campaigns/{campaign_id}/pause-summary`
- Purpose: Show confirmation data before pausing campaign.
- Auth: Yes, `HR_ADMIN`
- Path: `campaign_id` UUID required.
- Validation: campaign exists; campaign status must be `ACTIVE`, else `409`.
- Success `200`:

```json
{
  "success": true,
  "message": "Pause impact summary retrieved successfully",
  "data": {
    "candidate_count": 25,
    "queued_task_count": 4,
    "processing_bulk_job_count": 1,
    "warning": "Pausing will stop new uploads, halt queued processing tasks, and suspend automated pipeline progression."
  }
}
```

Frontend: call before showing pause modal; disable confirm if endpoint returns `409`.

Checklist: auth header, UUID, loading state, conflict handling.

## 22. Resume Summary

- Endpoint Name: Resume Summary
- Method/URL: `GET /airs/campaigns/{campaign_id}/resume-summary`
- Purpose: Show confirmation data before resuming paused campaign.
- Auth: Yes, `HR_ADMIN`
- Path: `campaign_id` UUID required.
- Validation: campaign exists; campaign status must be `PAUSED`, else `409`.
- Success `200`:

```json
{
  "success": true,
  "message": "Resume summary retrieved successfully",
  "data": {
    "paused_task_count": 3,
    "pending_resume_count": 2,
    "estimated_processing_seconds": 225,
    "warning": "Confirming will re-queue all suspended tasks and re-enable uploads."
  }
}
```

Frontend: call before resume modal; display estimated load.

Checklist: auth header, UUID, loading state, conflict handling.

## 23. Get Campaign Weight Presets

- Endpoint Name: Get Campaign Weight Presets
- Method/URL: `GET /airs/campaigns/scoring-presets`
- Auth: Yes, `HR_ADMIN`
- Request: no params/body.
- Success: `200`, returns raw array, not `APIResponse`.

```json
[
  {
    "id": "uuid",
    "name": "Balanced",
    "description": "Default balanced weights",
    "weight_deterministic": "30.00",
    "weight_semantic": "40.00",
    "weight_ai": "30.00",
    "semantic_threshold": "0.6500",
    "ai_threshold": "50.00",
    "created_by": "SYSTEM",
    "created_at": "2026-07-14T07:10:00Z"
  }
]
```

Frontend: populate scoring preset dropdown. Handle raw array shape.

Errors: `401`, `403`, `500`.

## 24. Get Campaign By ID

- Endpoint Name: Get Campaign
- Method/URL: `GET /airs/campaigns/{campaign_id}`
- Auth: Yes, `HR_ADMIN` or `RECRUITER`
- Path: `campaign_id` UUID.
- Validation: campaign exists; associated JD exists.
- Success: `200`, `APIResponse<CampaignResponse>`.
- Errors: `404` campaign/JD missing, `401`, `403`, `422`, `500`.
- Frontend: stale-link handling; detail page loader.

## 25. Get Campaign Scoring Configuration

- Endpoint Name: Get Campaign Scoring Configuration
- Method/URL: `GET /airs/campaigns/{campaign_id}/scoring-config`
- Auth: JWT required by middleware; route has no explicit role dependency.
- Path: `campaign_id` UUID.
- Validation: campaign exists.
- Success: `200`, `APIResponse<CampaignScoringConfigurationResponse>`.

```json
{
  "success": true,
  "message": "Campaign scoring configuration retrieved successfully",
  "data": {
    "weight_deterministic": 30,
    "weight_semantic": 40,
    "weight_ai": 30,
    "semantic_threshold": 0.65,
    "ai_threshold": 50,
    "total_weight": 100,
    "formula": "final_score = ...",
    "layers": [
      {
        "layer": "semantic",
        "weight": 40,
        "threshold": 0.65,
        "description": "..."
      }
    ],
    "defaults": {
      "weight_deterministic": 30,
      "weight_semantic": 40,
      "weight_ai": 30,
      "semantic_threshold": 0.65,
      "ai_threshold": 50
    }
  }
}
```

Frontend: use for scoring panel and edit defaults.

## 26. Get Campaign Scoring History

- Endpoint Name: Get Campaign Scoring History
- Method/URL: `GET /airs/campaigns/{campaign_id}/scoring-history`
- Auth: JWT required by middleware; route has no explicit role dependency.
- Path: `campaign_id` UUID.
- Success: `200`, `APIResponse<CampaignWeightHistoryResponse>`.

```json
{
  "success": true,
  "message": "Scoring history retrieved successfully",
  "data": {
    "history": [
      {
        "changed_by": "HR Admin",
        "changed_at": "2026-07-14T07:10:00Z",
        "before": {},
        "after": {}
      }
    ]
  }
}
```

Frontend: history/timeline table; handle empty history.

## 27. Update Campaign Scoring Configuration

- Endpoint Name: Update Campaign Scoring Configuration
- Method/URL: `PUT /airs/campaigns/{campaign_id}/scoring-config`
- Auth: Yes, `HR_ADMIN`
- Path: `campaign_id` UUID.
- Content Type: `application/json`

Body:

| Field | Type | Required | Validation |
|---|---:|---|---|
| `weight_deterministic` | decimal | Yes | `0-100`, two decimal places |
| `weight_semantic` | decimal | Yes | `0-100`, two decimal places |
| `weight_ai` | decimal | Yes | `0-100`, two decimal places |
| `semantic_threshold` | decimal | Yes | `0-100`, two decimal places |
| `ai_threshold` | decimal | Yes | `0-100`, two decimal places |

Service validates weights sum to `100.00`. Response is raw `CampaignScoringConfigurationResponse`, not `APIResponse`.

Frontend: validate sum before submit, show confirmation if changing active campaign via PATCH endpoint when applicable.

## 28. Create Campaign Weight Preset

- Endpoint Name: Create Campaign Weight Preset
- Method/URL: `POST /airs/campaigns/scoring-presets`
- Auth: Yes, `HR_ADMIN`
- Response: raw `CampaignWeightPresetResponse`, status `201`.

Body:

| Field | Type | Required | Validation |
|---|---:|---|---|
| `name` | string | Yes | length 1-100; service trims |
| `description` | string | No | max 255 |
| `weight_deterministic` | decimal | Yes | weights total must equal 100 |
| `weight_semantic` | decimal | Yes | weights total must equal 100 |
| `weight_ai` | decimal | Yes | weights total must equal 100 |
| `semantic_threshold` | decimal | Yes | no schema min/max |
| `ai_threshold` | decimal | Yes | no schema min/max |

Errors: `400` duplicate preset or total weight invalid, `401`, `403`, `422`, `500`.

Frontend: preset form with weight-sum validation and duplicate-name handling.

## 29. Update Campaign Weight Preset

- Endpoint Name: Update Campaign Weight Preset
- Method/URL: `PUT /airs/campaigns/scoring-presets/{preset_id}`
- Auth: Yes, `HR_ADMIN`
- Path: `preset_id` UUID.
- Body: same as create preset; all fields required.
- Validation: preset exists in org, name not duplicate, weights total 100.
- Response: raw `CampaignWeightPresetResponse`.
- Errors: `400`, `404`, `401`, `403`, `422`, `500`.

Frontend: edit preset modal; refresh preset list.

## 30. Delete Campaign Weight Preset

- Endpoint Name: Delete Campaign Weight Preset
- Method/URL: `DELETE /airs/campaigns/scoring-presets/{preset_id}`
- Auth: Yes, `HR_ADMIN`
- Path: `preset_id` UUID.
- Validation: preset exists and belongs to org.
- Success: `204 No Content`, empty body.
- Errors: `404`, `401`, `403`, `422`, `500`.

Frontend: confirmation dialog; handle empty response.

## 31. Get Campaign Details

- Endpoint Name: Get Campaign Details
- Method/URL: `GET /airs/campaigns/{campaign_id}/details`
- Auth: Yes, `HR_ADMIN`, `HIRING_MANAGER`, or `RECRUITER`
- Path: `campaign_id` UUID.
- Validation: campaign exists; associated JD exists.
- Success: `200`, `APIResponse<CampaignDetailResponse>`.

Important response behavior:

- `scoring_configuration` is `null` for hiring-manager-only users.
- `hiring_manager` can be `null`.

Frontend: use this for detail page sections; hide scoring section when null.

## 32. Get Campaign Pipeline Summary

- Endpoint Name: Get Campaign Pipeline Summary
- Method/URL: `GET /airs/campaigns/{campaign_id}/pipeline-summary`
- Auth: Yes, `HR_ADMIN` or `RECRUITER`
- Path: `campaign_id` UUID.
- Success: `200`, `APIResponse<PipelineSummaryResponse>`.

```json
{
  "success": true,
  "message": "Pipeline summary retrieved successfully.",
  "data": {
    "campaign_id": "uuid",
    "total_candidates": 50,
    "stages": [
      { "stage": "APPLIED", "count": 50, "drop_off_pct": null },
      { "stage": "SCREENING", "count": 30, "drop_off_pct": 40.0 }
    ]
  }
}
```

Frontend: funnel chart/table; handle `drop_off_pct: null`.

## 33. Get Campaign Timeline

- Endpoint Name: Get Campaign Timeline
- Method/URL: `GET /airs/campaigns/{campaign_id}/timeline`
- Auth: Yes, `HR_ADMIN`
- Path: `campaign_id` UUID.

Query:

| Field | Type | Required | Default | Validation |
|---|---:|---|---|---|
| `limit` | integer | No | `20` | `1-100` |
| `offset` | integer | No | `0` | `>=0` |
| `event_type` | string | No | `null` | no enum validation |

Success: `200`, `APIResponse<CampaignTimelineResponse>`.

Frontend: infinite scroll/load more using `offset`; event filter dropdown.

## 34. Update Campaign

### Endpoint Information

- Endpoint Name: Edit Campaign Configuration
- HTTP Method: `PATCH`
- URL: `/airs/campaigns/{campaign_id}`
- Purpose: Update campaign name, lifecycle status, deadline, candidate cap, or scoring configuration.
- Authentication Required: Yes
- Required Permission: `HR_ADMIN`

### Request Details

Path: `campaign_id` UUID.

Body fields are all optional:

| Field | Type | Required | Default | Validation |
|---|---:|---|---|---|
| `name` | string | No | `null` | length 1-255; trimmed cannot be empty |
| `status` | enum | No | `null` | `ACTIVE`, `PAUSED`, `CLOSED`; service only allows `ACTIVE <-> PAUSED` |
| `deadline` | datetime | No | `null` | must be future if sent |
| `clear_deadline` | boolean | No | `false` | clears deadline when true |
| `max_candidates` | integer | No | `null` | `gt=0`, `le=100000`; cannot be less than current candidate count |
| `clear_max_candidates` | boolean | No | `false` | clears cap when true |
| `weight_deterministic` | decimal | No | `null` | merged weights must sum to 100 |
| `weight_semantic` | decimal | No | `null` | merged weights must sum to 100 |
| `weight_ai` | decimal | No | `null` | merged weights must sum to 100 |
| `semantic_threshold` | decimal | No | `null` | no schema min/max |
| `ai_threshold` | decimal | No | `null` | no schema min/max |
| `confirm_scoring_change` | boolean | No | `false` | required true for scoring changes on active campaign |

### Mandatory Validations

- Campaign exists.
- Closed campaigns cannot be edited; returns `403`.
- Status transition only `ACTIVE -> PAUSED` or `PAUSED -> ACTIVE`.
- Duplicate campaign name rejected.
- Candidate cap cannot be below current count.
- Deadline must be future.
- Scoring changes on active campaign require `confirm_scoring_change=true`.
- Weight total must equal `100.00`.
- At least one change required.

### Processing Flow

JWT/RBAC -> schema validation -> fetch campaign -> lifecycle guards -> apply changes -> pause/resume task side effects if status changed -> audit -> commit -> response.

### Success Response

Status: `200`, `APIResponse<CampaignResponse>`.

### Error Responses

- `403`: closed campaign edit attempt.
- `409`: duplicate campaign name can use 409; pause/resume summary conflicts use 409.
- `422`: invalid transition, cap below count, deadline past, scoring confirmation missing, weights invalid, no changes.
- `404`, `401`, `403`, `500`.

### Frontend Requirements

- Use separate confirmation dialogs for pause/resume.
- If scoring changed on active campaign, show acknowledgement checkbox and send `confirm_scoring_change: true`.
- Do not send unchanged fields.
- Use clear flags for removing deadline/cap.

### API Integration Example

```ts
await axios.patch(`/airs/campaigns/${campaignId}`, {
  name,
  max_candidates: 200,
  weight_deterministic: "30.00",
  weight_semantic: "40.00",
  weight_ai: "30.00",
  confirm_scoring_change: true
}, authConfig);
```

### Edge Cases

- User tries to close via PATCH; service rejects unsupported transition.
- Two users rename to same campaign name concurrently.
- Active campaign scoring change without confirmation.

### Dependency Information

- Existing campaign.

### UI Mapping

- `name` -> Text input
- `status` -> Pause/resume controls
- `deadline` / `clear_deadline` -> Date picker + clear button
- `max_candidates` / `clear_max_candidates` -> Number input + clear button
- scoring fields -> Number inputs/preset controls
- `confirm_scoring_change` -> Checkbox in confirmation modal

### Integration Checklist

- [ ] Authorization header
- [ ] Send partial body
- [ ] Pause/resume summaries
- [ ] Scoring confirmation
- [ ] Refresh details

---

# Campaign Candidates

## 35. Create Campaign Candidate

### Endpoint Information

- Endpoint Name: Create Campaign Candidate
- HTTP Method: `POST`
- URL: `/airs/campaign-candidates`
- Purpose: Add candidate/resume to campaign.
- Authentication Required: Yes
- Required Permission: JWT required; no explicit route role dependency.

### Request Details

JSON Body:

| Field | Type | Required | Validation | Example |
|---|---:|---|---|---|
| `campaign_id` | UUID | Yes | valid UUID; campaign must exist and be active | `"uuid"` |
| `candidate_id` | UUID | Yes | valid UUID; unique within campaign | `"uuid"` |
| `resume_id` | UUID | Yes | valid UUID | `"uuid"` |

### Mandatory Validations

- Campaign exists.
- Candidate not already in campaign.
- Campaign must be `ACTIVE`.
- If paused: `409` uploads not accepted.
- If closed: `409` uploads not allowed.
- If candidate cap reached: campaign auto-closes and `409` returned.

### Processing Flow

JWT -> schema validation -> campaign lookup -> duplicate check -> status/cap checks -> create campaign candidate with pipeline stage `UPLOADED` -> audit -> response.

### Success Response

Status: `201`

```json
{
  "success": true,
  "message": "Candidate added to campaign successfully.",
  "data": {
    "id": "uuid",
    "campaign_id": "uuid",
    "candidate_id": "uuid",
    "resume_id": "uuid",
    "pipeline_stage": "UPLOADED",
    "created_at": "2026-07-14T07:10:00Z"
  }
}
```

Note: response schema imports `PipelineStage` from constants, but service sets model `PipelineStage.UPLOADED`; frontend should render backend value as received.

### Error Responses

- `404`: campaign not found.
- `409`: duplicate candidate, paused/closed campaign, cap reached.
- `401`, `422`, `500`.

### Frontend Requirements

- Disable upload/add when campaign status is paused/closed.
- Handle duplicate candidate with inline message.
- Refresh candidate count and campaign status after `409` cap reached.

### API Integration Example

```ts
await axios.post("/airs/campaign-candidates", {
  campaign_id: campaignId,
  candidate_id: candidateId,
  resume_id: resumeId
}, authConfig);
```

### Edge Cases

- Campaign auto-closes during add.
- Candidate already added by another user.

### Dependency Information

- Must have campaign, candidate, and resume IDs from other modules.

### UI Mapping

- `campaign_id` -> Current campaign context
- `candidate_id` -> Candidate selector
- `resume_id` -> Resume selector/upload result

### Integration Checklist

- [ ] Authorization header
- [ ] Active campaign check
- [ ] Duplicate handling
- [ ] Refresh counts

## 36. Get Campaign Candidates

- Endpoint Name: Get Campaign Candidates
- Method/URL: `GET /airs/campaign-candidates/campaign/{campaign_id}`
- Auth: JWT required; no explicit route role dependency.
- Path: `campaign_id` UUID.
- Validation: campaign exists.
- Success: `200`, `APIResponse<CampaignCandidateResponse[]>`.
- Errors: `404`, `401`, `422`, `500`.
- Frontend: candidate table; empty state.

## 37. Delete Campaign Candidate

- Endpoint Name: Delete Campaign Candidate
- Method/URL: `DELETE /airs/campaign-candidates/{campaign_candidate_id}`
- Auth: JWT required; no explicit route role dependency.
- Path: `campaign_candidate_id` UUID.
- Validation: campaign candidate exists.
- Success:

```json
{
  "success": true,
  "message": "Campaign candidate deleted successfully.",
  "data": null
}
```

- Errors: `404`, `401`, `422`, `500`.
- Frontend: confirmation modal; refresh candidates and counts.

---

# Skill Curation

## 38. List Pending Unknown Skills

- Endpoint Name: List Pending Unknown Skills
- Method/URL: `GET /airs/skills/unknown`
- Auth: Yes, `HR_ADMIN`
- Success: `200`, `APIResponse<UnknownSkillItem[]>`.
- Fields: `id`, `raw_text`, `normalized_key`, `frequency`, `first_seen`, `last_seen`, `status`.
- Frontend: review queue sorted by backend frequency.
- Empty state: no pending unknown skills.

## 39. Map Unknown Skill To Existing

- Endpoint Name: Map Unknown Skill To Existing
- Method/URL: `POST /airs/skills/unknown/{unknown_skill_id}/map`
- Auth: Yes, `HR_ADMIN`
- Path: `unknown_skill_id` UUID.
- Body:

| Field | Type | Required | Default | Validation |
|---|---:|---|---|---|
| `target_skill_id` | UUID | Yes | none | skill must exist |
| `save_as_alias` | boolean | No | `false` | alias must be globally unique if true |

- Validations: unknown skill exists; target skill exists; alias cannot collide with another skill.
- Success: `200`, `APIResponse<UnknownSkillActionResponse>`.
- Errors: `400` alias collision, `404` missing unknown/target skill, `401`, `403`, `422`, `500`.
- Frontend: canonical skill selector, alias checkbox.

## 40. Promote Unknown Skill

- Endpoint Name: Promote Unknown Skill
- Method/URL: `POST /airs/skills/unknown/{unknown_skill_id}/promote`
- Auth: Yes, `HR_ADMIN`
- Path: `unknown_skill_id` UUID.
- Body:

| Field | Type | Required | Default | Validation |
|---|---:|---|---|---|
| `category` | string | No | `null` | none |

- Validations: unknown skill exists; raw skill must not already exist as canonical name or alias.
- Success: `200`, `APIResponse<PromotedSkillResponse>`.
- Errors: `400` already exists, `404`, `401`, `403`, `422`, `500`.
- Frontend: category field/dropdown; show created canonical skill.

## 41. Dismiss Unknown Skill

- Endpoint Name: Dismiss Unknown Skill
- Method/URL: `POST /airs/skills/unknown/{unknown_skill_id}/dismiss`
- Auth: Yes, `HR_ADMIN`
- Path: `unknown_skill_id` UUID.
- Body: none.
- Validation: unknown skill exists.
- Success: `200`, `APIResponse<UnknownSkillActionResponse>`.
- Errors: `404`, `401`, `403`, `422`, `500`.
- Frontend: confirmation action; remove item from queue.

## 42. Remap JD Skill

- Endpoint Name: Remap JD Skill
- Method/URL: `PUT /airs/skills/jd-skills/{jd_skill_id}/remap`
- Auth: Yes, `HR_ADMIN`
- Path: `jd_skill_id` UUID.
- Body:

| Field | Type | Required | Validation |
|---|---:|---|---|
| `new_canonical_skill_id` | UUID | Yes | target skill must exist |

- Validations: JD skill exists; target skill exists.
- Success: `200`, `APIResponse<JDSkillRemapResponse>`.
- Errors: `404`, `401`, `403`, `422`, `500`.
- Frontend: canonical skill selector and audit-friendly confirmation.

---

# Skill Ontology

## 43. Skill Ontology Summary

- Endpoint Name: Skill Ontology Summary
- Method/URL: `GET /airs/skill-ontology/summary`
- Auth: Yes, `HR_ADMIN`, `RECRUITER`, or `HIRING_MANAGER`
- Request: none.
- Success: `200`, `APIResponse<SkillOntologySummaryResponse>`.
- Data fields: `total_skills`, `verified_skills`, `unverified_skills`, `active_skills`, `inactive_skills`, `categories`.
- Frontend: dashboard metric cards.

## 44. Skill Ontology Categories

- Endpoint Name: Skill Ontology Categories
- Method/URL: `GET /airs/skill-ontology/categories`
- Auth: Yes, `HR_ADMIN`, `RECRUITER`, or `HIRING_MANAGER`
- Success: `200`, `APIResponse<SkillCategoryResponse[]>`.
- Fields: `category`, `count`.
- Frontend: filter dropdown; handle null/empty category naming if returned.

## 45. Export Skill Ontology

- Endpoint Name: Export Skill Ontology
- Method/URL: `GET /airs/skill-ontology/export`
- Auth: Yes, `HR_ADMIN`, `RECRUITER`, or `HIRING_MANAGER`
- Query: `search`, `category`, `confidence`, `is_active`.
- Validation: no schema enum for `confidence` here, but UI should send `verified` or `unverified`.
- Success: `200`, XLSX blob with `Content-Disposition`.
- Frontend: blob download, preserve list filters.

## 46. Search Parent Skills

- Endpoint Name: Search Parent Skills
- Method/URL: `GET /airs/skill-ontology/parents`
- Auth: Yes, `HR_ADMIN`, `RECRUITER`, or `HIRING_MANAGER`
- Query: `search` optional.
- Service limit: 20 parents.
- Success: `200`, `APIResponse<ParentSkillResponse[]>`.
- Frontend: async parent skill autocomplete with debounce.

## 47. Bulk Import Skill Ontology

- Endpoint Name: Bulk Import Skill Ontology
- Method/URL: `POST /airs/skill-ontology/import`
- Auth: Yes, `HR_ADMIN`
- Content Type: `multipart/form-data`
- File field:

| Field | Type | Required | Allowed | Validation |
|---|---:|---|---|---|
| `file` | file | Yes | `.xlsx` | filename suffix must be `.xlsx`; Excel reader must parse file |

- Success: `200`, `APIResponse<BulkImportResponse>`.

```json
{
  "success": true,
  "message": "Skill ontology bulk import completed.",
  "data": { "inserted": 10, "skipped": 2, "failed": 1 }
}
```

- Errors: `400` unsupported file or invalid Excel content, `401`, `403`, `422`, `500`.
- Frontend: accept `.xlsx`, upload progress, show inserted/skipped/failed summary.

## 48. List Skill Ontology

### Endpoint Information

- Endpoint Name: List Skill Ontology
- HTTP Method: `GET`
- URL: `/airs/skill-ontology`
- Purpose: Paginated skill ontology list.
- Authentication Required: Yes
- Required Permission: `HR_ADMIN`, `RECRUITER`, or `HIRING_MANAGER`

### Request Details

Query Parameters:

| Field | Type | Required | Default | Validation | Example |
|---|---:|---|---|---|---|
| `page` | integer | No | `1` | `ge=1` | `1` |
| `page_size` | integer | No | `20` | `1-100` | `20` |
| `search` | string | No | `null` | none | `"python"` |
| `category` | string | No | `null` | none | `"Programming"` |
| `confidence` | string | No | `null` | no route enum; UI should use `verified`, `unverified` | `"verified"` |
| `is_active` | boolean | No | `null` | none | `true` |

### Mandatory Validations

- Query validation for pagination.

### Processing Flow

JWT/RBAC -> query parsing -> repository list/count -> response.

### Success Response

Status: `200`

```json
{
  "success": true,
  "message": "Skill ontology list retrieved successfully",
  "data": {
    "items": [
      {
        "id": "uuid",
        "canonical_name": "Python",
        "aliases": ["py"],
        "category": "Programming",
        "parent_skill_name": null,
        "confidence": "verified",
        "source": "seed import",
        "occurrence_count": 12,
        "is_active": true,
        "created_at": "2026-07-14T07:10:00Z"
      }
    ],
    "page": 1,
    "page_size": 20,
    "total": 100
  }
}
```

### Error Responses

- `401`, `403`, `422`, `500`.

### Frontend Requirements

- Debounce search.
- Use server pagination.
- Filter by active/confidence/category.

### API Integration Example

```ts
await axios.get("/airs/skill-ontology", {
  ...authConfig,
  params: { page: 1, page_size: 20, search, confidence: "verified" }
});
```

### Edge Cases

- Empty skill list.
- Unknown confidence value may return empty results rather than 422.

### Dependency Information

- Must login.

### UI Mapping

- `search` -> Search input
- `category` -> Category dropdown
- `confidence` -> Segmented control/dropdown
- `is_active` -> Toggle
- `page`, `page_size` -> Pagination

### Integration Checklist

- [ ] Authorization header
- [ ] Debounced search
- [ ] Pagination
- [ ] Empty state

## 49. Create Skill

- Endpoint Name: Create Skill
- Method/URL: `POST /airs/skill-ontology`
- Auth: Yes, `HR_ADMIN`
- Body:

| Field | Type | Required | Default | Allowed Values | Validation |
|---|---:|---|---|---|---|
| `canonical_name` | string | Yes | none | any | min length 1; trimmed cannot be empty; unique |
| `aliases` | string[] | No | `[]` | any | backend trims, removes empty and duplicate aliases |
| `category` | string | No | `null` | any | trimmed if present |
| `parent_skill_id` | UUID | No | `null` | existing skill | must exist |
| `confidence` | enum | No | `unverified` | `verified`, `unverified` | enum validation |
| `source` | enum | No | `manual entry` | `manual entry`, `seed import`, `jd extraction`, `resume extraction` | enum validation |
| `is_active` | boolean | No | `true` | true/false | none |

- Success: `201`, `APIResponse<SkillCreateResponse>`.
- Errors: `409` duplicate canonical name, `422` empty canonical name or missing parent, `401`, `403`, `500`.
- Frontend: canonical name required; parent autocomplete; alias chip input.

## 50. Get Skill Detail

- Endpoint Name: Get Skill Detail
- Method/URL: `GET /airs/skill-ontology/{skill_id}`
- Auth: Yes, `HR_ADMIN`, `RECRUITER`, or `HIRING_MANAGER`
- Path: `skill_id` UUID.
- Validation: skill exists.
- Success: `200`, `APIResponse<SkillOntologyResponse>`.
- Response includes `children: [{ id, canonical_name }]`.
- Errors: `404`, `401`, `403`, `422`, `500`.
- Frontend: detail drawer/page.

## 51. Update Skill

- Endpoint Name: Update Skill
- Method/URL: `PATCH /airs/skill-ontology/{skill_id}`
- Auth: Yes, `HR_ADMIN`
- Path: `skill_id` UUID.
- Body fields all optional:

| Field | Type | Validation |
|---|---:|---|
| `canonical_name` | string | min length 1; trimmed cannot be empty; unique if changed |
| `aliases` | string[] | trimmed; empty/duplicate aliases removed |
| `category` | string/null | trimmed or null |
| `parent_skill_id` | UUID/null | cannot be self; parent must exist |
| `confidence` | enum | `verified`, `unverified` |
| `source` | enum | `seed`, `admin`, `auto_extracted` |
| `is_active` | boolean | none |

- Success: `200`, `APIResponse<SkillOntologyResponse>`.
- Errors: `404`, `409`, `422`, `401`, `403`, `500`.
- Frontend: send only changed fields; support clearing parent/category with null.

## 52. Update Skill Status

- Endpoint Name: Update Skill Status
- Method/URL: `PATCH /airs/skill-ontology/{skill_id}/status`
- Auth: Yes, `HR_ADMIN`
- Path: `skill_id` UUID.
- Body:

```json
{
  "is_active": false
}
```

- Validation: skill exists.
- Success: `200`, `APIResponse<SkillOntologyResponse>`.
- Errors: `404`, `401`, `403`, `422`, `500`.
- Frontend: activate/deactivate toggle with confirmation for deactivation.

---

# Cross-Endpoint Frontend Checklist

- [ ] Attach `Authorization: Bearer <JWT>` to all secured endpoints.
- [ ] Handle middleware 401 shape and APIResponse error shape separately.
- [ ] Handle FastAPI 422 validation shape from `detail`.
- [ ] Use UUID validation before route navigation where practical.
- [ ] Use ISO datetime strings with timezone.
- [ ] Keep scoring weights precise and summing to `100.00`.
- [ ] Use blob handling for exports/downloads.
- [ ] Use `FormData` for file upload endpoints.
- [ ] Poll JD processing after any `202` response with `task_id`.
- [ ] Render empty states for list endpoints.
- [ ] Refresh dependent lists after create/update/delete actions.
- [ ] Show role-aware UI for HR admin, recruiter, and hiring manager permissions.
