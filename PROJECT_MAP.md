# Postgres Operator — Project Map

Legend: ✅ Done

```
postgres-operator/
│
├── crds/
│   ├── managedpostgres.yaml        ✅  CRD schema — registers ManagedPostgres with K8s
│   └── postgresrestore.yaml        ✅  CRD schema — registers PostgresRestore with K8s
│
├── operator/                       (operator pod image)
│   ├── config.py                   ✅  All constants, labels, defaults — single source of truth
│   ├── metrics.py                  ✅  All Prometheus metrics defined here
│   ├── status.py                   ✅  Writes status conditions back to K8s objects
│   ├── provisioner.py              ✅  Creates/patches K8s resources — idempotent
│   ├── main.py                     ✅  kopf handlers: on_create, on_update, on_delete, reconcile timer
│   └── restore.py                  ✅  Full restore Job implementation
│
├── backup/                         (separate container image — runs in CronJob)
│   ├── backup.py                   ✅  pg_dump + pluggable backend (local/S3)
│   ├── Dockerfile                  ✅  Backup container image
│   └── requirements.txt            ✅  boto3 only
│
├── helm/
│   └── postgres-operator/
│       ├── Chart.yaml              ✅  Helm chart metadata
│       ├── values.yaml             ✅  Configurable defaults
│       └── templates/
│           ├── rbac.yaml           ✅  ServiceAccount, ClusterRole, ClusterRoleBinding
│           ├── deployment.yaml     ✅  Operator Deployment manifest
│           ├── servicemonitor.yaml ✅  Prometheus ServiceMonitor
│           └── _helpers.tpl        ✅  Helm template helpers
│
├── examples/
│   └── manifests.yaml              ✅  Example ManagedPostgres YAMLs to test with
│
├── prometheus/
│   ├── prometheus.yml              ✅  Scrape config + ServiceMonitor selectors
│   └── alerting-rules.yaml         ✅  Alerting rules for operator + postgres
│
├── grafana/
│   └── dashboard.json              ✅  Dashboard JSON for operator + instance health
│
├── operator.Dockerfile             ✅  Operator container image (multi-stage)
├── requirements.txt                ✅  kopf, kubernetes, prometheus-client
├── Makefile                        ✅  `make dev`, `make build`, `make deploy`
├── README.md                       ✅  Project details, installation, quickstart
└── LEARNING_GUIDE.md               ✅  Learning material, mental models, code walkthrough
```