# Kubernetes deployment (pilot)

Plain manifests with a kustomization, validated in CI with `kubeconform -strict` (as written and as rendered).
They were **not applied to a live cluster** during development.

```bash
docker build -t ghcr.io/<you>/automotive-eng-ai:1.0.0 . && docker push ghcr.io/<you>/automotive-eng-ai:1.0.0
kubectl create namespace automotive-eng-ai
kubectl -n automotive-eng-ai create secret generic aeai-secrets \
  --from-literal=ADMIN_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')" \
  --from-literal=OPENAI_API_KEY= --from-literal=JIRA_USER= --from-literal=JIRA_API_TOKEN=
# edit kustomization.yaml (image), ingress.yaml (host, TLS secret), configmap.yaml (LLM gateway, Jira, webhooks)
kubectl apply -k deploy/k8s
kubectl -n automotive-eng-ai logs deploy/aeai-api | grep -i token      # the generated admin token if ADMIN_TOKEN is empty
```

| Resource | Notes |
|---|---|
| `aeai-api` Deployment | 1 replica and `Recreate` strategy: SQLite has one writer and the data volume is ReadWriteOnce. Non-root (uid 10001), read-only root file system, all capabilities dropped, seccomp `RuntimeDefault`, no service-account token. Probes `/ready` and `/health`. The AEB SIL libraries are prebuilt in the image, so no compiler is needed at run time. |
| `aeai-ui` Deployment | 2 replicas (stateless), same hardening, talks to `http://aeai-api:8000`. |
| `aeai-data` PVC | SQLite database, vector store, uploads. Back it up with `python scripts/manage.py backup` (consistent online snapshot). |
| Ingress | Only the UI is published, TLS at the ingress controller; long timeouts for the Streamlit websocket. |
| NetworkPolicies | Default deny; ingress controller -> UI, UI -> API, API egress for DNS and HTTPS (LLM gateway, Jira, webhooks). |
| Namespace | `pod-security.kubernetes.io/enforce: restricted`, which these pods satisfy. |

`AUTH_MODE=enforced` is set in the ConfigMap. Integrations stay in dry-run (`INTEGRATIONS_LIVE=0`) until you
configure the receivers; the background dispatcher delivers webhook events every 30 s once they are live.

To scale the API horizontally, move the records to PostgreSQL and the vectors to a server-based store first (the
schema is plain SQL; the vector store sits behind a small interface).
