# Module 17: Strict ₹0 Cost Safety

## Hard budget boundary

The permitted budget for the proposed student/demo deployment is **₹0**.
There is no allowance for paid tiers, usage overages, trial conversion, or
incidental spend. The proposed Render + Supabase + Upstash + Cloudflare R2 +
GHCR stack is not guaranteed to fit that boundary; free allowances and
eligibility must be checked against current provider terms and the account
before use.

This budget does not apply to or authorize the production AWS architecture.
AWS production resources are out of scope for this zero-cost proposal and
must not be created under it.

## Prohibited without explicit scope and budget approval

- AWS resources of any kind for this student deployment, including EC2,
  Auto Scaling, ALB, NAT Gateway, RDS, ElastiCache, S3, ECR, ACM, Secrets
  Manager, CloudWatch, or SSM.
- Any paid plan, paid add-on, trial that converts to paid, paid domain, or
  feature that requires billing.
- Automatic upgrades, autoscaling beyond confirmed free allowances, paid
  backups, paid persistent disks, premium egress, or increased quota purchases.
- Sending real customer/production data or sensitive documents to a demo stack.

## Preventing accidental charges

1. Check current provider plan pages, account eligibility, billing settings,
   resource limits, inactivity policies, and overage behavior for every
   provider. Record links and the date checked outside secrets; re-check when
   provider terms change.
2. Do not enable a paid tier. If a provider requires payment-card registration
   or billing activation, pause deployment until the account owner separately
   approves that specific requirement; do not register the card or activate
   billing before approval. Approval does not change the **₹0** budget. A
   billing alert or budget notification is not a hard spending cap.
3. Turn off auto-upgrade and other billable options where available. Confirm
   whether exceeding a free allowance blocks, throttles, pauses, or bills.
   Reject the provider if accidental charges cannot be reliably prevented or
   if the no-billing behavior cannot be established.
4. Use only a minimal demo workload and synthetic data. Avoid unnecessary
   image versions, high-volume builds, large document uploads, and continuous
   load tests that could consume quotas.
5. Review usage and billing dashboards after setup and regularly while active.
   Set provider alerts if available, but never treat alerts as proof that
   overspend is impossible.
6. Check GitHub Actions and GHCR usage/retention limits as part of the same
   review. Build/publish only what is needed and keep package access restricted.

## Stop and cleanup procedure

- Stop deployment work immediately if a required service is not available
  within verified no-cost limits, if usage is unclear, or if the provider
  indicates paid usage.
- Before removing any demo service, export any data that must be retained and
  confirm the export is usable. Deleting a database, storage bucket, or
  registry package may be irreversible.
- Disable deployed services and scheduled jobs, remove unused test resources,
  revoke credentials that are no longer needed, and confirm provider
  dashboards show no active resources or continuing usage. Check again after
  the provider's usage reporting interval.
- Do not remove or alter repository application code, Docker files,
  dependencies, `.env`, schemas, or AWS resources as part of this cleanup
  guidance.

## Unresolved cost decisions

Provider-specific free limits, overage actions, payment-method requirements,
resource sleep/pause policy, persistent storage, build minutes, data egress,
retention, and eligibility remain unverified. Until each is verified against
current official terms and the target account, the zero-cost deployment is a
proposal only and must not be represented as guaranteed ₹0.
