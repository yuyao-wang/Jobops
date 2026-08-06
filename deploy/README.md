# Jobops demo deployment plan

This deployment tree prepares the requested local-first architecture without
moving current business logic or real candidate data:

```text
Docker Desktop
└── kind
    └── jobops-demo namespace
        ├── Jobops API Deployment
        ├── Dashboard Deployment
        ├── Worker Deployment
        ├── PostgreSQL StatefulSet + PVC
        ├── Redis Deployment
        └── MinIO StatefulSet + PVC
```

The manifests use Kustomize so the future cloud deployment keeps the same
application base and replaces only the environment overlay:

```text
deploy/
├── base/
│   ├── api.yaml
│   ├── dashboard.yaml
│   ├── worker.yaml
│   ├── services.yaml
│   ├── serviceaccounts.yaml
│   └── configmap.yaml
└── overlays/
    ├── local/
    │   ├── postgres.yaml
    │   ├── redis.yaml
    │   ├── minio.yaml
    │   └── network-policies.yaml
    └── production/
        └── external-services-config.yaml
```

The older `deploy/k8s/real-application/` directory remains untouched because it
belongs to the existing narrow control-plane experiment. It is not composed
into this demo topology.

## Current safety state

The API, Dashboard and Worker Deployments intentionally have `replicas: 0` and
carry `jobops.dev/status: pending-business-entrypoint`. Their planned commands
do not exist yet:

| Workload | Required module | Health contract |
|---|---|---|
| API | `python -m jobops.api` | HTTP `8080`, `/health/live`, `/health/ready` |
| Dashboard | `python -m jobops.dashboard` | HTTP `3000`, `/health/live`, `/health/ready` |
| Worker | `python -m jobops.worker` | health HTTP `8081`, `/health/live`, `/health/ready` |

Do not scale these Deployments above zero until the business-code work supplies
those entry points and their integration tests. All three can initially use one
repository image (`jobops:demo`) with different commands.

The local PostgreSQL, Redis and MinIO manifests are also planning artifacts
until a repository-external `jobops-runtime-secrets` Secret is installed. The
example Secret manifests are excluded from both kustomizations and contain no
usable values.

## Runtime configuration contract

Application code must consume configuration only through these environment
variables; it must not hardcode Kubernetes Service names:

| Variable | Owner | Local value source | Production value source |
|---|---|---|---|
| `DATABASE_URL` | API and Worker | Secret pointing to `postgres:5432/jobops` | External Secret for managed PostgreSQL |
| `REDIS_URL` | API and Worker | Secret pointing to `redis:6379/0` | External Secret for managed Redis |
| `S3_ENDPOINT` | API and Worker | ConfigMap value `http://minio:9000` | ConfigMap value for cloud object storage |
| `S3_BUCKET` | API and Worker | `jobops-artifacts` | Environment-specific bucket name |
| `S3_ACCESS_KEY`, `S3_SECRET_KEY` | API and Worker | Local Secret | Workload identity when supported, otherwise external Secret |
| `JOBOPS_API_BASE_URL` | Dashboard | `http://jobops-api:8080` | Internal Kubernetes Service URL |

PostgreSQL is the planned authoritative business-state store. Redis is only for
non-authoritative notification, cache, rate-limit or queue hints; losing Redis
must never permit duplicate submission or erase a durable workflow decision.
MinIO holds artifact bytes after their metadata and content hashes are committed
in PostgreSQL.

## SQLite transition plan

The current repository has more than one persistence surface:

- legacy `applications.db`, still used by `utils/tracker.py`;
- Private Home SQLite repositories, including the event ledger and candidate
  identity stores when initialized;
- immutable JSON records, CSV queue state, source documents, generated
  materials and browser state under Private Home.

Mounting one SQLite file into the API would not migrate the current system. It
would also be unsafe to split API and Worker while both write a shared SQLite
volume. The transition must be an explicit application migration:

1. Define PostgreSQL repositories and migrations for authoritative entities,
   idempotency keys, leases, permits, submission intent and event history.
2. Keep Private Home and SQLite authoritative while the new adapters are tested
   with synthetic fixtures.
3. Stop all writers, create consistent SQLite backups through the SQLite backup
   API, run `quick_check`, and hash the source snapshots.
4. Import records with stable IDs and content hashes. Upload eligible artifact
   bytes to MinIO and verify every object hash; do not upload browser profiles,
   cookies, ATS credentials or Keychain material.
5. Reconcile table/entity counts, workflow states, immutable hashes and
   duplicate-submission fences before switching reads.
6. Keep a read-only rollback copy and perform one explicit cutover. Never run
   dual independent writers.

A migration Job will be added only after the new repository contracts exist.
It must be one-shot, idempotent, synthetic-data tested and excluded from normal
application Pod startup.

## Local resource envelope

The initial requests/limits target a Mac demo, not production sizing:

| Component | Request | Limit |
|---|---:|---:|
| API | 256 MiB | 512 MiB |
| Dashboard | 128 MiB | 256 MiB |
| Worker / Chromium | 1 GiB | 2 GiB |
| PostgreSQL | 512 MiB | 1 GiB |
| Redis | 64 MiB | 128 MiB |
| MinIO | 256 MiB | 512 MiB |

Configure Docker Desktop with at least 4 CPUs, 6 GiB memory and 25 GiB free
disk for the full demo. The Worker stays at one replica until browser leases,
submission fencing and measured capacity prove a higher safe concurrency.

## Local tool preparation

Docker Desktop is a machine-level runtime and cannot safely be installed inside
the repository. The repository can hold its verified installer plus kind and
kubectl and kubeconform clients under the gitignored `.tools/` directory.
Docker Desktop supplies the Docker daemon and Docker CLI:

```bash
scripts/download_docker_desktop.sh
scripts/bootstrap_local_k8s_tools.sh
source scripts/jobops_k8s_env.sh
scripts/local_k8s.sh doctor
scripts/local_k8s.sh render
scripts/local_k8s.sh validate
```

The Docker Desktop installer must be opened and installed manually because it
writes to `/Applications`, installs privileged helpers and requires license and
security confirmation. Once Docker Desktop is installed and running:

```bash
scripts/local_k8s.sh cluster-up
scripts/local_k8s.sh prepare
scripts/local_k8s.sh status
```

`cluster-up` creates the local kind cluster. `prepare` installs only the
`jobops-demo` namespace, ConfigMap, Services, ServiceAccounts and the three
zero-replica application Deployments. It does not install PostgreSQL, Redis or
MinIO, create secrets, start application Pods or copy real data.

The default kind CNI does not enforce Kubernetes `NetworkPolicy`. The local
overlay already declares default-deny and the required internal paths, but a
pinned policy-capable CNI such as Calico or Cilium must be installed before the
overlay is treated as isolated. Rendering NetworkPolicy objects without an
enforcing CNI is not a passed security check.

## Activation gate after business-code completion

When the application entry points and PostgreSQL/S3/Redis adapters are ready:

1. Rebase or reconcile the application work and rerun the full sanitized test
   suite.
2. Build one `jobops:demo` image and load it with
   `kind load docker-image jobops:demo --name jobops-local`.
3. Install the local runtime Secret through an approved repository-external
   secret workflow; never commit `secret.example.yaml` as a real Secret.
4. Install and verify a pinned NetworkPolicy-capable CNI, then render,
   policy-check and apply `deploy/overlays/local`.
5. Wait for PostgreSQL, Redis and MinIO health. Initialize the artifact bucket.
6. Run schema migrations as a one-shot Job.
7. Patch API, Dashboard and Worker replicas from zero to one and run the
   end-to-end synthetic demo.
8. Restart Pods and recreate the kind node to prove PVC persistence and
   recovery behavior before importing any real data.

The production overlay intentionally contains no PostgreSQL, Redis or MinIO
workloads. It will bind the same application Pods to managed services through
`jobops-runtime` and an external `jobops-runtime-secrets` provider.
