# Deployment runbook (AWS)

Everything is Terraform in `infra/terraform`. The design is private by default: no public
endpoints except the API Gateway front door, which requires IAM (SigV4) authentication.

## 0. Prerequisites

| Need | Detail |
|---|---|
| AWS account + admin access for the first apply | Use SSO/MFA. Set an **AWS Budget alert** first. |
| Terraform ≥ 1.6, AWS CLI v2, Docker | |
| Bedrock model access | Console → Bedrock → Model access: enable Titan Text Embeddings V2, Amazon Nova Lite, and the rerank model (in `rerank_region`) |
| A VPC with ≥ 2 private subnets | Needs a path to AWS APIs: VPC endpoints (below) and/or a NAT gateway |
| An S3 bucket + DynamoDB table for Terraform state | Recommended; uncomment the backend in `versions.tf` |
| A GitHub repository | For CI/CD (OIDC role created by Terraform when `github_repo` is set) |

**VPC endpoints** (create if you have no NAT, recommended even with one): `bedrock-runtime`,
`bedrock-agent-runtime`, `bedrock-agent`, `ecr.api`, `ecr.dkr`, `s3` (gateway), `logs`,
`secretsmanager`, `sts`, `comprehend`, `comprehendmedical`. The security group in
`security.tf` allows task egress on 443 to the VPC and to the internet; remove the internet
rule once everything goes through endpoints. The Langfuse host and the rerank region (if it
differs from your region) need a route out if they are not in the VPC.

## 1. Provision

```bash
cd infra/terraform
terraform fmt -recursive            # I could not run fmt where this was written: do it once
cp example.tfvars staging.tfvars    # edit: vpc_id, private_subnet_ids, github_repo, alarm_email
terraform init
terraform plan  -var-file=staging.tfvars -var image_tag=bootstrap
terraform apply -var-file=staging.tfvars -var image_tag=bootstrap
```

The first apply creates the ECS service pointing at an image tag that does not exist yet;
it will keep retrying to start tasks until step 2 pushes one. That is expected.
If `plan` rejects an argument, it is almost certainly provider-version drift (the Bedrock
guardrail and ElastiCache Valkey resources are newer): check the provider docs for the
current attribute names.

Resources created: KMS key; security groups; Bedrock guardrail (+ version); OpenSearch domain
(VPC, IAM auth, encrypted); ElastiCache for Valkey; DynamoDB `insurance-rag-eval-results`;
ECR repo (immutable tags, scan on push); ECS cluster, API service (autoscaling 1–6) and an
ingestion task definition; internal ALB; API Gateway HTTP API + VPC link; IAM roles
(`task`, `exec`, `ingest`, `ci`); CloudWatch alarms; optional CloudTrail.

> **Cache engine caveat.** The semantic tier needs vector search. Terraform provisions
> ElastiCache for **Valkey 8.2**, which supports it in regions where AWS has enabled it. If
> `apply` rejects the engine version, or the app logs search-command errors, set
> `RAG_CACHE_MODE=exact` (hash-only cache, works on any Redis/Valkey) or use MemoryDB.

## 2. Build and push images

```bash
REPO=$(terraform output -raw ecr_repository_url)
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}"
TAG=$(git rev-parse HEAD)
docker build -t "$REPO:$TAG" ../..
docker build --target ingest -t "$REPO:$TAG-ingest" ../..
docker push "$REPO:$TAG" && docker push "$REPO:$TAG-ingest"
terraform apply -var-file=staging.tfvars -var image_tag="$TAG"
```

(After this, CI does the same on every merge to `main`.)

## 3. Feed the system (inside the VPC)

OpenSearch has no public endpoint, so ingestion runs as an ECS task in the same subnets:

```bash
aws ecs run-task \
  --cluster "$(terraform output -raw cluster_name)" \
  --launch-type FARGATE \
  --task-definition "$(terraform output -raw ingest_task_definition)" \
  --network-configuration "awsvpcConfiguration={subnets=[subnet-aaaa,subnet-bbbb],securityGroups=[<app-sg-id>],assignPublicIp=DISABLED}"
```

Use the `app` security group id (`terraform state show aws_security_group.app`). The task
creates the index, downloads the public PDFs listed in `sources.yaml`, generates synthetic
claim notes, and ingests with `regex,comprehend` redaction. Watch it in CloudWatch Logs
(`/ecs/insurance-rag-staging`, stream prefix `ingest`). For your own documents, bake them
into a derived image or extend the command to pull from S3 (add an S3 read permission to
the `ingest` role).

After any re-ingest, redeploy with a bumped `corpus_version` so the cache starts clean:
`terraform apply -var-file=staging.tfvars -var image_tag=$TAG -var corpus_version=v2`.

## 4. Call the API

The route uses `AWS_IAM`, so requests must be SigV4-signed (service `execute-api`):

```bash
API=$(terraform output -raw api_url)
curl -s --aws-sigv4 "aws:amz:us-east-1:execute-api" --user "$AWS_ACCESS_KEY_ID:$AWS_SECRET_ACCESS_KEY" \
  -H "x-amz-security-token: $AWS_SESSION_TOKEN" -H 'content-type: application/json' \
  -d '{"question":"How long do I have to file a proof of loss?"}' "$API/v1/query"
```

The caller's IAM principal needs `execute-api:Invoke` on the API. To use your identity
provider instead, replace `authorization_type = "AWS_IAM"` in `app.tf` with a JWT authoriser.

## 5. Wire up CI/CD

1. Set `github_repo = "owner/repo"` and re-apply; note the `ci_role_arn` output.
2. GitHub → Settings → Variables (repository): `AWS_CI_ROLE_ARN`, `AWS_REGION`,
   `RAG_RERANK_REGION`, `TF_VARS_JSON` (your tfvars as one JSON object, no secrets),
   optionally `TF_ENVIRONMENT`.
3. GitHub → Environments → create `production` and add required reviewers: deploys then
   wait for human approval after the quality gate passes.
4. Uncomment the S3 backend in `versions.tf` so CI and your laptop share state.
5. Pin third-party actions to commit SHAs if your organisation requires it.

The pipeline: `check` + `terraform` + `docker` (parallel) → `eval` (quality gate) →
`deploy` (main only). Branch protection should require all of them.

## 6. Observability setup

* **Langfuse**: self-host following Langfuse's official docs (it needs its own Postgres,
  ClickHouse, Redis and blob storage; keep it in your VPC). Store `{"public_key": "...",
  "secret_key": "..."}` in a Secrets Manager secret, then set `langfuse_host` and
  `langfuse_secret_arn` in tfvars. Tracing switches on automatically.
* **Alarms**: set `alarm_email` and confirm the SNS subscription email.
* **Logs**: `/ecs/insurance-rag-<env>` (JSON); filter by `request_id`.

## 7. Operating it

| Task | How |
|---|---|
| Change a prompt | Edit `prompts_data/rag_answer.yaml`, open a PR; the gate scores it; merge publishes a new Bedrock version and deploys |
| Roll back a prompt | Set `prompt_version` to the previous version number and apply |
| Roll back the app | Re-apply with the previous `image_tag` (ECS also auto-rolls back failed deploys) |
| Re-index documents | Run the ingest task, then bump `corpus_version` |
| Change embedding model/dimension | New index required: `init-index --recreate`, re-ingest, bump `corpus_version` |
| Inspect evaluation history | DynamoDB `insurance-rag-eval-results`, Query by `commit_sha` |
| Scale | Raise `desired_count`/task size; OpenSearch instance type/count for load |

## 8. Teardown (stop the spend)

```bash
terraform destroy -var-file=staging.tfvars -var image_tag=bootstrap
```

KMS keys have a 14-day deletion window; ECR images and any non-empty S3 bucket (trail) may
need manual deletion first. Confirm in the console that the OpenSearch domain, ElastiCache
group, ALB and any NAT gateway are gone: these are the always-on costs.

## Cost shape (verify against current AWS pricing)

Always-on while deployed: OpenSearch node, ElastiCache node(s), ALB, Fargate task(s), VPC
endpoints/NAT, CloudWatch logs. Per-request: Bedrock tokens (embeddings are very cheap; LLM,
rewrite and judge calls dominate), rerank calls, guardrail text units. Evaluation runs add
LLM-judge tokens per commit. At minimum sizes expect tens of dollars per month of fixed
cost, so for a credit-funded account use the local quickstart for development.
