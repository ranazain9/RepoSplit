# RepoSplit 2.0 — Post-Hackathon Production SaaS Roadmap

This document outlines the step-by-step engineering plan to transition **RepoSplit 2.0** from a single-tenant engine into an enterprise-grade, multi-tenant commercial SaaS platform.

---

## 1. Architectural Evolution Overview

```
[ Current Hackathon State ]                   [ Production SaaS Target ]

┌────────────────────────────┐               ┌────────────────────────────────────────────────────────┐
│     FastAPI Web Server     │               │           CDN / API Gateway (Cloudflare / Envoy)       │
│  - Bundled UI (index.html) │               └───────────────────────────┬────────────────────────────┘
│  - In-memory Task Queue    │                                           │
│  - Local Disk (.reposplit) │               ┌───────────────────────────▼────────────────────────────┐
│  - SQLite / In-memory Runs │               │            FastAPI Control Plane (Stateless)           │
└────────────────────────────┘               │       - JWT / GitHub OAuth Authentication              │
                                             │       - Tenant Isolation & RBAC                        │
                                             │       - Rate Limiting & Quota Management               │
                                             └─────────────┬───────────────────────────┬──────────────┘
                                                           │                           │
                                           ┌───────────────▼───────────┐ ┌─────────────▼──────────────┐
                                           │   PostgreSQL / Cloud SQL  │ │ Cloud Object Storage (S3)  │
                                           │   - Users & Workspaces    │ │ - Uploaded source zips     │
                                           │   - Run Logs & Passports  │ │ - Modernized fleet zips    │
                                           └───────────────────────────┘ └────────────────────────────┘
                                                           │
                                           ┌───────────────▼──────────────────────────────────────────┐
                                           │           Distributed Worker Fleet (Celery/Redis)        │
                                           │   - Containerized AST & Louvain decomposition            │
                                           │   - Isolated Sandboxed Live Parity Execution             │
                                           └──────────────────────────────────────────────────────────┘
```

---

## 2. Phase-by-Phase Implementation Blueprint

### Phase 1: Authentication & Multi-Tenancy
- **Target:** Isolate user workspaces and protect modernization APIs.
- **Components:**
  1. **OAuth2 / JWT Provider:**
     - Integrate GitHub OAuth for developer self-serve login.
     - Optional enterprise SSO (SAML / OIDC for IBM Cloud / Okta).
  2. **API Keys:**
     - Enable headless CLI (`reposplit push --token <TOKEN>`) via scoped personal access tokens.
  3. **Data Schema Migration:**
     - Add `tenants`, `users`, and `runs` tables to PostgreSQL.
     - Associate every repository upload, AST graph, and Migration Passport with a `tenant_id`.

### Phase 2: Distributed Job Queue & Worker Decoupling
- **Target:** Prevent web server restarts from dropping modernization runs; scale compute independently.
- **Components:**
  1. **Task Queue:**
     - Replace `asyncio.create_task` with **Celery** or **ARQ** backed by **Redis**.
  2. **Real-Time Telemetry over Redis Pub/Sub:**
     - Broadcast Blackboard telemetry events across Redis channels (`runs:{run_id}:events`).
     - Web nodes subscribe to Redis channels and stream SSE directly to clients.
  3. **Resilience & Resumption:**
     - Leverage RepoSplit's existing `Supervisor.from_snapshot()` to automatically resume interrupted worker jobs from their last Blackboard checkpoint.

### Phase 3: Cloud Object Storage Integration
- **Target:** Store repositories and generated fleets durably in the cloud instead of server disk.
- **Components:**
  1. **S3 / IBM Cloud Object Storage / GCS Adapter:**
     - Abstract storage behind a standard `StorageBackend` protocol:
       - `upload_source(tenant_id, run_id, stream) -> s3_uri`
       - `download_fleet(tenant_id, run_id) -> presigned_url`
  2. **Direct Pre-Signed URL Uploads:**
     - Frontend uploads multi-gigabyte repository archives directly to Cloud Storage, bypassing the API server memory.

### Phase 4: Compute Sandboxing & Execution Security
- **Target:** Safely run live differential parity tests (`--live`) against arbitrary user monolith code.
- **Components:**
  1. **MicroVM / Sandboxed Containers:**
     - Execute the legacy monolith and generated microservices inside lightweight ephemeral microVMs (e.g. AWS Firecracker, gVisor, or Kubernetes ephemeral pods).
  2. **Automated Teardown & TTL:**
     - Hard execution limit (e.g., max 15 minutes per pipeline run).
     - Ephemeral port cleanup and container auto-destruction on run completion.

### Phase 5: Production Quotas & Monetization
- **Target:** Prevent denial-of-service and support tiering.
- **Components:**
  1. **Rate Limiting:**
     - Integrate Redis-backed sliding-window rate limiting (e.g. `slowapi`).
  2. **Tier Limits:**
     - Free tier: max 50k LOC, max 1 concurrent run, standard models.
     - Enterprise tier: unlimited LOC, parallel microservice extraction, dedicated LLM providers (WatsonX.ai on-prem / VPC).

---

## 3. Technology Stack Recommendations

| Layer | Recommended Technology | Alternative |
|---|---|---|
| **API Framework** | FastAPI (already implemented) | - |
| **Worker Queue** | Celery + Redis | ARQ / IBM Code Engine Jobs |
| **Primary Database** | PostgreSQL (asyncpg + SQLAlchemy) | Supabase / RDS |
| **Object Storage** | AWS S3 or IBM Cloud Object Storage | Cloudflare R2 / MinIO |
| **Cache & Pub/Sub** | Redis 7+ | Valkey / AWS ElastiCache |
| **Auth** | Auth0 / Supabase Auth / PyJWT | FastAPI-Users |
| **Container Runtime** | Kubernetes (EKS / IKS) | AWS ECS Fargate / Nomad |

---

## 4. Immediate Post-Hackathon Quick Starts

When ready to start, follow this priority sequence:
1. **Step 1:** Create `reposplit/api/auth.py` and require Bearer JWT tokens on `POST /api/runs`.
2. **Step 2:** Replace local disk writes in `delivery.py` with `boto3` / `ibm-cos-sdk` presigned URLs.
3. **Step 3:** Deploy the web server and Celery worker as two separate containers using Docker Compose.
