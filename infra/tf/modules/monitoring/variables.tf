variable "name_prefix" {
  description = "Prefix used to name/tag every resource this module creates"
  type        = string
}

variable "worker_role_name" {
  description = "Name of the worker nodes' IAM role - Alertmanager (running as a pod on a worker) publishes to the SNS topic through this role's instance profile, same no-IRSA pattern as S3/Bedrock access (see infra/k8s/README.md Step 3)"
  type        = string
}

variable "alert_email" {
  description = "Mailbox that receives cluster alert emails - subscribed to the SNS topic Alertmanager publishes to (infra/k8s/monitoring/values.yaml). AWS emails this address a subscription-confirmation link on first apply; alerts only start flowing once it's clicked."
  type        = string
}
