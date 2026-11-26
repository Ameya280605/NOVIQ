# Module 17: Production AWS Architecture

## Scope and separation

This document describes a proposed production deployment target for NOVIQ. It
preserves the requested Phase 1 network shape: one VPC across two Availability
Zones, public load-balancer subnets, private application subnets, and isolated
private data subnets. This is separate from the proposed zero-cost student
deployment in [module-17-zero-cost-deployment.md](./module-17-zero-cost-deployment.md).

This is an architecture description only. It does not create AWS resources,
select a region, or imply that any AWS account or budget is available.

## Network and request path

- **VPC and subnets:** Use a dedicated VPC with non-overlapping CIDRs in two
  Availability Zones. Place the internet-facing Application Load Balancer (ALB)
  in one public subnet per AZ. Place application EC2 instances in private
  application subnets and RDS/Redis in private data subnets. Do not assign
  public IPs to application or data instances.
- **Ingress:** Route public HTTPS traffic through the ALB. ACM provides and
  renews the ALB certificate. Permit only the intended listener ports at the
  ALB; security groups allow application traffic from the ALB security group,
  not from arbitrary internet sources.
- **Egress:** Private application subnets need controlled outbound connectivity
  for required external services and image/patch retrieval. A NAT Gateway per
  AZ is the resilient pattern; NAT gateways and data transfer can incur costs.
  Evaluate VPC endpoints where appropriate, including the S3 gateway endpoint
  and   interface endpoints for ECR, CloudWatch Logs, Secrets Manager, and Systems
  Manager. These endpoints also have configuration and, in some cases, cost
  implications.
- **Administration:** Keep instances private and use Systems Manager (SSM)
  Session Manager and Run Command instead of opening SSH to the internet.
  Provide the required SSM connectivity through endpoints or the approved
  egress path.
- **Data boundaries:** RDS and Redis accept connections only from the
  application security group on their required ports. Keep the database
  non-public and use private subnet groups spanning both AZs.

## NOVIQ workload and service mapping

| AWS service | Proposed NOVIQ responsibility |
| --- | --- |
| VPC, subnets, route tables, Internet Gateway, NAT Gateways, security groups | Isolated two-AZ network; public ingress only at the ALB; controlled private workload and data connectivity. |
| Application Load Balancer (ALB) | HTTPS termination and health-checked routing to the FastAPI API instances. |
| EC2 | Run the existing Uvicorn/FastAPI container, Celery workers, and one Celery Beat scheduler. Keep API and worker capacity separately scalable where operationally justified. |
| Auto Scaling Groups (ASG) | Use an API ASG across both AZs and, if worker scaling is needed, a separate Celery worker ASG. Scale API instances independently from worker instances. Any minimum capacity, scaling policy, and deployment strategy require sizing and cost approval. |
| Amazon RDS for PostgreSQL | NOVIQ relational data, including tenant, user, project, task, document metadata, subscription, notification, outbox, and audit records. Use private networking, encryption, backups, and a Multi-AZ configuration appropriate to the recovery objective. |
| ElastiCache for Redis | Redis service for Celery broker/result backend, rate-limit state, and project-risk caching. Keep it private and configure authentication/TLS and availability according to the selected Redis mode. |
| Amazon S3 | Private document-object storage. The existing storage service uses generated `documents/<uuid>` keys, short-lived presigned upload/download URLs, and server-side encryption; the bucket must remain non-public. |
| IAM | EC2 instance profiles with least-privilege access: S3 operations restricted to the NOVIQ bucket/prefix, read only the required Secrets Manager values, and use SSM/CloudWatch permissions. Avoid long-lived AWS access keys on instances. |
| Amazon ECR | Private container image registry for built NOVIQ images. Pin deployments to reviewed image digests and restrict push/pull permissions. |
| ACM | TLS certificate for the ALB HTTPS listener and configured NOVIQ hostname. DNS validation and hostname ownership are deployment prerequisites. |
| AWS Secrets Manager | Store database, Redis, and `JWT_SECRET_KEY` secrets. Grant workload roles only the specific secret ARNs needed. Decide and test a rotation procedure before enabling automatic rotation. |
| Amazon CloudWatch | Collect application, worker, Beat, migration, and host logs/metrics; configure retention, dashboards, and actionable alarms. Do not send secrets, authorization headers, request bodies, or presigned URLs to logs. |
| AWS Systems Manager (SSM) | Private instance inventory, Session Manager, patch/command operations, and deployment support without public SSH ingress. |

## NOVIQ process layout and deployment notes

1. Build the existing container and publish it to ECR using a controlled CI
   identity. Do not put runtime secrets in the image.
2. Run Alembic migrations as a controlled, one-off release task before routing
   traffic to the new application version. Serialize migrations; do not let
   every API or worker replica independently run migrations at startup.
3. Run API instances behind the ALB and use the application's
   `/health/live` endpoint for process health. Use `/health/ready` as an
   operational readiness signal; it checks PostgreSQL, Redis, and worker
   availability.
4. Run Celery workers separately from API instances so queue capacity can be
   scaled without coupling it to web traffic. Run exactly one Beat scheduler
   for the configured periodic jobs unless a tested leader-election or
   distributed scheduler design is introduced. Multiple Beat schedulers can
   enqueue duplicate periodic work.
5. Ensure task handlers and deployment/retry procedures tolerate redelivery.
   The existing Celery configuration enables late acknowledgement and
   requeue-on-worker-loss, so tasks may execute more than once.

## Decisions requiring verification

Before any implementation, verify the AWS region and AZ availability, CIDR
plan, instance sizing, RDS and Redis engine/version and availability modes,
backup/restore objectives, data residency, TLS policy, secret rotation, S3
encryption/key policy, endpoint/NAT design, log retention, scaling thresholds,
deployment/rollback mechanism, and a funded production budget. Production AWS
resources are not assumed to be free.
